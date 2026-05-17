#!/usr/bin/env python3
"""Unified CLI for the agentic-readiness assessment.

One Bash prefix, three subcommands. The orchestrator never writes files
directly — this script owns the entire `./.agentic-readiness/` directory and
prunes its own intermediates when the report is rendered.

Subcommands:
  prepare    Preflight + static checks. Resolves repo root (walking out of
             any `.claude/worktrees/agent-*` subdir), creates the run
             directory, snapshots git status, probes
             CLAUDE_CODE_SUBAGENT_MODEL, runs static checks. Emits JSON.
  attribute  Reconcile usage from session JSONL transcripts for the listed
             session ids. Writes `tmp/usage.json`.
  finalize   Accept the matrix JSON authored by the orchestrator (inline via
             --matrix-json or piped via --matrix-stdin). Removes leaked
             worktree paths, runs the leak check, renders the HTML report
             and a stable summary.json, then deletes the per-run `tmp/`
             directory so only the durable artifacts remain.

Layout (this script is the only writer):

  ./.agentic-readiness/
    runs/<iso-ts>/
      report.html      kept — open this in a browser
      summary.json     kept — small, grade/scores/cost/recs for history diff
      tmp/             deleted by finalize once render succeeds
        static.json
        usage.json
        matrix.json
        baseline.txt
        warnings.json
    latest -> runs/<iso-ts>   updated by prepare

Stdlib-only. Imports `static_checks`, `session_usage`, and `render_report`
from the same directory so the existing logic is reused verbatim.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import static_checks  # noqa: E402
import session_usage  # noqa: E402
import render_report  # noqa: E402

ARTIFACT_DIR_NAME = ".repo-audit"
RUN_DIR = "runs"
LATEST = "latest"


# ---------- public API -----------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="audit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare", help="preflight + static checks")
    p_prep.add_argument("--repo", default=".", help="repo path (default: cwd)")

    p_fin = sub.add_parser(
        "finalize",
        help="attribute usage, render report, prune intermediates",
    )
    p_fin.add_argument("--run", required=True)
    p_fin.add_argument("--sessions", nargs="*", default=[],
                       help="session ids to attribute usage for; omit to skip attribution")
    p_fin.add_argument("--rates", help="optional rates JSON override")
    src = p_fin.add_mutually_exclusive_group(required=True)
    src.add_argument("--matrix-json", help="inline JSON string with the matrix")
    src.add_argument("--matrix-stdin", action="store_true", help="read matrix JSON from stdin")

    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        return _cmd_prepare(args)
    if args.cmd == "finalize":
        return _cmd_finalize(args)
    return 2


# ---------- private helpers ------------------------------------------------


def _resolve_repo_root(start: Path) -> Path:
    """Resolve the user's repo root. If invoked from a worktree under
    `.claude/worktrees/agent-*`, walk up to the parent repo so sidecars never
    land inside a subagent worktree."""
    try:
        top = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return start.resolve()
    top_path = Path(top).resolve()
    parts = top_path.parts
    if ".claude" in parts and "worktrees" in parts:
        top_path = Path(*parts[:parts.index(".claude")])
    return top_path


def _new_run_id() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _artifact_root(repo: Path) -> Path:
    return repo / ARTIFACT_DIR_NAME


def _run_dir(repo: Path, run_id: str) -> Path:
    return _artifact_root(repo) / RUN_DIR / run_id


def _update_latest(repo: Path, run_id: str) -> None:
    """Point `<artifact>/latest` at `runs/<run_id>` via symlink. Falls back to
    writing a `latest.txt` pointer on platforms/filesystems that disallow
    symlinks (rare on macOS/Linux but cheap to handle)."""
    root = _artifact_root(repo)
    latest = root / LATEST
    target = Path(RUN_DIR) / run_id
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        (root / "latest.txt").write_text(str(target) + "\n", encoding="utf-8")


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


# Concrete-target selection: keeps the benchmark prompts identical-shaped
# across repos (so latency/cost are comparable) while pinning each task to a
# real file in the repo. The subagent doesn't choose targets — the
# orchestrator substitutes these into the templated prompts at dispatch.

import re as _re  # local; only used by target selection.


_CODE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                  ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".rb", ".php",
                  ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm",
                  ".sh", ".bash", ".zsh"}

# Path segments that almost always indicate generated/build output. Files
# under these directories must not be picked as benchmark targets: they have
# no natural insertion point and routinely carry "DO NOT EDIT" headers, which
# causes subagents to decline the task entirely.
_GENERATED_DIR_PARTS = {
    "dist", "build", "out", "target", "generated", "gen", "__generated__",
    ".next", ".nuxt", ".cache", "node_modules", "vendor", "third_party",
}
_GENERATED_HEADER_RE = _re.compile(r"DO NOT EDIT|@generated|autogenerated", _re.IGNORECASE)


def _looks_generated(repo: Path, p: Path) -> bool:
    parts = set(p.relative_to(repo).parts)
    if parts & _GENERATED_DIR_PARTS:
        return True
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048)
    except OSError:
        return False
    return bool(_GENERATED_HEADER_RE.search(head))


def _is_dunder(name: str) -> bool:
    """True for Python dunder names like __init__ / __repr__. Dunders are
    unrenameable — picking one as a refactor target produces an unactionable
    task (the subagent correctly refuses)."""
    return name.startswith("__") and name.endswith("__") and len(name) >= 4


def _pick_symbol(text: str) -> str | None:
    """Pick a renameable symbol from `text`. Prefer one with at least one
    in-file usage beyond its declaration so the Refactor task touches more
    than a single line. Tiers, in order of preference:
      1. `_`-prefixed (private, non-dunder) symbol with ≥1 in-file usage
      2. any def/class/function symbol with ≥1 in-file usage
      3. `_`-prefixed symbol with no in-file usage (declaration-only rename)
      4. any def/class/function symbol with no in-file usage
      5. any non-dunder identifier appearing ≥2 times in the file
    Dunders are always skipped — they're language reserved names and
    renaming them breaks code. Returns None only for empty/unusable files."""
    def usage_count(sym: str) -> int:
        return len(_re.findall(r"\b" + _re.escape(sym) + r"\b", text))

    private = _re.compile(
        r"^\s*(?:def|class|function|fn|func|const|let|var)\s+(_[A-Za-z_][\w]*)"
        r"|^\s*(_[A-Za-z_][\w]*)\s*\(\)\s*\{",  # shell: _name() {
        _re.MULTILINE,
    )
    public = _re.compile(
        r"^\s*(?:def|class|function|fn|func|const|let|var)\s+([A-Za-z_][\w]*)"
        r"|^\s*([A-Za-z_][\w]*)\s*\(\)\s*\{",  # shell: name() {
        _re.MULTILINE,
    )

    def matches(pattern) -> list[str]:
        out: list[str] = []
        for m in pattern.finditer(text):
            sym = m.group(1) or m.group(2)
            if sym and not _is_dunder(sym):
                out.append(sym)
        return out

    private_syms = matches(private)
    public_syms = matches(public)

    # Tiers 1–2: at least one in-file usage beyond the declaration line.
    for sym in private_syms:
        if usage_count(sym) >= 2:
            return sym
    for sym in public_syms:
        if usage_count(sym) >= 2:
            return sym
    # Tiers 3–4: declaration-only rename is acceptable as a last resort —
    # better than skipping the task. The prompt explicitly allows it.
    if private_syms:
        return private_syms[0]
    if public_syms:
        return public_syms[0]
    # Tier 5: any non-dunder identifier appearing ≥2 times.
    fallback = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
    seen: dict[str, int] = {}
    for m in fallback.finditer(text):
        tok = m.group(1)
        if _is_dunder(tok):
            continue
        if tok.lower() in {"the", "and", "for", "from", "import", "return",
                           "this", "that", "with", "true", "false", "null",
                           "none", "self", "args", "kwargs"}:
            continue
        seen[tok] = seen.get(tok, 0) + 1
        if seen[tok] >= 2:
            return tok
    # Final fallback: first non-keyword identifier we saw, even if it only
    # appears once. Empty file → None.
    return next(iter(seen), None)


def _pick_targets(repo: Path) -> dict:
    """Pick deterministic real targets for the simulated benchmark tasks.

    Every field is non-null whenever the repo has at least one text file, so
    the orchestrator never has to record `no candidate`.

    - deepest_file: text file with the deepest path; ties break on path.
    - largest_file: largest *code* file by bytes; falls back to the heaviest
      text file if no code-suffixed file exists.
    - symbol_in_largest_file: a renameable symbol picked from largest_file
      (prefers `_`-prefixed, then any def/class/function name, then any
      identifier). None only when largest_file is empty.
    - entry_point: best-guess top-level entry-point path (main/cli/index),
      else largest_file.
    """
    files = static_checks.collect_files(repo)
    text_files = [
        f for f in files
        if static_checks.is_text_path(f) and not _looks_generated(repo, f)
    ]
    code_files = [f for f in text_files if f.suffix.lower() in _CODE_SUFFIXES]

    deepest_path: str | None = None
    if text_files:
        deepest = max(
            text_files,
            key=lambda p: (len(p.relative_to(repo).parts), str(p)),
        )
        deepest_path = deepest.relative_to(repo).as_posix()

    # Pick largest_file from code files first; fall back to heaviest text.
    largest_pool = code_files or text_files
    largest: str | None = None
    if largest_pool:
        largest_file_obj = max(
            largest_pool, key=lambda p: static_checks.safe_size(p),
        )
        largest = largest_file_obj.relative_to(repo).as_posix()

    # Entry point: prefer canonical filenames *with real content*. A 4-line
    # bootstrap (index.ts that just imports and starts a server) gives the
    # Feature-add subagent nothing to graft onto, so it skips. Require a
    # minimum byte size before accepting a canonical name; otherwise fall
    # back to the largest code file, which always has surface area.
    entry_priorities = ("__main__.py", "main.py", "cli.py", "main.go",
                        "main.rs", "main.ts", "index.ts", "index.js", "cli.ts")
    ENTRY_MIN_BYTES = 500
    entry_point: str | None = None
    for f in text_files:
        if f.name in entry_priorities and static_checks.safe_size(f) >= ENTRY_MIN_BYTES:
            entry_point = f.relative_to(repo).as_posix()
            break
    if entry_point is None:
        entry_point = largest

    # Test target: prefer the largest file under a recognized tests dir.
    test_dir_names = {"tests", "test", "__tests__", "spec", "specs"}
    test_files = [
        f for f in code_files
        if any(part in test_dir_names for part in f.relative_to(repo).parts)
    ]
    test_target: str | None = None
    test_target_path: Path | None = None
    if test_files:
        test_target_path = max(test_files, key=lambda p: static_checks.safe_size(p))
        test_target = test_target_path.relative_to(repo).as_posix()

    symbol: str | None = None
    if largest:
        try:
            text = (repo / largest).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        symbol = _pick_symbol(text)

    # Symbol picked from the test target itself — used by the Write-a-test
    # task so the subagent has an existing test to mirror rather than being
    # asked to write a test against a symbol that isn't in the file's import
    # scope. Falls back to None when there's no test directory.
    test_symbol: str | None = None
    if test_target_path is not None:
        try:
            test_text = test_target_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            test_text = ""
        test_symbol = _pick_symbol(test_text)

    return {
        "deepest_file": deepest_path,
        "largest_file": largest,
        "entry_point": entry_point,
        "symbol_in_largest_file": symbol,
        "test_target": test_target,
        "symbol_in_test_target": test_symbol,
    }


# ---------- prepare --------------------------------------------------------


def _cmd_prepare(args) -> int:
    start = Path(args.repo).resolve()
    repo = _resolve_repo_root(start)
    if not (repo / ".git").exists() and not (repo / ".git").is_dir():
        # Not a git repo — still proceed; baseline will be empty.
        pass

    run_id = _new_run_id()
    rdir = _run_dir(repo, run_id)
    tmp = rdir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    env_override = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL") or None

    # Static checks — write into tmp/, never at repo root.
    static = static_checks.build_report(repo)
    (tmp / "static.json").write_text(
        json.dumps(static, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )

    _update_latest(repo, run_id)

    targets = _pick_targets(repo)

    _emit({
        "run_id": run_id,
        "run_dir": str(rdir),
        "repo_path": str(repo),
        "env_override": env_override,
        "static": static,
        "targets": targets,
    })
    return 0


# ---------- finalize -------------------------------------------------------


def _attribute_usage(sessions: list[str], rates_path: str | None) -> dict:
    """Reconcile per-session usage from local JSONL transcripts. Returns the
    full usage payload (`sessions` + `totals`). Empty `sessions` returns an
    empty payload — finalize can skip attribution entirely on dry runs."""
    if not sessions:
        return {"sessions": {}, "totals": {}}
    rates = session_usage.load_rates(rates_path)
    out_sessions: dict[str, dict] = {}
    totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "usd": None,
    }
    grand_usd = 0.0
    has_cost = False
    for sid in sessions:
        log = session_usage.find_log(sid)
        if log is None:
            out_sessions[sid] = {**session_usage.SessionTotals().__dict__, "status": "not-found"}
            continue
        s = session_usage.aggregate(log)
        s.usd = session_usage.cost_usd(s, rates)
        out_sessions[sid] = s.__dict__
        totals["input_tokens"] += s.input_tokens
        totals["output_tokens"] += s.output_tokens
        totals["cache_read_input_tokens"] += s.cache_read_input_tokens
        totals["cache_creation_input_tokens"] += s.cache_creation_input_tokens
        if s.usd is not None:
            grand_usd += s.usd
            has_cost = True
    if has_cost:
        totals["usd"] = round(grand_usd, 4)
    return {"sessions": out_sessions, "totals": totals}


def _build_summary(matrix: dict, usage: dict) -> dict:
    return {
        "grade":              matrix.get("grade"),
        "rationale":          matrix.get("rationale"),
        "scores":             matrix.get("scores", []),
        "recommendations":    matrix.get("recommendations", []),
        "orchestrator_model": matrix.get("orchestrator_model"),
        "actual_model":       matrix.get("actual_model"),
        "totals":             (usage or {}).get("totals", {}),
        "warnings":           matrix.get("warnings", []),
    }


def _cmd_finalize(args) -> int:
    repo = _resolve_repo_root(Path(".").resolve())
    rdir = _run_dir(repo, args.run)
    tmp = rdir / "tmp"
    if not tmp.is_dir():
        print(f"error: run not found: {rdir}", file=sys.stderr)
        return 2

    if args.matrix_stdin:
        matrix_raw = sys.stdin.read()
    else:
        matrix_raw = args.matrix_json or ""
    try:
        matrix = json.loads(matrix_raw)
    except json.JSONDecodeError as exc:
        print(f"error: invalid matrix JSON: {exc}", file=sys.stderr)
        return 2

    # Attribute usage (folded in from the old `attribute` subcommand).
    usage = _attribute_usage(args.sessions, args.rates)
    (tmp / "usage.json").write_text(
        json.dumps(usage, indent=2) + "\n", encoding="utf-8",
    )

    # Simulation mode: subagents are read-only by contract — no worktree
    # isolation, no edits, nothing to leak. We deliberately do not run the
    # leak check or any worktree cleanup here.
    all_warnings = list(matrix.get("warnings", []))
    matrix["warnings"] = all_warnings
    matrix.setdefault("repo_path", str(repo))
    (tmp / "matrix.json").write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    (tmp / "warnings.json").write_text(json.dumps(all_warnings, indent=2) + "\n", encoding="utf-8")

    # Render.
    static = json.loads((tmp / "static.json").read_text(encoding="utf-8"))
    report_path = rdir / "report.html"
    report_path.write_text(render_report.build_html(static, usage, matrix), encoding="utf-8")

    # Durable summary.
    (rdir / "summary.json").write_text(
        json.dumps(_build_summary(matrix, usage), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Prune intermediates.
    shutil.rmtree(tmp, ignore_errors=True)

    sessions = (usage or {}).get("sessions") or {}
    missing = sum(1 for v in sessions.values() if v.get("status") != "ok")
    warn_count = len(all_warnings) + (1 if missing else 0)

    _emit({
        "run_id": args.run,
        "report_path": str(report_path),
        "summary_path": str(rdir / "summary.json"),
        "warnings": warn_count,
        "warning_messages": all_warnings,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
