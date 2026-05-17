#!/usr/bin/env python3
"""Static repo checks for the agentic-readiness command.

Stdlib-only. Walks a repo and emits a JSON report on stdout with deterministic
top-level keys:

    {
      "repo_profile":       {...},   # size, file counts, heaviest paths
      "agent_instructions": {...},   # CLAUDE.md / AGENTS.md presence + shape
      "tests":              {...},   # detected test/lint/typecheck setup
      "hygiene":            {...}    # secrets, big binaries, gitignore signals
    }

Usage:
    python3 static_checks.py <repo_path> [--out PATH]

Token counts are heuristic (chars/4) — the LLM orchestrator reconciles them
with `ccusage` data for accurate cost figures.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Where to look for agent-instruction files. Top-of-tree is the most common
# layout, but dotfiles/templating repos (like this one) ship the files under
# `instructions/{claude,codex}/` — without checking deeper we'd incorrectly
# flag those repos as `missing-both`.
INSTRUCTION_CANDIDATES = {
    "claude_md": [
        Path("CLAUDE.md"),
        Path("instructions/claude/CLAUDE.md"),
        Path(".claude/CLAUDE.md"),
        Path("docs/CLAUDE.md"),
    ],
    "agents_md": [
        Path("AGENTS.md"),
        Path("instructions/codex/AGENTS.md"),
        Path(".codex/AGENTS.md"),
        Path("docs/AGENTS.md"),
    ],
}

CHARS_PER_TOKEN = 4
MAX_HEAVIEST = 15
BIG_BINARY_BYTES = 1 * 1024 * 1024  # 1 MB

# Provider-recommended line budgets for the per-tool instruction file.
# Anthropic best-practices guidance puts the soft ceiling for CLAUDE.md at
# ~200 lines (longer files degrade adherence); OpenAI's AGENTS.md guidance
# puts the ceiling at ~150 lines. Exceeding these flips the file to `warn`.
INSTRUCTION_LINE_LIMITS = {
    "claude_md": 200,
    "agents_md": 150,
}

# Tokens that indicate the instruction file actually documents operational
# commands (test / build / lint / typecheck / run). OpenAI's Codex docs
# explicitly say Codex is trained to run commands referenced in AGENTS.md;
# Anthropic and Google make the same point for CLAUDE.md / GEMINI.md. We
# scan code-fence / inline-code segments only, to avoid prose false hits.
INSTRUCTION_COMMAND_RE = re.compile(
    r"`[^`\n]*\b(test|tests|build|lint|typecheck|type-check|run|make|pytest|npm|pnpm|yarn|cargo|go test|mix|gradle|mvn)\b[^`\n]*`",
    re.IGNORECASE,
)

TEXT_SUFFIXES = {
    ".md", ".markdown", ".rst", ".txt",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".rb", ".php",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".sass", ".less",
    ".sql", ".graphql", ".proto",
}

EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
                "dist", "build", "target", ".next", ".nuxt", ".cache",
                ".pytest_cache", ".mypy_cache", ".tox", "vendor"}

# Files that are necessary, not optional — they're large by design (full
# resolved dependency trees) and must not be flagged as bloat. They still
# count toward `tracked_files` / `text_bytes` / `est_tokens` because they do
# contribute to read-the-whole-repo cost, but they're excluded from
# `heaviest_paths` / `heaviest_dirs` so the report doesn't surface them as
# "huge files" candidates for removal.
LOCKFILE_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "npm-shrinkwrap.json",
    "Cargo.lock",
    "go.sum",
    "gradle.lockfile", "packages.lock.json",
    "Gemfile.lock",
    "composer.lock",
    "Package.resolved",
    "poetry.lock", "uv.lock", "pdm.lock", "Pipfile.lock",
    "mix.lock", "conan.lock",
}


def _resolve_out_against_repo_root(raw: str) -> Path:
    """Resolve --out against the git toplevel; anchor relative paths to the
    user's repo even when cwd is a `.claude/worktrees/agent-*` subdir."""
    p = Path(raw)
    if p.is_absolute():
        return p
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return p.resolve()
    top_path = Path(top)
    parts = top_path.parts
    if ".claude" in parts and "worktrees" in parts:
        top_path = Path(*parts[:parts.index(".claude")])
    return (top_path / p).resolve()

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key"),
    (re.compile(r"-----BEGIN (RSA |OPENSSH |DSA |EC |PGP )?PRIVATE KEY-----"), "private-key"),
    (re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"), "slack-token"),
    (re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic-api-key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "github-token"),
]

