"""Config dataclasses, strict loader, and the `init` template."""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from preflight_review import gitutil
from preflight_review.errors import ConfigError, GitError

CONFIG_PATH = ".preflight/config.yaml"


@dataclass
class GuidelinesConfig:
    discover: bool = True
    ancestors: bool = True


@dataclass
class PathInstruction:
    path: str
    instructions: str


# Ruff rules that are test noise: assert (S101), hardcoded test passwords (S105-S107),
# temp paths (S108) and non-crypto random (S311). Not reported for test paths.
DEFAULT_RUFF_TEST_IGNORE = ["S101", "S105", "S106", "S107", "S108", "S311"]
DEFAULT_TEST_PATHS = [
    "tests/**", "test/**", "**/tests/**", "**/test/**",
    "**/test_*.py", "**/*_test.py", "**/conftest.py",
]


@dataclass
class RuffConfig:
    select: list[str] = field(default_factory=lambda: ["B", "S", "F", "E9", "RUF"])
    test_ignore: list[str] = field(default_factory=lambda: list(DEFAULT_RUFF_TEST_IGNORE))
    test_paths: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_PATHS))


@dataclass
class KeyConsumersConfig:
    """Candidate signals for keys and ids in changed JSON/YAML files (tools/key_consumers.py)."""

    enabled: bool = True
    exclude: list[str] = field(default_factory=lambda: [
        ".github/**", "pyproject.toml", "package.json", "**/*.lock", "**/package-lock.json",
        "**/pnpm-lock.yaml", "**/yarn.lock", "docker-compose*", "**/fixtures/**", "**/i18n/**",
        "**/locales/**", "**/openapi*", "**/*.schema.json", "**/schemas/**",
    ])
    consumed_whole: list[str] = field(default_factory=list)  # read whole, e.g. injected into a prompt
    id_patterns: list[str] = field(default_factory=list)  # regexes for id strings to resolve


@dataclass
class ToolsConfig:
    ruff: RuffConfig = field(default_factory=RuffConfig)
    gitleaks: bool = True
    ast_grep: bool = True
    osv: str | bool = "auto"
    semgrep: bool = False
    zizmor: bool = True  # GitHub Actions workflows and actions
    actionlint: bool = False  # GitHub Actions workflows (opt-in)
    shellcheck: bool = False  # shell scripts (opt-in)
    key_consumers: KeyConsumersConfig = field(default_factory=KeyConsumersConfig)


@dataclass
class TriageConfig:
    haiku: bool = True  # a Haiku call per changed file approves trivial ones before finders


@dataclass
class IntentConfig:
    issues: bool = False


@dataclass
class NitpicksConfig:
    # null = no cap. Caps apply only under profile chill.
    per_file: int | None = None
    per_run: int | None = None


@dataclass
class LearningsConfig:
    rerank: bool = True
    cap: int = 20


@dataclass
class VerifierConfig:
    cap: int | None = None  # null = verify every candidate
    # Sonnet votes per finding (quick mode: 1). Two run first; the rest only on disagreement.
    votes: int = 3


@dataclass
class EffortConfig:
    # `claude --effort` per mode for the finder, security and verifier stages.
    # low | medium | high | xhigh | max; null = the model's Claude Code default.
    quick: str | None = "low"
    standard: str | None = "medium"
    deep: str | None = "high"


# Changed paths that make `mode: auto` run a deep review. Generic defaults: schema and
# data migrations, auth and security code, secrets and env files, CI and deploy config,
# prompts and agent instructions, and scoring, grading, billing and payment code.
DEFAULT_DEEP_PATHS = [
    "**/migrations/**", "**/alembic/**", "**/*.sql",
    "**/auth/**", "**/*auth*.py", "**/security/**", "**/permissions*",
    "**/.env*", "**/*secret*", "**/*credential*",
    ".github/workflows/**", "**/Dockerfile*", "**/k8s/**", "**/helm/**", "**/deploy/**",
    "**/*.sh",
    "**/prompts/**", "**/*.jinja2", "**/AGENTS.md", "**/CLAUDE.md", "**/SKILL.md",
    "**/*scor*.py", "**/*grad*.py", "**/billing/**", "**/payment*/**",
]


