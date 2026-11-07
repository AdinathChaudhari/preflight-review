"""Shared helpers for the deterministic tool layer."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from preflight_review.scope import FileChange, Scope

INSTALL_HINTS = {
    "ruff": "uv tool install ruff",
    "ast-grep": "brew install ast-grep",
    "gitleaks": "brew install gitleaks",
    "osv-scanner": "brew install osv-scanner",
    "zizmor": "uv tool install zizmor",
    "actionlint": "brew install actionlint",
    "shellcheck": "brew install shellcheck",
}


@dataclass
class ToolStatus:
    name: str
    installed: bool
    version: str | None
    ran: bool
    finding_count: int = 0
    note: str = ""
    duration_ms: int = 0
    # An enabled tool that was relevant to this diff but did not give a usable result
    # (missing, failed, unparseable output). The report says DEGRADED; the run is not failed.
    degraded: bool = False


def which(name: str) -> str | None:
    """Single seam for binary lookup (tests monkeypatch this)."""
    return shutil.which(name)


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command without a shell; returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def missing_status(name: str, relevant: bool = True) -> ToolStatus:
    """Status for a missing binary; degraded only when the tool had work in this diff."""
    hint = INSTALL_HINTS.get(name, f"install {name}")
    return ToolStatus(
        name=name,
        installed=False,
        version=None,
        ran=False,
        note=f"not installed — install with `{hint}`",
        degraded=relevant,
    )


def tool_version(binary: str, cwd: Path, args: tuple[str, ...] = ("--version",)) -> str | None:
    try:
        code, out, err = run([binary, *args], cwd, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out or err).strip()
    return text.splitlines()[0] if code == 0 and text else None


def scannable_files(scope: Scope, suffix: str | None = None) -> list[FileChange]:
    """Changed, non-binary, non-deleted files that still exist on disk."""
    out = []
    for f in scope.files:
        if f.is_binary or f.status == "D":
            continue
        if suffix and not f.path.endswith(suffix):
            continue
        if not (scope.repo_root / f.path).is_file():
            continue
        out.append(f)
    return out


def rel_path(filename: str, repo_root: Path) -> str:
    """Repo-relative posix path from an absolute or relative tool path."""
    p = Path(filename)
    if not p.is_absolute():
        return p.as_posix().removeprefix("./")
    for root in (repo_root, repo_root.resolve()):
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    try:
        return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def source_line(repo_root: Path, rel: str, line: int) -> str:
    try:
        lines = (repo_root / rel).read_text(errors="replace").splitlines()
        return lines[line - 1].strip() if 1 <= line <= len(lines) else ""
    except OSError:
        return ""