# Secret scanning is restricted to config-like surfaces. Source files routinely
# mention key prefixes for parsing/testing (e.g. `auth_tests.rs` parsing PEM
# blocks); flagging them as secret hits is pure noise. Real leaked secrets land
# in env files, configs, and notebooks — those are what we scan.
SECRET_SCAN_SUFFIXES = {
    ".env", ".envrc", ".cfg", ".conf", ".ini", ".properties",
    ".json", ".jsonc", ".yaml", ".yml", ".toml",
    ".txt", ".md", ".markdown",
}
SECRET_SCAN_BASENAMES = {".env", ".env.local", ".env.development", ".env.production"}

TEST_SIGNALS = {
    "pytest":   ["pytest.ini", "pyproject.toml", "tox.ini", "conftest.py"],
    "unittest": ["tests/", "test/"],
    "jest":     ["jest.config.js", "jest.config.ts", "jest.config.cjs"],
    "vitest":   ["vitest.config.js", "vitest.config.ts"],
    "mocha":    [".mocharc.js", ".mocharc.json", ".mocharc.yml"],
    "go":       ["go.mod"],
    "cargo":    ["Cargo.toml"],
    "rspec":    [".rspec", "spec/"],
}

LINT_SIGNALS = {
    "ruff":       ["ruff.toml", ".ruff.toml"],
    "flake8":     [".flake8", "setup.cfg"],
    "eslint":     [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs", "eslint.config.js"],
    "biome":      ["biome.json", "biome.jsonc"],
    "prettier":   [".prettierrc", ".prettierrc.json", ".prettierrc.js", "prettier.config.js"],
    "golangci":   [".golangci.yml", ".golangci.yaml", ".golangci.toml"],
    "rustfmt":    ["rustfmt.toml", ".rustfmt.toml"],
    "shellcheck": [".shellcheckrc"],
}

TYPECHECK_SIGNALS = {
    "pyright":    ["pyrightconfig.json"],
    "mypy":       ["mypy.ini", ".mypy.ini"],
    "tsconfig":   ["tsconfig.json"],
    "flow":       [".flowconfig"],
}

# Pillar coverage. Each table is a presence probe — the point is "does the
# repo show evidence of this practice", not deep analysis. Signal breadth is
# modelled on Kodus's agent-readiness (the OSS reference) and Factory.ai's
# public pillar list. Signals derived from file *contents* (CI-integration
# scan, observability dep-manifest scan) are added by build_report below.
DEV_ENV_SIGNALS = {
    "devcontainer":     [".devcontainer/", ".devcontainer.json"],
    "docker":           ["Dockerfile", "docker-compose.yml", "compose.yml"],
    "make":             ["Makefile", "makefile", "GNUmakefile"],
    "asdf":             [".tool-versions"],
    "mise":             ["mise.toml", ".mise.toml"],
    "nvm":              [".nvmrc"],
    "pyenv":            [".python-version"],
    "rbenv":            [".ruby-version"],
    "phpenv":           [".php-version"],
    "swift-version":    [".swift-version"],
    "global-json":      ["global.json"],
    "rust-toolchain":   ["rust-toolchain.toml", "rust-toolchain"],
    "nix":              ["flake.nix", "shell.nix", "default.nix"],
    "vscode-tasks":     [".vscode/tasks.json", ".vscode/launch.json"],
    "lockfile-npm":     ["package-lock.json"],
    "lockfile-yarn":    ["yarn.lock"],
    "lockfile-pnpm":    ["pnpm-lock.yaml"],
    "lockfile-bun":     ["bun.lockb"],
    "lockfile-go":      ["go.sum"],
    "lockfile-cargo":   ["Cargo.lock"],
    "lockfile-gradle":  ["gradle.lockfile"],
    "lockfile-nuget":   ["packages.lock.json"],
    "lockfile-ruby":    ["Gemfile.lock"],
    "lockfile-composer":["composer.lock"],
    "lockfile-swift":   ["Package.resolved"],
    "lockfile-poetry":  ["poetry.lock"],
    "lockfile-uv":      ["uv.lock"],
    "lockfile-pdm":     ["pdm.lock"],
    "env-template":     [".env.example", ".env.template", ".env.sample"],
    "wrapper-maven":    ["mvnw"],
    "wrapper-gradle":   ["gradlew"],
    "setup-script":     ["script/setup", "script/bootstrap", "bin/setup", "scripts/setup.sh"],
}

OBSERVABILITY_SIGNALS = {
    "opentelemetry-config": ["otel-config.yaml", "otel-collector-config.yaml"],
    "prometheus":           ["prometheus.yml", "prometheus.yaml"],
    "grafana":              ["grafana/", "dashboards/"],
    "sentry":               ["sentry.properties", ".sentryclirc"],
    "datadog":              ["datadog.yaml", ".datadog.yaml"],
    "logging-config":       ["logging.conf", "log4j.properties", "log4j2.xml", "logback.xml"],
}

# Dependency-manifest tokens for observability/telemetry SDKs. A presence
# probe inside the manifests catches cloud-native repos that don't ship a
# config file alongside the SDK.
OBSERVABILITY_DEP_TOKENS = {
    "opentelemetry-sdk": ["opentelemetry"],
    "sentry-sdk":        ["@sentry/", "sentry-sdk", "sentry-go", "sentry-ruby", "raven-"],
    "datadog-sdk":       ["dd-trace", "ddtrace", "datadog-api-client", "datadogpy"],
    "prometheus-sdk":    ["prometheus_client", "prom-client", "prometheus-client", "micrometer"],
    "newrelic-sdk":      ["newrelic", "new-relic"],
    "elastic-apm-sdk":   ["elastic-apm", "elasticapm"],
}

OBSERVABILITY_DEP_FILES = [
    "package.json", "pyproject.toml", "requirements.txt", "Pipfile",
    "poetry.lock", "uv.lock", "Cargo.toml", "go.mod", "Gemfile",
    "composer.json", "build.gradle", "build.gradle.kts", "pom.xml",
]

SECURITY_SIGNALS = {
    "security-md":     ["SECURITY.md", "docs/SECURITY.md", ".github/SECURITY.md"],
    "codeowners":      ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"],
    "dependabot":      [".github/dependabot.yml", ".github/dependabot.yaml"],
    "renovate":        ["renovate.json", ".github/renovate.json", "renovate.json5"],
    "pre-commit":      [".pre-commit-config.yaml", ".pre-commit-config.yml"],
    "gitleaks-config": [".gitleaks.toml", ".gitleaks.yaml"],
    "trivy-config":    [".trivyignore", "trivy.yaml"],
    "license":         ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"],
}

# Substring-keyed scanner integrations found inside CI / pre-commit configs.
# The key is the canonical signal label; the values are case-insensitive
# substrings that, if present in any CI file, signal the integration. This
# catches the case where a scanner isn't installed as a top-level config but
# is wired into CI (e.g. `codeql-action` in a GitHub workflow).
SECURITY_CI_TOKENS = {
    "codeql":         ["codeql-action", "github/codeql"],
    "snyk":           ["snyk/actions", "snyk-action", "snyk.io"],
    "semgrep":        ["returntocorp/semgrep", "semgrep-action", "semgrep ci"],
    "sonarqube":      ["sonarsource/", "sonar-scanner", "sonarcloud"],
    "trivy":          ["aquasecurity/trivy", "trivy-action"],
    "gitleaks":       ["gitleaks/gitleaks", "gitleaks-action", "zricethezav/gitleaks"],
    "detect-secrets": ["yelp/detect-secrets", "detect-secrets"],
    "bandit":         ["pycqa/bandit", "bandit -r", "bandit-action"],
    "gosec":          ["securego/gosec", "gosec "],
    "brakeman":       ["presidentbeef/brakeman", "brakeman "],
    "pip-audit":      ["pypa/gh-action-pip-audit", "pip-audit"],
    "cargo-audit":    ["rustsec/audit-check", "cargo audit"],
    "dep-audit":      ["npm audit", "yarn audit", "pnpm audit"],
}

SECURITY_CI_FILES_GLOB = [".github/workflows/*.yml", ".github/workflows/*.yaml"]
SECURITY_CI_FILES_FIXED = [
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    ".gitlab-ci.yml", "azure-pipelines.yml", ".circleci/config.yml",
]


@dataclass
class RepoProfile:
    tracked_files: int = 0
    text_files: int = 0
    text_bytes: int = 0
    est_tokens: int = 0
    heaviest_paths: list[dict] = field(default_factory=list)
    heaviest_dirs: list[dict] = field(default_factory=list)


@dataclass
class AgentInstructions:
    claude_md: dict | None = None
    agents_md: dict | None = None
    mirror_parity: str = "n/a"
    at_least_one_present: bool = False


@dataclass
class Tests:
    runners: list[str] = field(default_factory=list)
    linters: list[str] = field(default_factory=list)
    typecheckers: list[str] = field(default_factory=list)
    ci_configs: list[str] = field(default_factory=list)


@dataclass
class Hygiene:
    gitignore_present: bool = False
    secret_hits: list[dict] = field(default_factory=list)
    big_binaries: list[dict] = field(default_factory=list)


@dataclass
class DevEnv:
    signals: list[str] = field(default_factory=list)


@dataclass
class Observability:
    signals: list[str] = field(default_factory=list)


@dataclass
class Security:
    signals: list[str] = field(default_factory=list)


@dataclass
class RepoShape:
    shape: str = "code"
    signals: dict = field(default_factory=dict)


@dataclass
class Evals:
    has_dir: bool = False
    files: list[dict] = field(default_factory=list)
    n_files: int = 0
    n_total_cases: int = 0


@dataclass
class SkillQuality:
    n_skills: int = 0
    skills: list[dict] = field(default_factory=list)
    skills_missing_description: list[str] = field(default_factory=list)
    skills_over_line_limit: list[str] = field(default_factory=list)


@dataclass
class PromptHygiene:
    n_md_files: int = 0
    total_lines: int = 0
    oversized: list[dict] = field(default_factory=list)


# Repo-shape detection. Agent-harness repos (plugin marketplaces, skill libs,
# prompt collections, eval suites) lack tests/dev-env/observability by design.
# Grading them against the code-repo pillar set gives a misleading D — instead
# we detect the shape and swap in Evals / Skill quality / Prompt hygiene.
SKILL_LINE_LIMIT = 200  # CLAUDE.md guidance: adherence degrades past ~200 lines.
MD_OVERSIZED_LINES = 300
HARNESS_SHAPE_THRESHOLD = 3  # ≥3 of 5 signals → agent-harness.
BUILD_MANIFEST_BASENAMES = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "build.gradle", "build.gradle.kts", "pom.xml",
    "Gemfile", "composer.json", "setup.py", "setup.cfg",
}