@dataclass
class AutoConfig:
    deep_paths: list[str] = field(default_factory=lambda: list(DEFAULT_DEEP_PATHS))


MIN_PACKET_TOKENS = 2000


@dataclass(frozen=True)
class PacketTokensConfig:
    """Diff tokens per packet, by depth: quick and standard work reads more per call."""

    quick: int = 32000
    standard: int = 24000
    deep: int = 16000


@dataclass
class ModelsConfig:
    # Cheap models by default; Opus only adjudicates escalated splits (decision 15).
    finder: str = "sonnet"  # bug lenses
    finder_deep: str = "sonnet"  # bug lenses in deep mode (deep differs by turns, effort, sweep)
    finder_rules: str = "sonnet"  # rules_compliance lens
    security: str = "sonnet"  # security lane
    verifier_major: str = "opus"  # unused since 1.1: every vote runs on `verifier` (kept so
    # older configs load)
    verifier: str = "sonnet"  # every verifier vote, bug or security
    adjudicator: str = "opus"  # escalated splits: critical, or security at major or above
    adjudicator_fallback: str = "opus"  # re-runs the adjudication if `adjudicator` fails
    sweep: str = "sonnet"  # deep-mode gap sweep, once per run
    triage: str = "haiku"
    rerank: str = "haiku"
    walkthrough: str = "haiku"


@dataclass
class Config:
    version: int = 1
    base: str = "origin/main"
    profile: str = "assertive"  # quiet | chill | assertive
    mode: str = "auto"  # auto | quick | standard | deep (see MODE_VALUES)
    backend: str = "cli"
    budget_usd: float | None = None  # null = no run cap and no per-call caps
    # Ask before a run estimated above this (no TTY: do not start it); null = never ask.
    confirm_above_usd: float | None = 5.0
    # Wall-clock limit per claude -p call: a Sonnet lane past 25 min is hung. A timeout fails
    # the lane and the report lists it under Not reviewed.
    call_timeout_s: int = 1500
    max_rounds: int = 3
    verify_cmd: str | None = None
    rules_file: str = ".preflight/RULES.md"
    guidelines: GuidelinesConfig = field(default_factory=GuidelinesConfig)
    path_filters: list[str] = field(
        default_factory=lambda: [
            "!**/*.lock",
            "!**/node_modules/**",
            "!**/.venv/**",
            "!**/*.min.*",
            "!**/__pycache__/**",
        ]
    )
    path_instructions: list[PathInstruction] = field(default_factory=list)
    docs_review: str = "auto"  # auto | on | off (see DOCS_REVIEW_VALUES)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    lenses: list[str] = field(
        default_factory=lambda: [
            "correctness",
            "contracts_tests",
            "data_concurrency",
            "rules_compliance",
            "deploy_ci",
        ]
    )
    # Add lenses that apply on their own: a path-gated lens (deploy_ci) whose paths changed,
    # and lenses a repo detector switches on. false = run exactly `lenses`.
    auto_lenses: bool = True
    security_lane: bool = True
    sweep: bool = True  # deep mode only: one gap-sweep call per run
    walkthrough: bool = True
    triage: TriageConfig = field(default_factory=TriageConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)
    nitpicks: NitpicksConfig = field(default_factory=NitpicksConfig)
    report_dir: str = ".preflight/reviews"
    concurrency: int = 10
    packet_tokens: PacketTokensConfig = field(default_factory=PacketTokensConfig)
    cache_warm_seconds: int = 20
    learnings: LearningsConfig = field(default_factory=LearningsConfig)
    auto: AutoConfig = field(default_factory=AutoConfig)
    verifier: VerifierConfig = field(default_factory=VerifierConfig)
    effort: EffortConfig = field(default_factory=EffortConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)


# docs_review: "auto" and "on" review docs-only diffs with a doc-consistency lens;
# "auto" also drops low-signal doc findings when docs are half or more of the changed
# lines; "off" skips model stages for docs-only diffs (the 1.0 behaviour).
DOCS_REVIEW_VALUES = ("auto", "on", "off")

