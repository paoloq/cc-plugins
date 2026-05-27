---
name: generate
description: Analyze a target codebase and write a small, hierarchical set of Claude Code onboarding files — a tiny always-loaded root CLAUDE.md plus per-area CLAUDE.md files placed next to the code they describe (and rare, gated deep-dive docs pointed to by path) — so future Claude Code sessions reach productive understanding in fewer tokens and locate code without grep-walking. Use when the user says "generate onboarding docs", "create agent docs", "write CLAUDE.md", "add a CLAUDE.md to this repo", "document this codebase for Claude", "onboard Claude to this project", "map this repo for Claude", "explain this codebase to Claude Code", or invokes "/docs:generate".
argument-hint: "[path] [--mode draft]"
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Generate Claude Code onboarding documentation for a target codebase. Read the code, run the signals helper, then write a small hierarchy of `CLAUDE.md` files into the target repo — a tiny always-loaded root file plus per-area files placed next to the code they describe. Write nothing else except, rarely, a gated deep-dive doc (see §"Output model").

## Inputs

- **path** — optional positional. Defaults to `.` (current working directory). Must be a git repo or have a recognizable manifest; otherwise stop and ask.
- **--mode draft** — optional flag. Forces an overwrite-prompt for any marked file (treat the run as a fresh draft even where prior generated output exists). Without it, marked files are reconciled in place per file. Decisions are per file; there is no global mode label.

## How Claude Code loads context (why hierarchy beats imports)

These mechanics drive every placement decision — internalize them before writing:

- **Ancestor chain at startup.** Every `CLAUDE.md` from the cwd up to the repo root loads at session start (root first, cwd last).
- **Nested files on demand.** When the agent reads or edits files in a subdirectory, that subdirectory's `CLAUDE.md` loads automatically — and then persists for the rest of the session. Nested files are *not* loaded at startup just because they exist.
- **`@`-imports are eager and permanent.** `@path` pulls the target into context the moment the importing file loads and keeps it all session (recursion capped at 4 hops).
- **Everything loaded is a recurring per-session cost.** There is no unloading.

So a `CLAUDE.md` *inside* an area beats `@`-importing a detail file from the root: the nested file loads only when the agent works there (automatic relevance scoping), while an import taxes every session regardless. This yields **three loading tiers** — see `references/output-spec.md`:

1. **`CLAUDE.md` content** — auto-loaded, persistent. Navigation + invariants. Keep small.
2. **`@`-import** — eager, permanent. Only for always-needed content or to split one oversized file.
3. **Pointed-to deep-dive doc** — referenced by path from a routing row, never imported. Zero ambient cost; read only when needed.

## Output model

- **Root `CLAUDE.md`** — always loaded. Target ~60 lines, hard ceiling ~120. Identity, command pointers, repo-wide routing surprises, repo-wide gotchas, and a cross-cutting `Where to start` table.
- **Per-area `CLAUDE.md`** — placed *inside* each area that earns one, so it loads on demand. Area-local routing rows and gotchas only; no repeat of repo-wide content.
- **Tier-3 deep-dive docs** — default zero. Emit only when content is non-derivable, substantial, *and* rarely needed, drawn from the canonical catalog in `references/output-spec.md` per its emit-when gate. Pointed to by a routing row by path, never `@`-imported. Repo-wide deep-dives in a dedicated `docs/` folder (reuse an existing docs home, else create `docs/`); area-specific co-located in the area. Never at the repo root.

Pointers, not restatement: commands, stack, and directory listings appear as pointers (`tests → evals/run.py`), never as dumps or reproduced trees. The highest-value content is task routing — "to do X, start in Y, then read Z, verify with W" — grounded in real signals and code reads, never fabricated. For exact file shapes, the marker, the area threshold, the deep-dive catalog, and the re-run rules, read `references/output-spec.md`.

## Hard rules