def detect_repo_shape(repo: Path, files: list[Path], profile: RepoProfile) -> RepoShape:
    has_plugin_manifest = False
    has_skill_md = False
    for p in files:
        name = p.name
        if name == "plugin.json" and p.parent.name == ".claude-plugin":
            has_plugin_manifest = True
        elif name == "marketplace.json" and p.parent.name == ".claude-plugin":
            has_plugin_manifest = True
        elif name == "SKILL.md":
            has_skill_md = True
        if has_plugin_manifest and has_skill_md:
            break

    has_evals_dir = (repo / "evals").is_dir()

    md_bytes = sum(safe_size(p) for p in files if p.suffix.lower() in {".md", ".markdown"})
    markdown_dominant = profile.text_bytes > 0 and md_bytes / profile.text_bytes > 0.60

    no_top_build_manifest = not any(
        (repo / b).is_file() for b in BUILD_MANIFEST_BASENAMES
    )

    signals = {
        "has_plugin_manifest":   has_plugin_manifest,
        "has_skill_md":          has_skill_md,
        "has_evals_dir":         has_evals_dir,
        "markdown_dominant":     markdown_dominant,
        "no_top_build_manifest": no_top_build_manifest,
    }
    shape = "agent-harness" if sum(signals.values()) >= HARNESS_SHAPE_THRESHOLD else "code"
    return RepoShape(shape=shape, signals=signals)