# auto resolves once per run, before any model call: deep when a changed path matches
# `auto.deep_paths` (tests, eval cases, fixtures and docs never count; depth.py), else
# standard. An explicit mode always wins.
MODE_VALUES = ("auto", "quick", "standard", "deep")


DEFAULT_CONFIG_YAML = """\
# preflight-review config (every key shown at its default).
version: 1
base: origin/main            # branch the review diff is taken against
profile: assertive           # quiet | chill | assertive
mode: auto                   # auto | quick | standard | deep (auto: deep on crucial paths)
backend: cli                 # claude -p subprocess
budget_usd: null             # e.g. 5.0 caps the run (and each call); null = no cap
confirm_above_usd: 5         # above this, offer a depth menu first; no TTY: not started
call_timeout_s: 1500         # wall-clock limit per claude -p call (a lane past 25 min is hung)
max_rounds: 3                # advisory: past this round the report suggests a step back
verify_cmd: null             # e.g. ".venv/bin/python -m pytest -q -x"
rules_file: .preflight/RULES.md
guidelines:
  discover: true             # CLAUDE.md, AGENTS.md, .claude/rules
  ancestors: true            # parent directories too
path_filters:
  - "!**/*.lock"
  - "!**/node_modules/**"
  - "!**/.venv/**"
  - "!**/*.min.*"
  - "!**/__pycache__/**"
path_instructions: []        # - {path: "migrations/**", instructions: "..."}
docs_review: auto            # auto | on | off: review docs-only diffs; off skips them
tools:
  ruff:
    select: [B, S, F, E9, RUF]
    test_ignore: [S101, S105, S106, S107, S108, S311]  # not reported on test_paths
    test_paths: ["tests/**", "test/**", "**/tests/**", "**/test/**",
                 "**/test_*.py", "**/*_test.py", "**/conftest.py"]
  gitleaks: true
  ast_grep: true
  osv: auto                  # auto | true | false
  semgrep: false
  zizmor: true               # GitHub Actions workflow audit (missing = DEGRADED)
  actionlint: false          # opt-in: workflow syntax and expression checks
  shellcheck: false          # opt-in: shell script lint
  key_consumers:             # candidate signals for keys/ids in changed JSON/YAML
    enabled: true
    exclude: [".github/**", pyproject.toml, package.json, "**/*.lock", "**/package-lock.json",
              "**/pnpm-lock.yaml", "**/yarn.lock", "docker-compose*", "**/fixtures/**",
              "**/i18n/**", "**/locales/**", "**/openapi*", "**/*.schema.json", "**/schemas/**"]
    consumed_whole: []       # data files read whole (e.g. injected into a prompt): no key check
    id_patterns: []          # e.g. ['step_\\d+_\\w+']: ids that must be defined in a data file
lenses: [correctness, contracts_tests, data_concurrency, rules_compliance, deploy_ci]
auto_lenses: true            # add path-gated lenses whose paths changed, and repo-detected lenses
security_lane: true
sweep: true                  # deep mode: one fresh finder per run reports only new issues
walkthrough: true
triage:
  haiku: true                # Haiku approves trivial files before the finders
intent:
  issues: false
nitpicks:                    # caps apply under profile chill; null = no cap
  per_file: null
  per_run: null
report_dir: .preflight/reviews
concurrency: 10
packet_tokens: {quick: 32000, standard: 24000, deep: 16000}  # diff tokens per packet, by depth
cache_warm_seconds: 20
learnings:
  rerank: true
  cap: 20
auto:                        # mode auto: deep if any changed path matches, else standard
                             # (tests, eval, fixtures and docs never count)
  deep_paths: ["**/migrations/**", "**/alembic/**", "**/*.sql",
               "**/auth/**", "**/*auth*.py", "**/security/**", "**/permissions*",
               "**/.env*", "**/*secret*", "**/*credential*",
               ".github/workflows/**", "**/Dockerfile*", "**/k8s/**", "**/helm/**",
               "**/deploy/**", "**/*.sh",
               "**/prompts/**", "**/*.jinja2", "**/AGENTS.md", "**/CLAUDE.md", "**/SKILL.md",
               "**/*scor*.py", "**/*grad*.py", "**/billing/**", "**/payment*/**"]
verifier:
  cap: null                  # null = verify every candidate
  votes: 3                   # Sonnet votes: 2 first, the 3rd only on disagreement (quick: 1)
effort:                      # claude --effort for finders, security, verifier, sweep
  quick: low
  standard: medium
  deep: high                 # xhigh | max for even deeper runs
models:                      # cheap by default: Opus only adjudicates escalated splits
  finder: sonnet             # bug lenses
  finder_deep: sonnet        # deep mode differs by turns, effort and sweep, not model
  finder_rules: sonnet       # rules_compliance lens
  security: sonnet
  verifier_major: opus       # unused since 1.1 (every vote runs on verifier)
  verifier: sonnet           # every verifier vote, bug or security
  adjudicator: opus          # splits on critical (or major+ security) findings; fable works too
  adjudicator_fallback: opus # used when the adjudicator call fails
  sweep: sonnet
  triage: haiku
  rerank: haiku
  walkthrough: haiku
"""


