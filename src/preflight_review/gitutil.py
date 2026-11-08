"""Thin wrappers over the git CLI. All functions take the working directory."""

from __future__ import annotations

import hashlib
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


def blob_shas(cwd: Path, ref: str, paths: list[str]) -> dict[str, str | None]:
    """Git blob sha of each path at `ref` (None when absent there or not a blob).

    One `git cat-file --batch-check` subprocess for all paths (`<ref>:<path>` lines on
    stdin, so spaces and unicode are safe). A path containing a newline cannot be sent
    line-wise and is looked up with its own `rev-parse`.
    """
    out: dict[str, str | None] = {p: None for p in paths}
    batch = [p for p in dict.fromkeys(paths) if "\n" not in p]
    for p in out:
        if "\n" in p:
            proc = _run(["rev-parse", "--verify", "--quiet", f"{ref}:{p}"], cwd)
            out[p] = proc.stdout.strip() or None if proc.returncode == 0 else None
    if not batch:
        return out
    stdin = "".join(f"{ref}:{p}\n" for p in batch)
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch-check"], cwd=cwd, input=stdin.encode("utf-8"),
            capture_output=True, check=False,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise GitError(f"git cat-file --batch-check: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git cat-file --batch-check failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    lines = proc.stdout.decode("utf-8", "replace").split("\n")
    for path, line in zip(batch, lines):
        parts = line.split(" ")
        if len(parts) == 3 and parts[1] == "blob":
            out[path] = parts[0]
    return out


def worktree_blob_sha(root: Path, path: str) -> str:
    """Git's blob sha of the worktree file (equals git's for committed content); 'D' if absent."""
    try:
        data = (Path(root) / path).read_bytes()
    except OSError:
        return "D"
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
