#!/usr/bin/env python3
"""Render a code-audit HTML report.

Reads a JSON findings document from --in (or stdin) and writes a
self-contained HTML report to --out. The visual style mirrors the
repo-audit report so the two plugins feel like one family.

Input JSON schema:
{
  "target":   "relative/or/absolute path that was reviewed",
  "stack":    "Python · pytest · ruff",           (optional, short)
  "files_scanned": 142,                            (optional)
  "summary":  "one-paragraph overall impression",  (optional)
  "recommendations": ["...", "..."],               (optional, top 3-7)
  "findings": [
    {
      "id":       "F1",
      "title":    "Swallowed exception in retry loop",
      "category": "bugs",        # bugs|security|anti-patterns|complexity|refactor|dead-code|tests|dependencies|performance|docs
      "severity": "critical",    # critical|high|medium|low
      "effort":   "S",           # S|M|L
      "where":    "src/api/client.py:88-104",
      "what":     "What the issue is, in plain terms.",
      "why":      "Why it matters.",
      "fix":      "Suggested fix in one or two sentences."
    }
  ]
}
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sys
from pathlib import Path

# --- visual tokens copied from plugins/repo audit so the two reports match ---

CSS = """
:root {
  --bg:#0b0d12; --panel:#151926; --panel-2:#1c2133;
  --ink:#f1f4fa; --mute:#a4adc1;
  --line:#2a3045; --line-2:#3a4260;
  --accent:#9ebaff; --accent-ink:#0b1020; --accent-bg:rgba(158,186,255,.16);
  --ok:#5cd2a8;   --ok-fg:#74dab4;    --ok-bg:rgba(92,210,168,.16);
  --warn:#f5c451; --warn-fg:#f5c451;  --warn-bg:rgba(245,196,81,.18);
  --bad:#ff5d6c;  --bad-fg:#ff8a93;   --bad-bg:rgba(255,93,108,.16);
  --radius:14px; --radius-sm:10px;
  --shadow:0 1px 0 rgba(255,255,255,.03) inset, 0 6px 24px rgba(0,0,0,.25);
  --bg-glow-a:color-mix(in srgb, var(--accent) 8%, transparent);
  --bg-glow-b:color-mix(in srgb, #8b5cf6 6%, transparent);
}
:root[data-theme="light"] {
  --bg:#f7f9fd; --panel:#fff; --panel-2:#eef1f8;
  --ink:#0e1422; --mute:#4a5365;
  --line:#d4dae8; --line-2:#a9b2c5;
  --accent:#1d4ed8; --accent-ink:#fff; --accent-bg:rgba(29,78,216,.14);
  --ok:#047857; --ok-fg:#065f46; --ok-bg:rgba(16,185,129,.20);
  --warn:#b45309; --warn-fg:#92400e; --warn-bg:rgba(217,119,6,.20);
  --bad:#dc2626; --bad-fg:#b91c1c; --bad-bg:rgba(220,38,38,.16);
  --shadow:0 1px 0 rgba(255,255,255,.9) inset, 0 12px 32px rgba(15,23,42,.10);
}
* { box-sizing:border-box }
:focus { outline:none }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:6px }
html,body { margin:0; background:
  radial-gradient(1200px 600px at 100% -10%, var(--bg-glow-a), transparent 60%),
  radial-gradient(900px 500px at -10% 110%, var(--bg-glow-b), transparent 55%),
  var(--bg);
  color:var(--ink);
  font:14.5px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Inter,sans-serif;
  -webkit-font-smoothing:antialiased; }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
button { font:inherit; color:inherit }

.app { display:grid; grid-template-columns:248px 1fr; min-height:100vh }
nav.side { position:sticky; top:0; height:100vh; padding:22px 14px;
  border-right:1px solid var(--line); background:var(--panel); overflow:auto;
  box-shadow:var(--shadow) }
nav.side .brand { display:flex; align-items:center; gap:8px; padding:4px 6px 16px;
  font-weight:600 }
