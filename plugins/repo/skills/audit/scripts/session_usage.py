#!/usr/bin/env python3
"""Aggregate exact token usage for one or more agent sessions.

Reads the per-session JSONL transcripts that Claude Code writes under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (and the Codex
equivalent under `~/.codex/sessions/`). Each assistant message carries a
`usage` block with model-reported counts — no heuristic, no rate-table
estimation — which we sum per session.

USD is computed from a small static rate table; pass `--rates path.json`
to override. Unknown models report `usd: null` rather than a fabricated cost.

Usage:
    session_usage.py --sessions <id> [<id> ...] [--surface claude|codex] [--rates rates.json]

Output: JSON to stdout, shape:

    {
      "sessions": {
        "<session-id>": {
          "model": "claude-opus-4-7",
          "input_tokens": ...,
          "output_tokens": ...,
          "cache_read_input_tokens": ...,
          "cache_creation_input_tokens": ...,
          "messages": <int>,
          "usd": <float|null>,
          "log_path": "<absolute path>",
          "status": "ok" | "not-found" | "schema-unrecognized"
        },
        ...
      },
      "totals": {"input_tokens": ..., "output_tokens": ..., "usd": <float|null>}
    }
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def resolve_out_path(raw: str) -> Path:
    """Resolve --out against the git toplevel when a bare `./...` is given.

    Worktree subagents run from `<repo>/.claude/worktrees/<agent>`, where
    `./.agentic-readiness.usage.json` resolves to the worktree, not the user's
    repo. We anchor relative paths starting with `./` (or no slash) to the
    git toplevel so sidecars always land at the user's working tree.
    Absolute paths are honored as-is.
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return p.resolve()
    # If the toplevel itself is inside a `.claude/worktrees/agent-*/` path,
    # walk up to find the parent repo.
    top_path = Path(top)
    parts = top_path.parts
    if ".claude" in parts and "worktrees" in parts:
        i = parts.index(".claude")
        top_path = Path(*parts[:i])
    return (top_path / p).resolve()

# USD per million tokens. Sourced from upstream pricing pages (May 2026).
# Anthropic cache_write is the 5-minute TTL rate (1.25x input); 1h TTL is 2x.
# Override via --rates for new models.
# Refs:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/pricing
DEFAULT_RATES: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input":  5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-6": {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":  {"input":  1.0, "output":  5.0, "cache_read": 0.10, "cache_write": 1.25},
    "gpt-5.5":           {"input":  5.0, "output": 30.0, "cache_read": 0.50, "cache_write": 0.0},
    "gpt-5.4":           {"input":  2.5, "output": 15.0, "cache_read": 0.25, "cache_write": 0.0},
}

def _claude_search_roots() -> list[Path]:
    roots: list[Path] = [Path.home() / ".claude" / "projects"]
    # Claude Code (≥ recent build) writes backgrounded subagent transcripts as
    # `.output` files under `/private/tmp/claude-<uid>/<encoded-cwd>/<parent-session>/tasks/`.
    # Older builds put them under `~/.claude/projects/<encoded-cwd>/<parent>/subagents/agent-*.jsonl`.
    # Search both; pick the first .jsonl or .output match per session id.
    import glob as _glob
    for hit in _glob.glob("/private/tmp/claude-*"):
        roots.append(Path(hit))
    return roots


SEARCH_ROOTS: list[Path] = _claude_search_roots()


@dataclass
class SessionTotals:
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    messages: int = 0
    usd: float | None = None
    log_path: str | None = None
    status: str = "not-found"


TRANSCRIPT_EXTS = (".jsonl", ".output")


