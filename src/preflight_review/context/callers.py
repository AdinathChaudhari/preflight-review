"""Changed symbols, callees, callers and tests touching them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from preflight_review.context.search import grep_repo
from preflight_review.context.symbols import FileIndex, RepoIndex, Symbol
from preflight_review.scope import Scope


@dataclass
class CallSite:
    file: str
    line: int
    enclosing: str | None
    snippet: str  # +-context lines, each prefixed "<line_no> "


def _touch_points(fc) -> list[tuple[int, int]]:
    """New-file line ranges touched by a change: added ranges plus deletion points."""
    pts = [r for h in fc.hunks for r in h.added_ranges]
    for h in fc.hunks:
        new_no = h.new_start
        for ln in h.lines:
            if ln.startswith("-"):
                p = max(new_no - 1, 1)
                pts.append((p, p))
            elif ln.startswith("+") or ln.startswith(" "):
                new_no += 1
    return pts


def changed_symbols(scope: Scope, index: RepoIndex) -> list[Symbol]:
    """Symbols changed by the diff.

    New-text symbols whose span overlaps a touched line (innermost only: a class is
    reported only when a touched line is outside all of its children), plus symbols
    in old_text whose qualname no longer exists in the new text (deleted/renamed).
    """
    out: list[Symbol] = []
    seen: set[tuple[str, str]] = set()

    def add(s: Symbol) -> None:
        key = (s.file, s.qualname)
        if key not in seen:
            seen.add(key)
            out.append(s)

    for fc in scope.files:
        if fc.is_binary or not fc.path.endswith(".py"):
            continue
        new_fi = FileIndex(fc.path)
        if fc.status != "D" and fc.new_text is not None:
            new_fi = index.index_text(fc.path, fc.new_text)
            for lo, hi in _touch_points(fc):
                hit = [s for s in new_fi.symbols if s.start_line <= hi and s.end_line >= lo]
                for s in hit:
                    inner = [
                        c for c in hit
                        if c is not s and c.qualname.startswith(s.qualname + ".")
                    ]
                    if not inner:
                        add(s)
        if fc.old_text is not None:
            new_names = {s.qualname for s in new_fi.symbols}
            old_fi = index.index_text(fc.path, fc.old_text)
            for s in old_fi.symbols:
                if s.qualname not in new_names:
                    add(Symbol(**{**s.__dict__, "file": fc.path}))
    return out


def callees(symbol: Symbol, index: RepoIndex) -> list[Symbol]:
    fi = index.get(symbol.file)
    names: list[str] = []
    for name, _line, enc in fi.calls:
        if enc is None:
            continue
        if enc == symbol.qualname or enc.startswith(symbol.qualname + "."):
            if name not in names:
                names.append(name)
    out: list[Symbol] = []
    seen: set[tuple[str, str]] = set()
    for name in names:
        for d in index.definitions(name):
            key = (d.file, d.qualname)
            if (d.file == symbol.file and d.qualname == symbol.qualname) or key in seen:
                continue
            seen.add(key)
            out.append(d)
            if len(out) >= 10:
                return out
    return out


def _snippet(repo_root: Path, path: str, line: int, context: int) -> str:
    try:
        text = (repo_root / path).read_text(errors="replace")
    except OSError:
        return ""
    lines = text.split("\n")
    lo, hi = max(1, line - context), min(len(lines), line + context)
    return "\n".join(f"{n} {lines[n - 1]}" for n in range(lo, hi + 1))


def callers(
    symbol: Symbol,
    index: RepoIndex,
    scope: Scope,
    per_symbol: int = 5,
    context: int = 5,
) -> list[CallSite]:
    root = index.repo_root
    in_diff_files = {fc.path for fc in scope.files}
    hits = grep_repo(root, rf"\b{re.escape(symbol.name)}\(")
    allowed = set(index.python_files()) if index.path_filters else None
    cands: list[tuple[bool, str, int, str]] = []
    for path, line, text in hits:
        if allowed is not None and path not in allowed:
            continue
        if re.match(r"\s*(async\s+def|def|class)\b", text):
            continue
        if path == symbol.file and symbol.start_line <= line <= symbol.start_line + len(
            symbol.signature.split("\n")
        ) - 1:
            continue
        fi = index.get(path)
        if not any(n == symbol.name and ln == line for n, ln, _ in fi.calls):
            continue
        cands.append((path in in_diff_files, path, line, text))
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    out: list[CallSite] = []
    for _in_diff, path, line, _text in cands[:per_symbol]:
        enc = index.enclosing_symbol(path, line)
        out.append(
            CallSite(path, line, enc.qualname if enc else None, _snippet(root, path, line, context))
        )
    return out


def tests_touching(symbols: list[Symbol], repo_root: Path) -> list[str]:
    from preflight_review.gitutil import ls_files

    names = {s.name for s in symbols}
    if not names:
        return []
    rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
    found: list[str] = []
    for path in sorted(ls_files(repo_root, ["*.py"])):
        base = path.rsplit("/", 1)[-1]
        is_test = (
            path.startswith("tests/")
            or "/tests/" in path
            or base.startswith("test_")
            or base.endswith("_test.py")
        )
        if not is_test:
            continue
        try:
            text = (Path(repo_root) / path).read_text(errors="replace")
        except OSError:
            continue
        if rx.search(text):
            found.append(path)
            if len(found) >= 10:
                break
    return found
