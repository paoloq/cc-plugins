# repo

Tools for managing and assessing a repository.

## Skills

### `audit` — `/repo:audit`

Assesses a repository's readiness for agentic coding. Runs static checks, dispatches five benchmark subagents on the orchestrator's model, reconciles exact token usage from local session logs, and renders a self-contained HTML scorecard under `./.repo-audit/`.

**Triggers:** "agentic readiness", "assess agentic readiness", "check this repo for agent readiness", or `/repo:audit`.

**Output:** `./.repo-audit/runs/<iso-ts>/report.html` (open in a browser) plus a `latest` symlink and a diffable `summary.json`.

## Install

Add this repo as a plugin marketplace, then enable `repo`. The bundled `.claude-plugin/settings.json` pre-approves the script's Bash prefix and the read paths needed for session-log attribution, so no permission prompts on first run.
