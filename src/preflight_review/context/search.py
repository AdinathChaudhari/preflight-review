"""Repo-wide regex search: ripgrep when available, pure-Python scan otherwise."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from preflight_review.gitutil import ls_files


def _rg_search(
    repo_root: Path, pattern: str, globs: list[str]
) -> list[tuple[str, int, str]] | None:
    """Optional fast path (not exercised by the unit tests). None = fall back."""
    cmd = ["rg", "--json", "-e", pattern]
    for g in globs:
        cmd += ["-g", g]
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode not in (0, 1):
        return None
    hits: list[tuple[str, int, str]] = []
    for raw in proc.stdout.splitlines():
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") != "match":
            continue
        data = msg["data"]
        path = data.get("path", {}).get("text")
        text = data.get("lines", {}).get("text")
        if path is None or text is None:
            continue
        if path.startswith("./"):
            path = path[2:]
        hits.append((path, int(data["line_number"]), text.rstrip("\r\n")))
    return hits


def _python_search(
    repo_root: Path, pattern: str, globs: list[str]
) -> list[tuple[str, int, str]]:
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    hits: list[tuple[str, int, str]] = []
    for path in sorted(set(ls_files(repo_root, globs))):
        try:
            data = (repo_root / path).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append((path, n, line))
    return hits


def grep_repo(
    repo_root: Path,
    pattern: str,
    globs: list[str] | None = None,
    max_hits: int = 200,
    use_rg: bool | None = None,
) -> list[tuple[str, int, str]]:
    """Return (path, 1-based line, line text) hits sorted by (path, line)."""
    repo_root = Path(repo_root)
    globs = ["*.py"] if globs is None else globs
    hits: list[tuple[str, int, str]] | None = None
    want_rg = (shutil.which("rg") is not None) if use_rg is None else use_rg
    if want_rg and shutil.which("rg") is not None:
        hits = _rg_search(repo_root, pattern, globs)
    if hits is None:
        hits = _python_search(repo_root, pattern, globs)
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits[:max_hits]