nav.side .brand .dot { width:10px; height:10px; border-radius:3px;
  background:linear-gradient(135deg,var(--accent),#a47bff) }
nav.side h1 { font-size:11px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--mute); margin:14px 6px 6px; font-weight:600 }
nav.side ol { list-style:none; padding:0; margin:0; counter-reset:step }
nav.side li { counter-increment:step; margin:1px 0 }
nav.side a { display:flex; align-items:center; gap:10px; padding:7px 10px;
  border-radius:var(--radius-sm); color:var(--ink); font-size:13.5px }
nav.side a::before { content:counter(step); display:inline-flex;
  width:18px; height:18px; align-items:center; justify-content:center;
  border-radius:4px; background:var(--panel-2); color:var(--mute);
  font:11px/1 ui-monospace,SF Mono,Menlo,monospace }
nav.side a:hover { background:var(--panel-2); text-decoration:none }
nav.side a.active { background:var(--accent-bg); color:var(--accent) }
nav.side a.active::before { background:var(--accent); color:var(--accent-ink) }

main { padding:0 40px 80px; max-width:1080px; width:100% }
header.top { position:sticky; top:0; z-index:10; padding:18px 0 14px;
  background:linear-gradient(var(--bg) 70%, transparent);
  display:flex; align-items:center; gap:14px; flex-wrap:wrap }
header.top .repo { font-size:15px; font-weight:600 }
header.top .path { color:var(--mute); font:12.5px/1.5 ui-monospace,SF Mono,Menlo,monospace;
  background:var(--panel-2); padding:3px 8px; border-radius:6px; word-break:break-all }
header.top .ts { color:var(--mute); font-size:12.5px; margin-left:auto }
.theme-toggle { background:var(--panel); border:1px solid var(--line); color:var(--mute);
  width:36px; height:36px; display:inline-flex; align-items:center;
  justify-content:center; border-radius:999px; cursor:pointer; padding:0 }
.theme-toggle:hover { color:var(--ink); border-color:var(--line-2) }
.theme-toggle svg { width:16px; height:16px }
.theme-toggle .icon-sun { display:none }
:root[data-theme="light"] .theme-toggle .icon-sun { display:block }
:root[data-theme="light"] .theme-toggle .icon-moon { display:none }

section { scroll-margin-top:24px; padding:28px 0 4px; border-top:1px solid var(--line) }
section:first-of-type { border-top:0; padding-top:8px }
h2 { margin:0 0 18px; font-size:20px; letter-spacing:-.01em }
h3 { margin:22px 0 10px; font-size:12px; color:var(--mute); font-weight:600;
  letter-spacing:.08em; text-transform:uppercase }

.hero { display:grid; grid-template-columns:auto 1fr; gap:22px; align-items:start;
  padding:22px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); box-shadow:var(--shadow); margin-bottom:18px;
  position:relative; overflow:hidden }
.hero::before { content:''; position:absolute; inset:0 0 auto 0; height:3px;
  background:linear-gradient(90deg, var(--bad), var(--warn), var(--ok), var(--accent)) }
.hero .grade { width:88px; height:88px; border-radius:18px; display:grid;
  place-items:center; font-size:34px; font-weight:700;
  background:var(--accent-bg); color:var(--accent);
  border:1px solid color-mix(in srgb, var(--accent) 30%, transparent) }
.hero.tone-ok   .grade { background:var(--ok-bg);   color:var(--ok-fg) }
.hero.tone-warn .grade { background:var(--warn-bg); color:var(--warn-fg) }
.hero.tone-bad  .grade { background:var(--bad-bg);  color:var(--bad-fg) }
.hero .rationale { margin:6px 0 14px; font-size:16px; line-height:1.45 }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px; margin-top:6px }
.kpi { padding:12px 14px; background:var(--panel-2); border-radius:var(--radius-sm);
  border:1px solid var(--line) }
