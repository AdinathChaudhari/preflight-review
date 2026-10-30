"""PR-Agent-style hunk rendering, extended to the enclosing function."""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight_review.context.symbols import FileIndex, RepoIndex, Symbol
from preflight_review.scope import FileChange, Hunk, Scope


@dataclass
class RenderedHunk:
    path: str
    start: int
    end: int
    text: str


@dataclass
class _Span:
    start: int
    end: int
    added: set[int] = field(default_factory=set)
    old: list[str] = field(default_factory=list)
    enclosing: str | None = None


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _innermost(symbols: list[Symbol], line: int, kinds: tuple[str, ...] | None) -> Symbol | None:
    best: Symbol | None = None
    for s in symbols:
        if kinds is not None and s.kind not in kinds:
            continue
        if s.start_line <= line <= s.end_line:
            if best is None or (s.end_line - s.start_line, -s.start_line) <= (
                best.end_line - best.start_line,
                -best.start_line,
            ):
                best = s
    return best


def _hunk_span(
    h: Hunk, fi: FileIndex, n_lines: int, max_fn_lines: int, pad: int
) -> _Span:
    added = {n for lo, hi in h.added_ranges for n in range(lo, hi + 1)}
    lo = max(h.new_start, 1)
    hi = h.new_start + h.new_len - 1 if h.new_len > 0 else lo
    anchor = min(added) if added else lo
    fn = _innermost(fi.symbols, anchor, ("function", "method"))
    any_sym = _innermost(fi.symbols, anchor, None)
    if fn is not None and fn.end_line - fn.start_line + 1 <= max_fn_lines:
        start, end = min(lo, fn.start_line), max(hi, fn.end_line)
    else:
        start, end = lo - pad, hi + pad
    start, end = max(start, 1), min(end, n_lines)
    old = [ln for ln in h.lines if ln.startswith("-")]
    return _Span(start, end, added, old, any_sym.qualname if any_sym else None)


def render_file_hunks(
    fc: FileChange, index: RepoIndex, max_fn_lines: int = 80, pad: int = 15
) -> list[RenderedHunk]:
    if fc.status == "D":
        return [RenderedHunk(fc.path, 0, 0, f"## File '{fc.path}' was deleted")]
    if fc.is_binary:
        return [RenderedHunk(fc.path, 0, 0, f"## File '{fc.path}' is binary (not shown)")]
    if fc.new_text is None or not fc.hunks:
        return []
    lines = _split_lines(fc.new_text)
    if not lines:
        return []
    fi = index.index_text(fc.path, fc.new_text) if fc.path.endswith(".py") else FileIndex(fc.path)
    spans = sorted(
        (_hunk_span(h, fi, len(lines), max_fn_lines, pad) for h in fc.hunks),
        key=lambda s: (s.start, s.end),
    )
    merged: list[_Span] = []
    for s in spans:
        if merged and s.start <= merged[-1].end + 1:
            prev = merged[-1]
            prev.end = max(prev.end, s.end)
            prev.added |= s.added
            prev.old.extend(s.old)
        else:
            merged.append(s)
    out: list[RenderedHunk] = []
    for s in merged:
        end = max(s.end, s.start)
        head = f"@@ {fc.path}:{s.start}-{end}"
        if s.enclosing:
            head += f" (enclosing: {s.enclosing})"
        body = [
            f"{n} {'+' if n in s.added else ' '}{lines[n - 1]}"
            for n in range(s.start, end + 1)
            if 1 <= n <= len(lines)
        ]
        parts = [head, "__new hunk__", *body]
        if s.old:
            parts += ["__old hunk__", *s.old]
        out.append(RenderedHunk(fc.path, s.start, end, "\n".join(parts)))
    return out


def render_diff(scope: Scope, index: RepoIndex) -> str:
    blocks: list[str] = []
    for fc in scope.files:
        rendered = render_file_hunks(fc, index)
        if not rendered:
            continue
        if fc.status == "D" or fc.is_binary:
            blocks.append(rendered[0].text)
            continue
        blocks.append(f"## File: '{fc.path}'\n\n" + "\n\n".join(r.text for r in rendered))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