def stage_effort(config: Config, model: str | None = None) -> str | None:
    """`--effort` for the finder, security, verifier, adjudicator and sweep stages. A Haiku
    model never gets an effort level (it is the cheap tier; high would only add cost)."""
    if model is not None and "haiku" in model.lower():
        return None
    return getattr(config.effort, config.mode, None)


def call_cap(config: Config, usd: float) -> float | None:
    """A fixed per-call USD cap, applied only when the user set a run budget."""
    return None if config.budget_usd is None else usd


def split_call_cap(config: Config, n_calls: int, floor: float) -> float | None:
    """Per-call cap for a fan-out of n_calls: an even split of the run budget, never
    below `floor` and never above the budget. None when no run budget is set."""
    if config.budget_usd is None:
        return None
    return min(config.budget_usd, max(config.budget_usd / (n_calls + 2), floor))


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _is_union(tp: Any) -> bool:
    return typing.get_origin(tp) in (typing.Union, types.UnionType)


def _convert(value: Any, tp: Any, path: str) -> Any:
    origin = typing.get_origin(tp)
    if dataclasses.is_dataclass(tp):
        return _build(tp, value, path)
    if _is_union(tp):
        options = typing.get_args(tp)
        if value is None and type(None) in options:
            return None
        errors = []
        for opt in options:
            if opt is type(None):
                continue
            try:
                return _convert(value, opt, path)
            except ConfigError as exc:
                errors.append(exc)
        raise ConfigError(f"{path}: wrong type {type(value).__name__}")
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list, got {type(value).__name__}")
        (item_tp,) = typing.get_args(tp)
        return [_convert(v, item_tp, f"{path}[{i}]") for i, v in enumerate(value)]
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected bool, got {type(value).__name__}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected int, got {type(value).__name__}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: expected number, got {type(value).__name__}")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected string, got {type(value).__name__}")
        return value
    raise ConfigError(f"{path}: unsupported type {tp!r}")  # pragma: no cover


def _build(cls: type, data: Any, path: str = "") -> Any:
    where = path or "<root>"
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    hints = _hints(cls)
    for key in data:
        if key not in hints:
            raise ConfigError(f"unknown config key: {path + '.' if path else ''}{key}")
    kwargs = {}
    for key, value in data.items():
        kwargs[key] = _convert(value, hints[key], f"{path}.{key}" if path else key)
    required = [
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    ]
    for name in required:
        if name not in kwargs:
            raise ConfigError(f"{where}: missing required key {name}")
    return cls(**kwargs)