.kpi .label { color:var(--mute); font-size:11.5px; text-transform:uppercase;
  letter-spacing:.06em; margin-bottom:4px }
.kpi .value { font:600 18px/1.2 ui-sans-serif,system-ui,Inter,sans-serif;
  font-variant-numeric:tabular-nums }
.kpi.tone-bad  .value { color:var(--bad-fg) }
.kpi.tone-warn .value { color:var(--warn-fg) }
.kpi.tone-ok   .value { color:var(--ok-fg) }

ol.recs { padding-left:0; margin:0; list-style:none; counter-reset:rec }
ol.recs li { counter-increment:rec; position:relative; padding:10px 14px 10px 42px;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius-sm); margin:6px 0 }
ol.recs li::before { content:counter(rec); position:absolute; left:12px; top:10px;
  width:22px; height:22px; border-radius:6px; background:var(--accent-bg);
  color:var(--accent); display:grid; place-items:center;
  font:600 12px/1 ui-monospace,monospace }

.finding { padding:16px 18px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius-sm); margin:10px 0; position:relative }
.finding .head { display:flex; flex-wrap:wrap; align-items:center; gap:8px;
  margin-bottom:8px }
.finding .id { font:600 12px/1 ui-monospace,monospace; color:var(--mute);
  background:var(--panel-2); padding:3px 7px; border-radius:6px }
.finding .title { font-weight:600; font-size:15px; flex:1; min-width:200px }
.finding .where { font:12.5px/1.5 ui-monospace,SF Mono,Menlo,monospace;
  color:var(--mute); background:var(--panel-2); padding:3px 8px;
  border-radius:6px; word-break:break-all; margin:6px 0 8px }
.finding p { margin:6px 0 }
.finding p.fix { margin-top:8px; padding-top:8px; border-top:1px dashed var(--line) }
.finding p.fix strong { color:var(--accent) }

.chip { display:inline-flex; align-items:center; gap:6px; padding:3px 10px;
  border-radius:999px; font-size:12px; background:var(--panel-2); color:var(--mute);
  border:1px solid var(--line) }
.chip.sev-critical { color:var(--bad-fg);  background:var(--bad-bg);  border-color:transparent }
.chip.sev-high     { color:var(--bad-fg);  background:var(--bad-bg);  border-color:transparent; opacity:.85 }
.chip.sev-medium   { color:var(--warn-fg); background:var(--warn-bg); border-color:transparent }
.chip.sev-low      { color:var(--ok-fg);   background:var(--ok-bg);   border-color:transparent }
.chip.eff { font:600 11.5px/1 ui-monospace,monospace }

.empty { color:var(--mute); padding:14px 16px; background:var(--panel);
  border:1px dashed var(--line); border-radius:var(--radius-sm) }

