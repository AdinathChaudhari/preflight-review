"""Config dataclasses, strict loader, and the `init` template."""

from __future__ import annotations

import dataclasses
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


@dataclass
class RuffConfig:
    select: list[str] = field(default_factory=lambda: ["B", "S", "F", "E9", "RUF"])


@dataclass
class ToolsConfig:
    ruff: RuffConfig = field(default_factory=RuffConfig)
    gitleaks: bool = True
    ast_grep: bool = True
    osv: str | bool = "auto"
    semgrep: bool = False


@dataclass
class TriageConfig:
    haiku: bool = False


@dataclass
class IntentConfig:
    issues: bool = False


@dataclass
class NitpicksConfig:
    per_file: int = 2
    per_run: int = 5


@dataclass
class LearningsConfig:
    rerank: bool = True
    cap: int = 20


@dataclass
class VerifierConfig:
    cap: int = 15


@dataclass
class ModelsConfig:
    finder: str = "sonnet"
    finder_deep: str = "opus"
    security: str = "opus"
    verifier_major: str = "opus"
    verifier: str = "sonnet"
    triage: str = "haiku"
    rerank: str = "haiku"
    walkthrough: str = "sonnet"


@dataclass
class Config:
    version: int = 1
    base: str = "origin/main"
    profile: str = "chill"  # quiet | chill | assertive
    mode: str = "standard"  # quick | standard | deep
    backend: str = "cli"
    budget_usd: float = 1.50
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
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    lenses: list[str] = field(
        default_factory=lambda: [
            "correctness",
            "contracts_tests",
            "data_concurrency",
            "rules_compliance",
        ]
    )
    security_lane: bool = True
    walkthrough: bool = True
    triage: TriageConfig = field(default_factory=TriageConfig)
    intent: IntentConfig = field(default_factory=IntentConfig)
    nitpicks: NitpicksConfig = field(default_factory=NitpicksConfig)
    report_dir: str = ".preflight/reviews"
    concurrency: int = 5
    cache_warm_seconds: int = 20
    learnings: LearningsConfig = field(default_factory=LearningsConfig)
    verifier: VerifierConfig = field(default_factory=VerifierConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)


DEFAULT_CONFIG_YAML = """\
# preflight-review config (every key shown at its default).
version: 1
base: origin/main            # branch the review diff is taken against
profile: chill               # quiet | chill | assertive
mode: standard               # quick | standard | deep
backend: cli                 # claude -p subprocess
budget_usd: 1.5
max_rounds: 3
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
tools:
  ruff:
    select: [B, S, F, E9, RUF]
  gitleaks: true
  ast_grep: true
  osv: auto                  # auto | true | false
  semgrep: false
lenses: [correctness, contracts_tests, data_concurrency, rules_compliance]
security_lane: true
walkthrough: true
triage:
  haiku: false
intent:
  issues: false
nitpicks:
  per_file: 2
  per_run: 5
report_dir: .preflight/reviews
concurrency: 5
cache_warm_seconds: 20
learnings:
  rerank: true
  cap: 20
verifier:
  cap: 15
models:
  finder: sonnet
  finder_deep: opus
  security: opus
  verifier_major: opus
  verifier: sonnet
  triage: haiku
  rerank: haiku
  walkthrough: sonnet
"""


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
    return cfg


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


def merge_overrides(config: Config, **overrides: Any) -> Config:
    names = {f.name for f in dataclasses.fields(Config)}
    clean = {}
    for key, value in overrides.items():
        if key not in names:
            raise ConfigError(f"unknown config override: {key}")
        if value is not None:
            clean[key] = value
    return dataclasses.replace(config, **clean)
