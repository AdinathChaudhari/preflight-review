"""Install / uninstall the report-only pre-push git hook."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from preflight_review.errors import PreflightError

HOOK_MARKER = "# preflight-review pre-push hook v1"
CHAINED_NAME = "pre-push.pre-preflight"


@dataclass
class HookResult:
    path: Path
    message: str


def _git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def hooks_dir(repo_root: Path) -> Path:
    repo_root = Path(repo_root)
    toplevel = _git(repo_root, "rev-parse", "--show-toplevel")
    if not toplevel:
        raise PreflightError(f"not a git repository: {repo_root}")
    configured = _git(repo_root, "config", "--get", "core.hooksPath")
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_absolute() else Path(toplevel) / p
    path = _git(repo_root, "rev-parse", "--git-path", "hooks")
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def _has_marker(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    except OSError:
        return False
    return any(line.strip() == HOOK_MARKER for line in head)


def install_hook(repo_root: Path, force: bool = False) -> HookResult:
    hdir = hooks_dir(repo_root)
    hdir.mkdir(parents=True, exist_ok=True)
    target = hdir / "pre-push"
    chained = hdir / CHAINED_NAME
    note = ""
    if target.exists() and not _has_marker(target):
        if chained.exists() and not force:
            raise PreflightError(
                f"{chained} already exists; refusing to overwrite (use --force)"
            )
        target.replace(chained)
        note = f"; existing hook moved to {chained.name} and will run first"
        verb = "installed"
    else:
        verb = "upgraded" if target.exists() else "installed"
    script = resources.files("preflight_review").joinpath("hooks", "pre-push.sh")
    target.write_bytes(script.read_bytes())
    target.chmod(0o755)
    return HookResult(target, f"{verb} pre-push hook at {target}{note}")


def uninstall_hook(repo_root: Path) -> str:
    hdir = hooks_dir(repo_root)
    target = hdir / "pre-push"
    chained = hdir / CHAINED_NAME
    if not target.exists():
        return "nothing to uninstall: no pre-push hook found"
    if not _has_marker(target):
        raise PreflightError(f"{target} is not a preflight-review hook; leaving it alone")
    target.unlink()
    if chained.exists():
        chained.replace(target)
        return f"removed preflight-review hook; restored previous hook at {target}"
    return f"removed preflight-review hook at {target}"
