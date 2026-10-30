"""`preflight-review doctor`: environment checks. Never raises."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from preflight_review import gitutil
from preflight_review.config import load_config
from preflight_review.model.backend import claude_version
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


def run_doctor(
    repo_root: Path,
    which: Callable[[str], str | None] = tools_base.which,
    claude_version_fn: Callable[[], str | None] = claude_version,
) -> list[Check]:
    repo_root = Path(repo_root)
    state: dict = {}

    def git_check() -> tuple[bool, str]:
        state["root"] = gitutil.toplevel(repo_root)
        return True, str(state["root"])

    def config_check() -> tuple[bool, str]:
        root = state.get("root", repo_root)
        state["config"] = load_config(root, None)
        if not (root / ".preflight" / "config.yaml").exists():
            return True, "not present; defaults apply"
        return True, ".preflight/config.yaml ok"

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

    def tool_check(name: str) -> Callable[[], tuple[bool, str]]:
        def inner() -> tuple[bool, str]:
            path = which(name)
            if path is None:
                return False, f"not installed (install: {INSTALL_HINTS[name]})"
            return True, tool_version(path, cwd) or "installed"

        return inner

    for name in ("ruff", "gitleaks", "ast-grep", "osv-scanner"):
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