def audit_evals(repo: Path) -> Evals:
    root = repo / "evals"
    if not root.is_dir():
        return Evals()
    files: list[dict] = []
    n_cases_total = 0
    for p in root.rglob("*.json"):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        n_cases = 0
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            n_cases = len(data)
        elif isinstance(data, dict):
            for key in ("cases", "output", "outputs", "tests", "examples"):
                v = data.get(key)
                if isinstance(v, list):
                    n_cases = len(v)
                    break
        files.append({"path": str(p.relative_to(repo)), "n_cases": n_cases})
        n_cases_total += n_cases
    files.sort(key=lambda f: f["path"])
    return Evals(has_dir=True, files=files, n_files=len(files), n_total_cases=n_cases_total)


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONT_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", re.MULTILINE)


def _parse_skill_frontmatter(text: str) -> dict[str, str] | None:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    for fm in _FRONT_FIELD_RE.finditer(m.group(1)):
        fields[fm.group(1).lower()] = fm.group(2)
    return fields


def audit_skill_quality(repo: Path, files: list[Path]) -> SkillQuality:
    skills: list[dict] = []
    missing_desc: list[str] = []
    over_limit: list[str] = []
    for p in files:
        if p.name != "SKILL.md":
            continue
        rel = str(p.relative_to(repo))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line_count = text.count("\n") + (0 if text.endswith("\n") else 1)
        fm = _parse_skill_frontmatter(text)
        has_fm = fm is not None
        has_desc = bool(fm and fm.get("description"))
        is_over = line_count > SKILL_LINE_LIMIT
        skills.append({
            "path": rel,
            "has_frontmatter": has_fm,
            "has_description": has_desc,
            "line_count": line_count,
            "over_line_limit": is_over,
        })
        if not has_desc:
            missing_desc.append(rel)
        if is_over:
            over_limit.append(rel)
    skills.sort(key=lambda s: s["path"])
    return SkillQuality(
        n_skills=len(skills),
        skills=skills,
        skills_missing_description=sorted(missing_desc),
        skills_over_line_limit=sorted(over_limit),
    )


