---
name: audit
description: Perform a read-only assessment of a codebase (whole repo or a specified subfolder) and produce a self-contained HTML report listing prioritized pain points — bugs, security issues, anti-patterns, complexity hotspots, refactoring opportunities, dead or duplicated code, missing tests, dependency smells, performance concerns, and documentation gaps. Use when the user says "audit this code", "audit this codebase", "find pain points", "what's wrong with this code", "code smells", or invokes "/code:audit".
argument-hint: "[path] [--focus <category>] [--out <file>]"
allowed-tools: Read, Grep, Glob, Bash, Write
---

You are performing a **read-only code audit** of a target path. You write nothing to the codebase. The only file you produce is the **HTML report** rendered by this skill's script.

## Inputs

- **path** — optional positional. Defaults to `.` (current working directory). May be a repo root or a subfolder.
- **--focus <category>** — optional. One of: `bugs`, `security`, `anti-patterns`, `complexity`, `refactor`, `dead-code`, `tests`, `dependencies`, `performance`, `docs`. When set, deprioritize other categories.
- **--out <file>** — optional. Output HTML path. Defaults to `./.code-audit.html`.

## Hard rules

- **Read-only.** No `Edit` and no `Write` to files in the target path. The only `Write` allowed is the report file when the renderer can't be used (fallback path below).
- **No execution of project code.** `Bash` is for `ls`, `wc`, `find`, `git log`, `git ls-files`, language version probes (`node --version`, etc.) and the renderer script — never `npm install`, `pytest`, `make`, build commands, or anything with side effects.
- **Cite every finding** with `path:line` (or `path:line-range`). Findings without a citation are not allowed.
- **No speculation.** If you can't verify a claim by reading the code, drop it or mark it `(unverified)` in the `what` field.
- **HTML is the only durable artifact.** Do not also paste the report into chat. Print only the final summary line (see end).

## Workflow

### 1. Orient (one batched pass)

Run in parallel:

- Confirm target path exists.
- Detect stack: look for `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`, `Gemfile`, `composer.json`, etc.
- Size sense: `git ls-files <path> | wc -l` (or `find <path> -type f | wc -l` if not a git repo).
- Top-level listing and likely entry points / test directories.

Do not narrate. One batched set of tool calls.

### 2. Plan the sweep

Pick which categories below apply (e.g. skip `dependencies` if no manifest). If `--focus` is set, lead with that category plus anything trivially co-detectable.

### 3. Investigate by category

For each applicable category run targeted `Grep`/`Read` passes. Batch independent searches in parallel. Stop a category once you have enough concrete findings or have ruled it out.

**Bugs / correctness** — off-by-one risks, null/undefined dereferences, swallowed errors (`except:` / `catch {}` with no handling), unawaited promises, race conditions, mutable default args, comparison bugs (`==` vs `===`, `is` for strings), resource leaks (unclosed files/connections).

**Security** — hardcoded secrets, SQL/command injection, unsafe deserialization, missing input validation at trust boundaries, weak crypto, permissive CORS, `eval`/`exec` on untrusted input, outdated auth patterns.

**Anti-patterns** — god classes/files, deeply nested conditionals, primitive obsession, feature envy, magic numbers, copy-paste blocks, comments that recap code, defensive scaffolding around impossible cases, dict/tuple returns where a struct fits, cross-module private access.

**Complexity hotspots** — functions > ~60 lines, cyclomatic spikes, files > ~500 lines, deep / circular imports.

**Refactoring opportunities** — duplicated logic across files, near-duplicate functions, parallel hierarchies, shotgun-surgery patterns, missing abstractions (same 5-line block in 6 places).

**Dead / duplicated code** — unused exports, unreferenced files, commented-out blocks, stale `TODO`/`FIXME`/`XXX`, dead feature-flag branches.

**Tests** — missing tests for critical modules, tests that assert nothing, heavy mocking of owned code, no integration tests for I/O boundaries.

**Dependencies** — pinned-but-stale majors, duplicates (e.g. `lodash` + `ramda`), tiny one-liner deps, deps imported but missing from manifest, manifest deps not imported.

**Performance** — N+1 patterns, sync I/O on async paths, unnecessary deep copies, repeated work in hot paths, regex compiled inside loops, unbounded in-memory growth.

**Documentation** — public APIs without docstrings, README that doesn't match current entry points, stale config examples, missing `CONTRIBUTING`/`SECURITY` for shared repos.

### 4. Score & prioritize

For each finding assign:

- **severity** — `critical` (likely to bite users now), `high` (clear correctness or design defect), `medium` (smell / maintainability), `low` (nit).
- **effort** — `S` (single-file, < 1h), `M` (multi-file, < 1d), `L` (cross-cutting / design change).

Stable IDs: `F1`, `F2`, …, ordered by `severity` desc then `effort` asc.

### 5. Build the findings JSON

Assemble a single JSON document with this exact shape (omit empty fields rather than emitting null):

```json
{
  "target": "<the resolved path string>",
  "stack": "<short label like 'Python · pytest · ruff'>",
  "files_scanned": 142,
  "summary": "<one-paragraph overall impression — neutral, evidence-based>",
  "recommendations": [
    "<one-line action — start with a verb>",
    "..."
  ],
  "findings": [
    {
      "id": "F1",
      "title": "<short, specific>",
      "category": "bugs",
      "severity": "high",
      "effort": "S",
      "where": "path/to/file.py:42-58",
      "what": "<concrete description grounded in the code>",
      "why":  "<impact, one sentence>",
      "fix":  "<suggested fix, one or two sentences>"
    }
  ]
}
```

Rules:

- `category` must be one of: `bugs`, `security`, `anti-patterns`, `complexity`, `refactor`, `dead-code`, `tests`, `dependencies`, `performance`, `docs`.
- Skip categories with zero findings — do not emit empty placeholders.
- 3–7 recommendations max. Each one a single imperative line, no bullets, no markdown.
- `summary` is plain text, no markdown, no closing pleasantries.

### 6. Render the HTML report

Pipe the JSON into the renderer in a single `Bash` call:

```
cat <<'JSON' | ${CLAUDE_PLUGIN_ROOT}/skills/audit/scripts/render_report.py --out <out-path>
{ ...the JSON document... }
JSON
```

- `<out-path>` is the user's `--out` value or `./.code-audit.html` by default.
- The script prints the resolved output path on success. Capture it.
- The script has the executable bit set and uses `#!/usr/bin/env python3` — Claude Code's "Always allow" attaches to this exact path, not to `python3 *`.

### 7. Final chat output

Emit exactly two lines and nothing else. Status lines are emoji-prefixed, capitalized, and describe the activity generically — never name a specific guide id, provider, or model:

```
🧾  Report ready: <resolved output path>
🚦  <critical>C / <high>H / <medium>M / <low>L  ·  <total> findings
```

No preamble, no recap of findings, no closing remark.

## When to stop early

- Target path does not exist → say so in one line and stop. Do not render an empty report.
- Path is empty or contains < 5 source files → say so in one line and stop.
- Renderer script fails (non-zero exit, missing Python) → report the error in one line and stop. Do not write a partial HTML report by hand.
