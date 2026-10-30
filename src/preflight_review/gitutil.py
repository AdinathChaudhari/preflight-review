"""Thin wrappers over the git CLI. All functions take the working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path

from preflight_review.errors import GitError


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from exc


def run_git(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = _run(args, cwd)
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def toplevel(cwd: Path) -> Path:
    return Path(run_git(["rev-parse", "--show-toplevel"], cwd).strip())


def current_branch(cwd: Path) -> str | None:
    name = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    return None if name == "HEAD" else name


def try_rev_parse(ref: str, cwd: Path) -> str | None:
    proc = _run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def ref_candidates(ref: str) -> list[str]:
    """`origin/<b>` falls back to `<b>`, and `<b>` falls back to `origin/<b>`."""
    other = ref[len("origin/"):] if ref.startswith("origin/") else f"origin/{ref}"
    return [ref, other]


def resolve_ref_with_fallback(ref: str, cwd: Path) -> str:
    tried = ref_candidates(ref)
    for cand in tried:
        if try_rev_parse(cand, cwd):
            return cand
    raise GitError(f"cannot resolve ref; tried: {', '.join(tried)}")


def rev_parse(ref: str, cwd: Path) -> str:
    sha = try_rev_parse(ref, cwd)
    if sha is None:
        raise GitError(f"git rev-parse {ref}: unknown ref")
    return sha


def merge_base(base_ref: str, cwd: Path, head: str = "HEAD") -> str:
    return run_git(["merge-base", base_ref, head], cwd).strip()


def show_file(ref: str, path: str, cwd: Path) -> str | None:
    rev_parse(ref, cwd)  # unknown ref must raise
    proc = _run(["show", f"{ref}:{path}"], cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout


def ls_files(cwd: Path, globs: list[str] | None = None) -> list[str]:
    args = ["ls-files", "-z"]
    if globs:
        args += ["--", *globs]
    return [p for p in run_git(args, cwd).split("\0") if p]


def is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool:
    proc = _run(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(
        f"git merge-base --is-ancestor failed ({proc.returncode}): {proc.stderr.strip()}"
    )