def find_log(session_id: str) -> Path | None:
    """Return the first matching transcript file (`.jsonl` or `.output`) for a
    given session id. Never returns a `.meta.json` sidecar.

    Layouts seen in the wild:
      - Older Claude Code: `~/.claude/projects/<cwd>/<parent>/subagents/agent-<id>.jsonl`
      - Newer Claude Code: `/private/tmp/claude-<uid>/<cwd>/<parent>/tasks/<id>.output`
      - Top-level session : `~/.claude/projects/<cwd>/<sid>.jsonl`
    """
    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        # 1. Exact match across known extensions.
        for ext in TRANSCRIPT_EXTS:
            for hit in root.rglob(f"{session_id}{ext}"):
                return hit
            # 2. `agent-`-prefixed variants for older Claude Code subagent dumps.
            for hit in root.rglob(f"agent-{session_id}{ext}"):
                return hit
        # 3. Fuzzy fallback per extension, never a `.meta.json` sidecar.
        for ext in TRANSCRIPT_EXTS:
            for hit in root.rglob(f"*{session_id}*{ext}"):
                return hit
    return None


def extract_usage(record: dict) -> dict | None:
    # Claude Code shape: {"message": {"usage": {...}, "model": "..."}}.
    # Codex / older shapes may differ — try a couple of fall-backs.
    msg = record.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
        out = dict(msg["usage"])
        if "model" in msg:
            out["_model"] = msg["model"]
        return out
    if isinstance(record.get("usage"), dict):
        out = dict(record["usage"])
        if "model" in record:
            out["_model"] = record["model"]
        return out
    return None


def aggregate(log: Path) -> SessionTotals:
    totals = SessionTotals(log_path=str(log))
    saw_usage = False
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                usage = extract_usage(rec)
                if not usage:
                    continue
                saw_usage = True
                totals.messages += 1
                if totals.model is None and usage.get("_model"):
                    totals.model = usage["_model"]
                totals.input_tokens                += int(usage.get("input_tokens", 0) or 0)
                totals.output_tokens               += int(usage.get("output_tokens", 0) or 0)
                totals.cache_read_input_tokens     += int(usage.get("cache_read_input_tokens", 0) or 0)
                totals.cache_creation_input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
    except OSError as exc:
        totals.status = f"read-error: {exc}"
        return totals
    totals.status = "ok" if saw_usage else "schema-unrecognized"
    return totals


def cost_usd(t: SessionTotals, rates: dict[str, dict[str, float]]) -> float | None:
    if t.model is None:
        return None
    rate = rates.get(t.model)
    if rate is None:
        # try a prefix match for fast-evolving model ids
        rate = next((r for k, r in rates.items() if t.model.startswith(k)), None)
    if rate is None:
        return None
    per_m = 1_000_000.0
    return round(
        t.input_tokens                * rate["input"]       / per_m
        + t.output_tokens             * rate["output"]      / per_m
        + t.cache_read_input_tokens   * rate["cache_read"]  / per_m
        + t.cache_creation_input_tokens * rate.get("cache_write", 0.0) / per_m,
        4,
    )


def load_rates(path: str | None) -> dict[str, dict[str, float]]:
    if not path:
        return DEFAULT_RATES
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--rates", help="JSON file overriding the per-million-token rate table")
    ap.add_argument("--out",   help="Write JSON to this path instead of stdout")
    args = ap.parse_args(argv)

    rates = load_rates(args.rates)
    out_sessions: dict[str, dict] = {}
    grand = SessionTotals()
    grand_usd: float = 0.0
    grand_has_cost = False

    for sid in args.sessions:
        log = find_log(sid)
        if log is None:
            out_sessions[sid] = {**SessionTotals().__dict__, "status": "not-found"}
            continue
        totals = aggregate(log)
        totals.usd = cost_usd(totals, rates)
        out_sessions[sid] = totals.__dict__
        grand.input_tokens                += totals.input_tokens
        grand.output_tokens               += totals.output_tokens
        grand.cache_read_input_tokens     += totals.cache_read_input_tokens
        grand.cache_creation_input_tokens += totals.cache_creation_input_tokens
        if totals.usd is not None:
            grand_usd += totals.usd
            grand_has_cost = True

    report = {
        "sessions": out_sessions,
        "totals": {
            "input_tokens": grand.input_tokens,
            "output_tokens": grand.output_tokens,
            "cache_read_input_tokens": grand.cache_read_input_tokens,
            "cache_creation_input_tokens": grand.cache_creation_input_tokens,
            "usd": round(grand_usd, 4) if grand_has_cost else None,
        },
    }
    if args.out:
        out_path = resolve_out_path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(out_path)
    else:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