@media (max-width:880px) {
  .app { grid-template-columns:1fr }
  nav.side { position:static; height:auto; border-right:0;
    border-bottom:1px solid var(--line) }
  nav.side ol { display:flex; flex-wrap:wrap; gap:4px }
  main { padding:0 18px 60px }
  .hero { grid-template-columns:1fr }
}
@media print {
  .app { grid-template-columns:1fr }
  nav.side, .theme-toggle { display:none }
  main { padding:0; max-width:none }
  section, .finding { break-inside:avoid }
  body { background:#fff; color:#000 }
}
"""

THEME_BOOT_JS = """
(function () {
  try {
    var saved = localStorage.getItem('code-audit-theme');
    var theme = (saved === 'light' || saved === 'dark') ? saved
      : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (_) { document.documentElement.setAttribute('data-theme', 'dark'); }
})();
"""

SCROLLSPY = """
(function () {
  const root = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  if (toggle) toggle.addEventListener('click', () => {
    const cur = root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = cur === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('code-audit-theme', next); } catch (_) {}
  });
  const links = [...document.querySelectorAll('nav.side a')];
  const sections = links.map(a => document.querySelector(a.getAttribute('href')));
  function setActive(i) { links.forEach((a, j) => a.classList.toggle('active', j === i)); }
  function onScroll() {
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
      setActive(sections.length - 1); return;
    }
    const probe = window.scrollY + window.innerHeight * 0.3;
    let idx = 0;
    sections.forEach((s, i) => { if (s && s.offsetTop <= probe) idx = i; });
    setActive(idx);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  onScroll();
})();
"""

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
EFFORT_ORDER = {"S": 0, "M": 1, "L": 2}
CATEGORY_LABELS = {
    "bugs":          "Bugs & correctness",
    "security":      "Security",
    "anti-patterns": "Anti-patterns",
    "complexity":    "Complexity hotspots",
    "refactor":      "Refactoring opportunities",
    "dead-code":     "Dead & duplicated code",
    "tests":         "Tests",
    "dependencies":  "Dependencies",
    "performance":   "Performance",
    "docs":          "Documentation",
}


def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def fmt_int(n: int | None) -> str:
    return "—" if n is None else f"{int(n):,}"


def fmt_human_datetime(now: datetime.datetime) -> str:
    day = str(now.day)
    hour = str(((now.hour - 1) % 12) + 1)
    return now.strftime(f"%b {day}, %Y · {hour}:%M %p")


def overall_tone(counts: dict[str, int]) -> tuple[str, str, str]:
    if counts.get("critical", 0):
        return "tone-bad", "!", "Critical issues found. Address before shipping."
    if counts.get("high", 0):
        return "tone-bad", str(counts["high"]), "High-severity issues are present."
    if counts.get("medium", 0):
        return "tone-warn", str(counts["medium"]), "Maintainability concerns to plan in."
    if counts.get("low", 0):
        return "tone-ok", str(counts["low"]), "Only minor nits found."
    return "tone-ok", "✓", "No findings — codebase looks clean against the checks performed."


def render_header(target: str, ts: datetime.datetime) -> str:
    sun = ('<svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
           'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/>'
           '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41'
           'M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>')
    moon = ('<svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 '
            '7 7 0 0 0 21 12.79z"/></svg>')
    return (
        f'<header class="top"><span class="repo">Code audit</span>'
        f'<span class="path">{e(target)}</span>'
        f'<span class="ts">{e(fmt_human_datetime(ts))}</span>'
        f'<button id="theme-toggle" class="theme-toggle" type="button" '
        f'aria-label="Toggle theme">{sun}{moon}</button></header>'
    )


def render_hero(doc: dict, counts: dict[str, int]) -> str:
    tone, grade, default_rationale = overall_tone(counts)
    rationale = doc.get("summary") or default_rationale
    kpis = [
        ("Critical", counts.get("critical", 0), "tone-bad" if counts.get("critical") else ""),
        ("High",     counts.get("high", 0),     "tone-bad" if counts.get("high") else ""),
        ("Medium",   counts.get("medium", 0),   "tone-warn" if counts.get("medium") else ""),
        ("Low",      counts.get("low", 0),      "tone-ok" if counts.get("low") else ""),
    ]
    if doc.get("files_scanned") is not None:
        kpis.append(("Files scanned", doc["files_scanned"], ""))
    if doc.get("stack"):
        kpis.append(("Stack", doc["stack"], ""))
    kpi_html = "".join(
        f'<div class="kpi {tc}"><div class="label">{e(lbl)}</div>'
        f'<div class="value">{e(fmt_int(v) if isinstance(v, int) else v)}</div></div>'
        for lbl, v, tc in kpis
    )
    return (
        f'<section id="overview"><h2>Overview</h2>'
        f'<div class="hero {tone}"><div class="grade">{e(grade)}</div>'
        f'<div><div class="rationale">{e(rationale)}</div>'
        f'<div class="kpis">{kpi_html}</div></div></div></section>'
    )


def render_recommendations(recs: list[str]) -> str:
    if not recs:
        return ""
    items = "".join(f"<li>{e(r)}</li>" for r in recs)
    return (
        f'<section id="recs"><h2>Top recommendations</h2>'
        f'<ol class="recs">{items}</ol></section>'
    )


def render_finding(f: dict) -> str:
    sev = (f.get("severity") or "low").lower()
    eff = (f.get("effort") or "M").upper()
    cat = (f.get("category") or "").lower()
    cat_label = CATEGORY_LABELS.get(cat, cat or "—")
    head = (
        f'<div class="head">'
        f'<span class="id">{e(f.get("id", ""))}</span>'
        f'<span class="title">{e(f.get("title", "(untitled)"))}</span>'
        f'<span class="chip sev-{e(sev)}">{e(sev)}</span>'
        f'<span class="chip eff">effort {e(eff)}</span>'
        f'<span class="chip">{e(cat_label)}</span>'
        f"</div>"
    )
    where = (
        f'<div class="where">{e(f.get("where", ""))}</div>'
        if f.get("where") else ""
    )
    body = []
    if f.get("what"):
        body.append(f'<p>{e(f["what"])}</p>')
    if f.get("why"):
        body.append(f'<p><strong>Why it matters:</strong> {e(f["why"])}</p>')
    if f.get("fix"):
        body.append(f'<p class="fix"><strong>Suggested fix:</strong> {e(f["fix"])}</p>')
    return f'<article class="finding">{head}{where}{"".join(body)}</article>'


def render_findings_by_severity(findings: list[dict]) -> str:
    if not findings:
        return (
            '<section id="findings"><h2>Findings</h2>'
            '<div class="empty">No findings to report.</div></section>'
        )
    findings = sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER.get((f.get("severity") or "low").lower(), 9),
            EFFORT_ORDER.get((f.get("effort") or "M").upper(), 9),
        ),
    )
    groups: dict[str, list[dict]] = {}
    for f in findings:
        groups.setdefault((f.get("severity") or "low").lower(), []).append(f)
    parts = ['<section id="findings"><h2>Findings</h2>']
    for sev in ("critical", "high", "medium", "low"):
        if sev not in groups:
            continue
        parts.append(f"<h3>{sev} · {len(groups[sev])}</h3>")
        for f in groups[sev]:
            parts.append(render_finding(f))
    parts.append("</section>")
    return "".join(parts)


def build_html(doc: dict) -> str:
    findings = doc.get("findings", []) or []
    counts: dict[str, int] = {}
    for f in findings:
        sev = (f.get("severity") or "low").lower()
        counts[sev] = counts.get(sev, 0) + 1
    target = doc.get("target") or "."
    ts = datetime.datetime.now()
    nav_links = [("#overview", "Overview")]
    if doc.get("recommendations"):
        nav_links.append(("#recs", "Recommendations"))
    nav_links.append(("#findings", "Findings"))
    nav_html = "".join(f'<li><a href="{h}">{e(l)}</a></li>' for h, l in nav_links)
    body = (
        render_header(target, ts)
        + render_hero(doc, counts)
        + render_recommendations(doc.get("recommendations") or [])
        + render_findings_by_severity(findings)
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Code audit — {e(target)}</title>"
        f"<script>{THEME_BOOT_JS}</script>"
        f"<style>{CSS}</style></head><body>"
        '<div class="app">'
        '<nav class="side" aria-label="Sections">'
        '<div class="brand"><span class="dot"></span>code audit</div>'
        f'<h1>Sections</h1><ol>{nav_html}</ol></nav>'
        f"<main>{body}</main></div>"
        f"<script>{SCROLLSPY}</script></body></html>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a code-audit HTML report.")
    ap.add_argument("--in",  dest="inp", default="-",
                    help="findings JSON file (default: stdin)")
    ap.add_argument("--out", default="./.code-audit.html",
                    help="output HTML path (default: ./.code-audit.html)")
    args = ap.parse_args()

    if args.inp == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.inp).read_text(encoding="utf-8")
    doc = json.loads(raw)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(doc), encoding="utf-8")
    print(str(out_path.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