- **Read-only against source.** No `Edit` or `Write` to anything except the skill's own output files (`CLAUDE.md` files and gated tier-3 deep-dive docs). Never edit source, config, or any pre-existing file the skill does not own.
- **No project execution.** `Bash` is limited to read-only inventory commands — `ls`, `wc`, `find`, `git ls-files`, `git log -n …`, version probes (`node --version`, `python3 --version`) — and running this skill's `signals.py`. Never `npm install`, `pytest`, `make`, or any network/build command.
- **No new runtime deps.** `signals.py` is Python stdlib only.
- **Run the helper; don't re-derive.** Get co-change coupling, cross-file ripple, and area shape from `signals.py` — never re-grep them. Read stack, manifests, tree, and env vars directly (one cheap command each); the helper does not.
- **Cite, don't restate.** Use `path` for files and `path:line` for symbols. Skip what the manifest already makes obvious — link to it. Capture the *non-obvious*.
- **Mark inferred claims** with `(inferred)` per `references/output-spec.md`.
- **Pointers beat invented prose.** When the knowledge to explain *why* a module exists is missing, write a pointer to the truth, not a confident guess. The value is navigation, not narration.
- **Don't fabricate.** If no recognizable stack and no source files are found, stop and report.

## Workflow

### 1. Orient

Resolve the target path; confirm it's a git repo (or has a manifest). Read stack, manifests, tree, and env vars directly. Then run the signals helper:

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/generate/scripts/signals.py <target-path>
```

Consume its JSON: `area_shape` (with `earns_own_file`), `co_change`, `ripple`, `existing_outputs`, and `marker`. Do not re-derive what it provides.

### 2. Decide which areas earn a file

For each area, take `area_shape[area].earns_own_file` as the mechanical gate, then apply the one residual judgement the script can't: the area must be **core code**, not config dumps, vendored examples, or opt-in overlays. Default to *not* creating a nested file — a small or flat area gets a row in the root table instead. See `references/output-spec.md` for the rule.

### 3. Reconcile with what exists

Using `existing_outputs`:

- **Pre-existing onboarding** — if `preexisting_onboarding` reports an unmarked `CLAUDE.md`, an `AGENTS.md`, or onboarding at a non-default path, **STOP and ask via `AskUserQuestion`** (maintain alongside / skip) before writing. Never shadow deliberate prior work (R8b).
- **Per output file** — write fresh if absent; regenerate in place if marked **and** its signals/content materially changed (else leave it, to bound churn — a no-op re-run should produce a near-empty diff); ask before overwriting an unmarked file (`--mode draft` forces the ask).
- **Orphans** — list any previously-marked file no longer warranted (area dropped below threshold, deep-dive criterion no longer holds) and confirm via `AskUserQuestion` (remove / keep) before deleting. Never silently remove.

### 4. Investigate

The signals are the spine. Use targeted `Grep`/`Read`/`Glob` only for the residual the helper can't precompute — and spend most of the budget on the `Where to start` routing: pick real tasks and write the recipe an unfamiliar agent would need (first file → then read → verify). Add hand-found invariants to a `Gotchas` section only when observed *while* writing other sections; don't go hunting. Batch independent reads in parallel.

### 5. Compose — bottom-up

Write area files first, then the root file, so the root can point at what actually exists:

1. Per-area `<area>/CLAUDE.md` (only for areas that earned one in §2).
2. Any gated tier-3 deep-dive docs.
3. Root `CLAUDE.md` — identity, command pointers, repo-wide routing surprises, repo-wide gotchas, and the cross-cutting `Where to start` table (pointing at deep-dives by path where they exist).

Produce each file with the marker on line 1 via a single `Write` call. For exact shapes, read `references/output-spec.md`.

### 6. Write

Use `Write` once per file. Final chat output: one line per file written, skipped, or removed, then a single summary line. No preamble, no recap of contents.

## When to stop early

- **Ambiguous target.** No path, CWD not a git repo, no manifest → ask for the target path.
- **Unknown stack.** No stack and no source files → stop and report; don't fabricate.
- **Pre-existing onboarding / unmarked outputs.** Ask before writing (§3). Never silently overwrite hand-written docs.
- **Orphaned marked files.** Confirm before removing (§3).
- **Token-budget overflow.** If a file would exceed its cap, prune breadth-first (one example per concept, link out) before splitting. Never emit a file that ends mid-table.
- **Out-of-scope edits requested.** If asked to modify source, configs, CI, or other plugins, stop and confirm — outside this skill's contract.

## Additional Resources

- **`references/output-spec.md`** — file shapes (root, area, tier-3), the marker, the three loading tiers, the area-threshold rule, the canonical deep-dive catalog, inferred-vs-verified, pointers-beat-prose, and the Model A re-run rules.