def audit_prompt_hygiene(repo: Path, files: list[Path]) -> PromptHygiene:
    n_files = 0
    total_lines = 0
    oversized: list[dict] = []
    for p in files:
        if p.suffix.lower() not in {".md", ".markdown"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n_files += 1
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        total_lines += lines
        if lines > MD_OVERSIZED_LINES:
            oversized.append({"path": str(p.relative_to(repo)), "lines": lines})
    oversized.sort(key=lambda o: -o["lines"])
    return PromptHygiene(n_md_files=n_files, total_lines=total_lines, oversized=oversized)


def list_tracked(repo: Path) -> list[Path] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [repo / p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def walk_files(repo: Path) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in files:
            out.append(Path(root) / name)
    return out


def collect_files(repo: Path) -> list[Path]:
    tracked = list_tracked(repo)
    if tracked is not None:
        return tracked
    return walk_files(repo)


def is_text_path(p: Path) -> bool:
    return p.suffix.lower() in TEXT_SUFFIXES


def safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def profile_repo(repo: Path, files: list[Path]) -> RepoProfile:
    prof = RepoProfile()
    prof.tracked_files = len(files)
    per_dir: dict[str, int] = {}
    # `paths` drives the heaviest_paths surface and intentionally excludes
    # lockfiles (necessary, not optional — see LOCKFILE_BASENAMES). Lockfiles
    # still count toward `text_bytes` / `est_tokens` because they do
    # contribute to read-the-whole-repo cost.
    paths: list[tuple[int, Path]] = []
    for f in files:
        size = safe_size(f)
        if is_text_path(f):
            prof.text_files += 1
            prof.text_bytes += size
            if f.name not in LOCKFILE_BASENAMES:
                paths.append((size, f))
                top = f.relative_to(repo).parts[0] if f != repo else "."
                per_dir[top] = per_dir.get(top, 0) + size
    prof.est_tokens = prof.text_bytes // CHARS_PER_TOKEN
    paths.sort(key=lambda t: t[0], reverse=True)
    prof.heaviest_paths = [
        {"path": p.relative_to(repo).as_posix(), "bytes": s, "est_tokens": s // CHARS_PER_TOKEN}
        for s, p in paths[:MAX_HEAVIEST]
    ]
    prof.heaviest_dirs = sorted(
        ({"dir": d, "bytes": b, "est_tokens": b // CHARS_PER_TOKEN} for d, b in per_dir.items()),
        key=lambda x: x["bytes"], reverse=True,
    )[:MAX_HEAVIEST]
    return prof


def inspect_instructions_file(p: Path, repo: Path, line_limit: int) -> dict | None:
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    headings = [ln.strip() for ln in text.splitlines() if ln.startswith("#")]
    lines = text.count("\n") + 1
    try:
        rel = p.relative_to(repo).as_posix()
    except ValueError:
        rel = p.name
    return {
        "path": rel,
        "bytes": p.stat().st_size,
        "est_tokens": p.stat().st_size // CHARS_PER_TOKEN,
        "lines": lines,
        "line_limit": line_limit,
        "over_line_limit": lines > line_limit,
        "headings": len(headings),
        "mentions_gotchas": any(w in text.lower() for w in ("gotcha", "pitfall", "footgun", "caveat")),
        "mentions_commands": bool(INSTRUCTION_COMMAND_RE.search(text)),
    }


def find_instructions(repo: Path, key: str) -> dict | None:
    limit = INSTRUCTION_LINE_LIMITS[key]
    for rel in INSTRUCTION_CANDIDATES[key]:
        info = inspect_instructions_file(repo / rel, repo, limit)
        if info is not None:
            return info
    return None


def parity_status(claude: dict | None, agents: dict | None) -> str:
    """Return the mirror-parity status.

    Having *either* CLAUDE.md or AGENTS.md present is sufficient for the
    pillar — the absence of the other is informational, not a failure. The
    "agents-only" / "claude-only" statuses signal "one is present; mirroring
    the other is encouraged but not required."
    """
    if claude is None and agents is None:
        return "missing-both"
    if claude is None:
        return "agents-only"
    if agents is None:
        return "claude-only"
    delta = abs(claude["bytes"] - agents["bytes"])
    ref = max(claude["bytes"], agents["bytes"]) or 1
    if delta / ref < 0.10:
        return "in-sync"
    return "drift"


def _marker_present(repo: Path, marker: str) -> bool:
    """True if `marker` exists at repo root or any non-excluded subdirectory.

    Monorepos commonly nest manifests (`codex-rs/Cargo.toml`,
    `frontend/package.json`); root-only detection misses them entirely.
    """
    target = repo / marker
    if marker.endswith("/"):
        dirname = marker.rstrip("/")
        if target.is_dir():
            return True
        for entry in os.walk(repo):
            dirs = entry[1]
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
            if dirname in dirs:
                return True
        return False
    if target.exists():
        return True
    basename = Path(marker).name
    for entry in os.walk(repo):
        dirs = entry[1]
        files = entry[2]
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        if basename in files:
            return True
    return False


def detect_signals(repo: Path, table: dict[str, list[str]]) -> list[str]:
    hits: list[str] = []
    for name, markers in table.items():
        for marker in markers:
            if _marker_present(repo, marker):
                hits.append(name); break
    return hits


def _read_text_safe(p: Path, limit: int = 256 * 1024) -> str:
    """Return up to `limit` bytes of decoded text. Used by the two
    content-scanning helpers below; sized to handle large lockfiles cheaply."""
    try:
        with p.open("rb") as fh:
            data = fh.read(limit)
        return data.decode("utf-8", "replace")
    except OSError:
        return ""


def scan_security_ci_tokens(repo: Path) -> list[str]:
    """Scan CI / pre-commit configs for known scanner integrations. Returns
    the canonical signal labels that matched, sorted. Substring match is
    deliberate — workflow YAML embeds action refs like `aquasecurity/trivy@v1`,
    which a structural parser would have to handle case-by-case."""
    files: list[Path] = []
    for pattern in SECURITY_CI_FILES_GLOB:
        files.extend((repo).glob(pattern))
    for name in SECURITY_CI_FILES_FIXED:
        p = repo / name
        if p.exists():
            files.append(p)
    if not files:
        return []
    hits: set[str] = set()
    for f in files:
        low = _read_text_safe(f).lower()
        if not low:
            continue
        for label, tokens in SECURITY_CI_TOKENS.items():
            if any(tok.lower() in low for tok in tokens):
                hits.add(f"ci-{label}")
    return sorted(hits)


def scan_observability_deps(repo: Path) -> list[str]:
    """Scan dependency manifests for telemetry/observability SDK names. The
    most reliable observability signal for cloud-native repos that ship no
    standalone config file alongside the SDK."""
    hits: set[str] = set()
    for name in OBSERVABILITY_DEP_FILES:
        p = repo / name
        if not p.exists():
            continue
        low = _read_text_safe(p).lower()
        if not low:
            continue
        for label, tokens in OBSERVABILITY_DEP_TOKENS.items():
            if any(t.lower() in low for t in tokens):
                hits.add(label)
    return sorted(hits)


def detect_ci(repo: Path) -> list[str]:
    out: list[str] = []
    gha = repo / ".github" / "workflows"
    if gha.is_dir() and any(gha.iterdir()):
        out.append("github-actions")
    for fname in ("circle.yml", ".circleci/config.yml", ".gitlab-ci.yml",
                  ".travis.yml", "azure-pipelines.yml", "Jenkinsfile"):
        if (repo / fname).exists():
            out.append(fname.split("/")[-1])
    return out


def _is_secret_scan_target(p: Path) -> bool:
    if p.name in SECRET_SCAN_BASENAMES:
        return True
    return p.suffix.lower() in SECRET_SCAN_SUFFIXES


def scan_secrets(repo: Path, files: list[Path]) -> list[dict]:
    hits: list[dict] = []
    for f in files:
        if not _is_secret_scan_target(f) or safe_size(f) > 2 * 1024 * 1024:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat, label in SECRET_PATTERNS:
            if pat.search(text):
                hits.append({"path": f.relative_to(repo).as_posix(), "kind": label})
                break
        if len(hits) >= 20:
            break
    return hits


def find_big_binaries(repo: Path, files: list[Path]) -> list[dict]:
    out: list[dict] = []
    for f in files:
        if is_text_path(f):
            continue
        if f.name in LOCKFILE_BASENAMES:
            # Binary lockfiles (e.g. bun.lockb) are necessary, not bloat.
            continue
        size = safe_size(f)
        if size >= BIG_BINARY_BYTES:
            out.append({"path": f.relative_to(repo).as_posix(), "bytes": size})
    out.sort(key=lambda x: x["bytes"], reverse=True)
    return out[:MAX_HEAVIEST]


def build_report(repo: Path) -> dict:
    files = collect_files(repo)
    profile = profile_repo(repo, files)
    claude = find_instructions(repo, "claude_md")
    agents = find_instructions(repo, "agents_md")
    instructions = AgentInstructions(
        claude_md=claude,
        agents_md=agents,
        mirror_parity=parity_status(claude, agents),
        at_least_one_present=(claude is not None or agents is not None),
    )
    tests = Tests(
        runners=detect_signals(repo, TEST_SIGNALS),
        linters=detect_signals(repo, LINT_SIGNALS),
        typecheckers=detect_signals(repo, TYPECHECK_SIGNALS),
        ci_configs=detect_ci(repo),
    )
    hygiene = Hygiene(
        gitignore_present=(repo / ".gitignore").exists(),
        secret_hits=scan_secrets(repo, files),
        big_binaries=find_big_binaries(repo, files),
    )
    dev_env = DevEnv(signals=detect_signals(repo, DEV_ENV_SIGNALS))
    observability = Observability(
        signals=sorted(set(detect_signals(repo, OBSERVABILITY_SIGNALS))
                       | set(scan_observability_deps(repo))),
    )
    security = Security(
        signals=sorted(set(detect_signals(repo, SECURITY_SIGNALS))
                       | set(scan_security_ci_tokens(repo))),
    )
    repo_shape = detect_repo_shape(repo, files, profile)
    evals = audit_evals(repo)
    skill_quality = audit_skill_quality(repo, files)
    prompt_hygiene = audit_prompt_hygiene(repo, files)
    return {
        "repo_profile":       asdict(profile),
        "repo_shape":         asdict(repo_shape),
        "agent_instructions": asdict(instructions),
        "tests":              asdict(tests),
        "hygiene":            asdict(hygiene),
        "dev_env":            asdict(dev_env),
        "observability":      asdict(observability),
        "security":           asdict(security),
        "evals":              asdict(evals),
        "skill_quality":      asdict(skill_quality),
        "prompt_hygiene":     asdict(prompt_hygiene),
    }


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_path")
    ap.add_argument("--out", help="Write JSON to this path instead of stdout")
    args = ap.parse_args(argv[1:])
    repo = Path(args.repo_path).resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2
    report = build_report(repo)
    if args.out:
        out_path = _resolve_out_against_repo_root(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(out_path)
    else:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
