"""Review scope: base resolution, diff parsing, path filters, diff hash."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from preflight_review.errors import GitError, ScopeError
from preflight_review.gitutil import (
    resolve_ref_with_fallback,
    current_branch,
    is_ancestor,
    merge_base,
    run_git,
    show_file,
    toplevel,
    try_rev_parse,
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_FLAGS = ["-c", "core.quotePath=false", "diff", "--no-color", "--find-renames"]


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)
    added_ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    old_path: str | None
    status: str  # "A" | "M" | "D" | "R"
    is_binary: bool
    hunks: list[Hunk]
    added_lines: int
    deleted_lines: int
    new_text: str | None
    old_text: str | None


@dataclass
class Scope:
    repo_root: Path
    repo_name: str
    branch: str | None
    base_ref: str
    base_sha: str
    head_sha: str
    files: list[FileChange]
    filtered_out: list[str]
    untracked: list[str]
    diff_hash: str
    include_worktree: bool
    diff_from: str = ""
    incremental: bool = False


# --------------------------------------------------------------------- filters


def glob_to_regex(pattern: str) -> re.Pattern:
    """Gitignore-style glob: `**` any depth (incl. zero dirs), `*` no `/`, `?` one char.

    A pattern without `/` matches at any depth; a leading `/` anchors it; a
    trailing `/` matches everything below that directory. A match on a
    directory also matches the files beneath it.
    """
    pat = pattern
    anchored = pat.startswith("/") or "/" in pat.rstrip("/")
    pat = pat.lstrip("/")
    if pat.endswith("/"):
        pat += "**"
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"^{prefix}{''.join(out)}(?:/.*)?$")


def matches_path_filters(path: str, filters: list[str]) -> bool:
    """True = keep. `!x` entries exclude; other entries are include-only."""
    excludes = [f[1:] for f in filters if f.startswith("!")]
    includes = [f for f in filters if not f.startswith("!") and f]
    if any(glob_to_regex(p).match(path) for p in excludes):
        return False
    if includes and not any(glob_to_regex(p).match(path) for p in includes):
        return False
    return True


# --------------------------------------------------------------- diff parsing


def _unquote_path(raw: str) -> str:
    return raw.rstrip("\t")


def _header_path(line: str) -> str | None:
    """Path from a `--- a/x` or `+++ b/x` line; None for /dev/null."""
    raw = _unquote_path(line[4:])
    if raw == "/dev/null":
        return None
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def _git_header_paths(line: str) -> tuple[str, str]:
    """Best-effort paths from `diff --git a/x b/y` (exact when x == y)."""
    rest = line[len("diff --git "):]
    n = len(rest) - 5
    if n >= 0 and n % 2 == 0:
        p = rest[2 : 2 + n // 2]
        if rest == f"a/{p} b/{p}":
            return p, p
    a, _, b = rest.partition(" b/")
    return a[2:] if a.startswith("a/") else a, b


def _parse_diff(text: str) -> list[dict]:
    """Parse unified diff text into raw per-file records."""
    files: list[dict] = []
    cur: dict | None = None
    hunk: Hunk | None = None
    old_left = new_left = 0
    new_no = 0
    run_start: int | None = None

    def close_run() -> None:
        nonlocal run_start
        if run_start is not None and hunk is not None:
            hunk.added_ranges.append((run_start, new_no - 1))
        run_start = None

    for line in text.split("\n"):
        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith("\\"):
                continue
            tag = line[:1] or " "
            if tag == "+":
                if run_start is None:
                    run_start = new_no
                new_no += 1
                new_left -= 1
                cur["added"] += 1
            elif tag == "-":
                close_run()
                old_left -= 1
                cur["deleted"] += 1
            else:
                close_run()
                tag = " "
                old_left -= 1
                new_left -= 1
                new_no += 1
            hunk.lines.append(tag + line[1:])
            if old_left <= 0 and new_left <= 0:
                close_run()
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            a, b = _git_header_paths(line)
            cur = {
                "path": b, "old_path": a, "status": "M", "binary": False,
                "hunks": [], "added": 0, "deleted": 0,
            }
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_len = 1 if m[2] is None else int(m[2])
            new_len = 1 if m[4] is None else int(m[4])
            hunk = Hunk(int(m[1]), old_len, int(m[3]), new_len)
            cur["hunks"].append(hunk)
            old_left, new_left, new_no = old_len, new_len, int(m[3])
            run_start = None
            continue
        if line.startswith("new file mode"):
            cur["status"] = "A"
        elif line.startswith("deleted file mode"):
            cur["status"] = "D"
        elif line.startswith("rename from "):
            cur["status"] = "R"
            cur["old_path"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur["path"] = line[len("rename to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("--- "):
            p = _header_path(line)
            if p is not None:
                cur["old_path"] = p
        elif line.startswith("+++ "):
            p = _header_path(line)
            if p is not None:
                cur["path"] = p
            elif cur["status"] == "D":
                cur["path"] = cur["old_path"]
    return files


def _untracked_hunk(text: str) -> Hunk | None:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return None
    n = len(lines)
    return Hunk(0, 0, 1, n, ["+" + ln for ln in lines], [(1, n)])


def _read_worktree(repo: Path, path: str) -> bytes | None:
    try:
        return (repo / path).read_bytes()
    except (OSError, IsADirectoryError):
        return None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ resolution


def _resolve_base(repo_root: Path, base_ref: str) -> tuple[str, str]:
    try:
        ref = resolve_ref_with_fallback(base_ref, repo_root)
    except GitError as exc:
        raise ScopeError(f"cannot resolve base ref; {str(exc).split('; ', 1)[-1]}") from exc
    return ref, ref


def resolve_scope(
    repo_root: Path,
    base_ref: str,
    include_worktree: bool = True,
    since_sha: str | None = None,
    path_filters: list[str] | None = None,
) -> Scope:
    repo_root = Path(repo_root)
    filters = path_filters or []
    try:
        root = toplevel(repo_root)
        resolved, _ = _resolve_base(root, base_ref)
        head_sha = try_rev_parse("HEAD", root)
        if head_sha is None:
            raise ScopeError("repository has no commits (HEAD is unborn)")
        try:
            base_sha = merge_base(resolved, root, "HEAD")
        except GitError as exc:
            raise ScopeError(f"no merge-base between {resolved} and HEAD: {exc}") from exc
        branch = current_branch(root)

        incremental = False
        diff_from = base_sha
        if since_sha:
            try:
                ok = is_ancestor(since_sha, "HEAD", root)
            except GitError:
                ok = False
            if ok:
                incremental = True
                diff_from = since_sha

        end = [] if include_worktree else ["HEAD"]
        raw = _parse_diff(run_git([*_DIFF_FLAGS, "-U3", diff_from, *end], root))
        hash_raw = _parse_diff(
            run_git([*_DIFF_FLAGS, "--ignore-space-at-eol", "-U0", diff_from, *end], root)
        )
        untracked: list[str] = []
        if include_worktree:
            out = run_git(["ls-files", "--others", "--exclude-standard", "-z"], root)
            untracked = sorted(p for p in out.split("\0") if p)
    except GitError as exc:
        raise ScopeError(str(exc)) from exc

    files: list[FileChange] = []
    filtered_out: list[str] = []
    hash_items: list[tuple] = []

    def build(rec: dict) -> FileChange:
        path, status, binary = rec["path"], rec["status"], rec["binary"]
        if status == "D" or binary:
            new_text = None
        elif include_worktree:
            data = _read_worktree(root, path)
            new_text = _decode(data) if data is not None else None
        else:
            new_text = show_file("HEAD", path, root)
        old_text = None
        if status != "A" and not binary:
            old_text = show_file(diff_from, rec["old_path"] or path, root)
        return FileChange(
            path=path,
            old_path=rec["old_path"] if status == "R" else None,
            status=status,
            is_binary=binary,
            hunks=rec["hunks"],
            added_lines=rec["added"],
            deleted_lines=rec["deleted"],
            new_text=new_text,
            old_text=old_text,
        )

    for rec in raw:
        if not matches_path_filters(rec["path"], filters):
            filtered_out.append(rec["path"])
            continue
        files.append(build(rec))

    for rec in hash_raw:
        if not matches_path_filters(rec["path"], filters):
            continue
        add = tuple(ln[1:].rstrip() for h in rec["hunks"] for ln in h.lines if ln[:1] == "+")
        rem = tuple(ln[1:].rstrip() for h in rec["hunks"] for ln in h.lines if ln[:1] == "-")
        hash_items.append((rec["path"], rec["status"], add, rem))

    for path in untracked:
        if not matches_path_filters(path, filters):
            filtered_out.append(path)
            continue
        data = _read_worktree(root, path)
        if data is None:  # broken symlink, directory, race: skip
            continue
        binary = b"\0" in data
        text = None if binary else _decode(data)
        hunk = None if binary else _untracked_hunk(text)
        files.append(
            FileChange(
                path=path, old_path=None, status="A", is_binary=binary,
                hunks=[hunk] if hunk else [], added_lines=hunk.new_len if hunk else 0,
                deleted_lines=0, new_text=text, old_text=None,
            )
        )
        add = tuple(ln[1:].rstrip() for ln in hunk.lines) if hunk else ()
        if binary:
            add = (hashlib.sha1(data).hexdigest(),)
        hash_items.append((path, "A", add, ()))

    digest = hashlib.sha1(repr(sorted(hash_items)).encode("utf-8")).hexdigest()
    return Scope(
        repo_root=root,
        repo_name=root.name,
        branch=branch,
        base_ref=resolved,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
        filtered_out=filtered_out,
        untracked=untracked,
        diff_hash=digest,
        include_worktree=include_worktree,
        diff_from=diff_from,
        incremental=incremental,
    )


# --------------------------------------------------------------------- helpers


def changed_line_ranges(scope: Scope) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for f in scope.files:
        ranges = [r for h in f.hunks for r in h.added_ranges]
        if ranges:
            out[f.path] = ranges
    return out


def is_in_diff(scope: Scope, path: str, line: int) -> bool:
    return any(lo <= line <= hi for lo, hi in changed_line_ranges(scope).get(path, []))


_DOC_PATTERNS = ("*.md", "*.rst", "*.txt", "*.lock", "LICENSE*")
_DOC_TEXT_PATTERNS = ("*.md", "*.rst", "*.txt")


def _is_docs_or_lockfile(path: str) -> bool:
    """Name matches a doc/lockfile pattern and the risk tier is "docs".

    Prompt files and agent-instruction files (SKILL.md, CLAUDE.md, AGENTS.md, .claude/**)
    have risk tier "source", so a diff of only those is not a docs-only diff.
    """
    from preflight_review.context.risk import risk_tier  # local: context imports scope

    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in _DOC_PATTERNS) and risk_tier(path) == "docs"


def only_docs_or_lockfiles(scope: Scope) -> bool:
    return all(_is_docs_or_lockfile(f.path) for f in scope.files)


def has_doc_text(scope: Scope) -> bool:
    """True when some changed, non-deleted file is prose (.md/.rst/.txt), not only
    lockfiles, licences or requirements*.txt."""
    def prose(name: str) -> bool:
        return not name.startswith("requirements") and any(
            fnmatch.fnmatch(name, p) for p in _DOC_TEXT_PATTERNS
        )

    return any(f.status != "D" and prose(f.path.rsplit("/", 1)[-1]) for f in scope.files)


def docs_only_skip(scope: Scope, docs_review: str) -> bool:
    """Skip model stages: a lockfile/licence-only diff, or a docs-only diff with docs_review off.

    Under docs_review auto|on a docs-only diff with prose still gets a doc-consistency review.
    """
    if not only_docs_or_lockfiles(scope):
        return False
    return docs_review == "off" or not has_doc_text(scope)
