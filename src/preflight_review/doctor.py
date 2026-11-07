"""`preflight-review doctor`: environment checks. Never raises."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from preflight_review import gitutil
from preflight_review.config import (
    CONFIG_PATH,
    Config,
    legacy_defaults_note,
    load_config,
)
from preflight_review.errors import GitError
from preflight_review.model.backend import ModelRequest, claude_version
from preflight_review.tools import base as tools_base
from preflight_review.tools.base import INSTALL_HINTS, tool_version


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool


def _guard(name: str, required: bool, fn: Callable[[], tuple[bool, str]]) -> Check:
    try:
        ok, detail = fn()
    except Exception as exc:  # doctor never raises
        return Check(name, False, str(exc) or type(exc).__name__, required)
    return Check(name, ok, detail, required)


PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def probe_model(alias: str, cwd: Path) -> tuple[bool, str]:
    """One tiny `claude -p --model <alias>` call: does the alias resolve and answer here?

    `claude --help` listing an alias does not mean this account or provider can call it.
    """
    from preflight_review.model.cli_backend import CliBackend

    res = CliBackend(retries=0, timeout_s=120).run(ModelRequest(
        role="doctor", model=alias, system_prompt="You answer environment health checks.",
        prompt='Return {"ok": true}.', schema=PROBE_SCHEMA, cwd=cwd, tools=[], max_turns=2,
    ))
    if res.ok:
        return True, f"{alias} -> {res.model or alias}"
    return False, (res.error or res.subtype)[:200]


def review_config(root: Path) -> tuple[Config, str]:
    """The config `review` uses, read from the base ref the same way, and a detail line.

    When the base ref has no config file (or cannot be resolved), the working-tree file is
    checked instead and the detail says that review will not use it until it is merged.
    """
    base, where = Config().base, ""
    for attempt in range(2):  # the base branch's config may name another base, as in review
        try:
            resolved = gitutil.resolve_ref_with_fallback(base, root)
        except GitError:
            where = f"cannot resolve base {base}; checked the working-tree file"
            break
        if gitutil.show_file(resolved, CONFIG_PATH, root) is None:
            where = (f"not on {resolved}, so review uses defaults until it is on the base "
                     "branch; checked the working-tree file")
            break
        cfg = load_config(root, base)
        if cfg.base == base or attempt:
            return cfg, f"{CONFIG_PATH} ok (read from {resolved}, as review reads it)"
        base = cfg.base
    cfg = load_config(root, None)
    if not (root / CONFIG_PATH).exists():
        return cfg, "not present; defaults apply"
    return cfg, f"{CONFIG_PATH} {where}"


def run_doctor(
    repo_root: Path,
    which: Callable[[str], str | None] = tools_base.which,
    claude_version_fn: Callable[[], str | None] = claude_version,
    model_probe_fn: Callable[[str, Path], tuple[bool, str]] | None = None,
) -> list[Check]:
    """`model_probe_fn` (e.g. probe_model) makes one real model call to check that the
    adjudicator alias answers; None skips that check."""
    repo_root = Path(repo_root)
    state: dict = {}

    def git_check() -> tuple[bool, str]:
        state["root"] = gitutil.toplevel(repo_root)
        return True, str(state["root"])

    def config_check() -> tuple[bool, str]:
        state["config"], detail = review_config(state.get("root", repo_root))
        return True, detail

    def base_check() -> tuple[bool, str]:
        root = state["root"]
        base = state["config"].base
        resolved = gitutil.resolve_ref_with_fallback(base, root)
        mb = gitutil.merge_base(resolved, root)
        return True, f"{base} -> {resolved} (merge-base {mb[:8]})"

    def claude_check() -> tuple[bool, str]:
        v = claude_version_fn()
        if v is None:
            return False, "claude CLI not found or not working"
        return True, v

    checks = [
        _guard("git", True, git_check),
        _guard("config", True, config_check),
    ]
    if "root" in state and "config" in state:
        checks.append(_guard("base", True, base_check))
    else:
        checks.append(Check("base", False, "skipped: git or config check failed", True))
    checks.append(_guard("claude", True, claude_check))

    cwd = state.get("root", repo_root)

    cfg = state.get("config")
    if model_probe_fn is not None and cfg is not None and checks[-1].ok:
        alias = cfg.models.adjudicator
        fallback = cfg.models.adjudicator_fallback

        def adjudicator_check() -> tuple[bool, str]:
            ok, detail = model_probe_fn(alias, cwd)
            if ok or alias == fallback:
                return ok, detail
            return False, f"{alias} unavailable ({detail}); split votes adjudicate on {fallback}"

        checks.append(_guard("adjudicator model", False, adjudicator_check))

    if cfg is not None:
        legacy = legacy_defaults_note(cfg)
        checks.append(Check("config defaults", legacy is None, legacy or "no 1.0 defaults pinned", False))

    def tool_check(name: str) -> Callable[[], tuple[bool, str]]:
        def inner() -> tuple[bool, str]:
            path = which(name)
            if path is None:
                return False, f"not installed (install: {INSTALL_HINTS[name]})"
            return True, tool_version(path, cwd) or "installed"

        return inner

    for name in (
        "ruff", "gitleaks", "ast-grep", "osv-scanner", "zizmor", "actionlint", "shellcheck",
    ):
        checks.append(_guard(name, False, tool_check(name)))

    def gh_check() -> tuple[bool, str]:
        path = which("gh")
        if path is None:
            return False, "not installed (used for PR number)"
        return True, tool_version(path, cwd) or "installed (used for PR number)"

    checks.append(_guard("gh", False, gh_check))
    return checks


def doctor_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        tag = "[ok]" if c.ok else ("[FAIL]" if c.required else "[--]")
        lines.append(f"{tag} {c.name}: {c.detail}")
    return "\n".join(lines)