def config_from_dict(d: dict[str, Any] | None) -> Config:
    if d is None:
        return Config()
    cfg = _build(Config, d)
    if cfg.version != 1:
        raise ConfigError(f"unsupported config version: {cfg.version}")
    if cfg.mode not in MODE_VALUES:
        raise ConfigError(f"mode: expected one of {', '.join(MODE_VALUES)}, got {cfg.mode!r}")
    if cfg.docs_review not in DOCS_REVIEW_VALUES:
        raise ConfigError(
            f"docs_review: expected one of {', '.join(DOCS_REVIEW_VALUES)}, got {cfg.docs_review!r}"
        )
    for f in dataclasses.fields(cfg.packet_tokens):
        v = getattr(cfg.packet_tokens, f.name)
        if v < MIN_PACKET_TOKENS:
            raise ConfigError(
                f"packet_tokens.{f.name}: expected an integer >= {MIN_PACKET_TOKENS}, got {v}"
            )
    compile_id_patterns(cfg.tools.key_consumers.id_patterns)
    return cfg


def compile_id_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Compile `tools.key_consumers.id_patterns`; a bad regex is a ConfigError."""
    out = []
    for i, pattern in enumerate(patterns):
        try:
            out.append(re.compile(pattern))
        except (re.error, TypeError) as exc:
            raise ConfigError(
                f"tools.key_consumers.id_patterns[{i}]: invalid regex {pattern!r}: {exc}"
            ) from exc
    return out


def load_config(repo_root: Path, base_ref: str | None, git: Any = gitutil) -> Config:
    if base_ref:
        try:
            resolved = git.resolve_ref_with_fallback(base_ref, repo_root)
        except GitError as exc:
            raise ConfigError(f"cannot resolve base ref for config; {str(exc).split('; ', 1)[-1]}") from exc
        text = git.show_file(resolved, CONFIG_PATH, repo_root)
    else:
        p = Path(repo_root) / CONFIG_PATH
        text = p.read_text() if p.exists() else None
    if text is None:
        return Config()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG_PATH}: invalid YAML: {exc}") from exc
    return config_from_dict(data)


# Keys that `init` 1.0 wrote out at a default which 1.1 changed: (dotted key, 1.0 value).
# A config written by 1.0 init pins these, so the 1.1 defaults never take effect.
LEGACY_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("profile", "chill"),
    ("mode", "standard"),
    ("budget_usd", 1.5),
    ("nitpicks.per_file", 2),
    ("nitpicks.per_run", 5),
    ("verifier.cap", 15),
)


def _dotted(obj: Any, key: str) -> Any:
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def legacy_defaults(config: Config) -> list[str]:
    """`key: old (now new)` for each key still equal to its 1.0 default where 1.1 changed it."""
    fresh = Config()
    return [
        f"{key}: {old} (1.1 default: {_dotted(fresh, key)})"
        for key, old in LEGACY_DEFAULTS
        if _dotted(config, key) == old
    ]


def legacy_defaults_note(config: Config) -> str | None:
    found = legacy_defaults(config)
    if not found:
        return None
    return (
        "config: keys still at their 1.0 defaults, so the 1.1 defaults do not apply: "
        + "; ".join(found)
        + ". Remove them from .preflight/config.yaml (or run `init --force`) if not deliberate;"
        " config is read from the base branch, so this takes effect once merged there"
    )


def worktree_config_diff(root: Path, base_ref: str) -> list[str]:
    """Sorted top-level keys whose values differ between `.preflight/config.yaml` at
    `base_ref` and the worktree copy. [] when either is missing or unparsable, or equal.
    Config is still loaded from the base ref; this only feeds a note."""
    try:
        base_text = gitutil.show_file(base_ref, CONFIG_PATH, Path(root))
        p = Path(root) / CONFIG_PATH
        work_text = p.read_text() if p.exists() else None
        if base_text is None or work_text is None:
            return []
        base, work = yaml.safe_load(base_text), yaml.safe_load(work_text)
    except (GitError, OSError, yaml.YAMLError):
        return []
    if not isinstance(base, dict) or not isinstance(work, dict):
        return []
    return sorted(k for k in set(base) | set(work) if base.get(k) != work.get(k))


def merge_overrides(config: Config, **overrides: Any) -> Config:
    names = {f.name for f in dataclasses.fields(Config)}
    clean = {}
    for key, value in overrides.items():
        if key not in names:
            raise ConfigError(f"unknown config override: {key}")
        if value is not None:
            clean[key] = value
    return dataclasses.replace(config, **clean)
