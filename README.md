# 🧩 cc-plugins

A plugin marketplace for [Claude Code](https://docs.claude.com/en/docs/claude-code) — shipped as a single git repo.

## 🚀 Install

```text
/plugins marketplace add paoloq/cc-plugins
```

Or from a local clone:

```text
/plugins marketplace add /absolute/path/to/cc-plugins
```

Then install any plugin below with its listed command. Skills are invoked as `/<plugin>:<skill>` — e.g. `/prompt:draft`, `/prompt:review`.

## 📦 Plugins

| Name | Skills | Install | Description |
| --- | --- | --- | --- |
| [✍️ prompt](plugins/prompt/) | `draft` · `guides` · `review` · `revise` | `/plugins install prompt@cc-plugins` | Draft, revise, and review prompts using curated guides from Anthropic, OpenAI, and Google. |
| [📝 task](plugins/task/) | `draft-spec` · `plan-council` | `/plugins install task@cc-plugins` | Plan a coding task: draft a spec, then deliberate the implementation plan with Codex before handing off via `ExitPlanMode`. |
| [🛠️ repo](plugins/repo/) | `audit` | `/plugins install repo@cc-plugins` | Assess a repo's agentic readiness via static checks plus benchmark subagents; renders a navigable HTML scorecard. |
| [🔬 code](plugins/code/) | `audit` | `/plugins install code@cc-plugins` | Tools for assessing and improving source code. |

## 🧪 Evals

```
python3 evals/run.py all
```

To run them automatically before every commit (one-time, per clone):

```
git config core.hooksPath .githooks
```

Bypass with `git commit --no-verify` or `PRE_COMMIT_SKIP_EVALS=1`.

## 📄 License

MIT — see [LICENSE](LICENSE).
