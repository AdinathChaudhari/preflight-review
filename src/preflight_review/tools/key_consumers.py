"""Key and id consumer signals for changed data files.

Two deterministic checks over changed JSON/YAML files. Their output is a list of
*candidate* tool signals for the review packet, never findings: a finder checks
each lead against the code and reports it only when it holds, and the verifier
decides as usual.

- Unread key: a key added to a data file that no code reads, while at least
  MIN_READ_SIBLINGS of its sibling keys are read by literal key somewhere in code
  (`"key"`, `['key']`, `.get('key')`, `.key`). A file whose siblings nobody reads
  by name is consumed whole or by iteration, and is skipped.
- Undefined id: a string value added to a data file that matches a configured id
  regex (`tools.key_consumers.id_patterns`, empty by default) but is defined
  nowhere in the repo's data files, or only inside a `_superseded*`/`deprecated*`
  container. An id is defined where it is a mapping key or the value of an `id` key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from preflight_review import gitutil
from preflight_review.config import Config, compile_id_patterns
from preflight_review.errors import GitError
from preflight_review.scope import FileChange, Scope, glob_to_regex

DATA_EXTS = {".json", ".yaml", ".yml"}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt",
    ".rs", ".php", ".cs", ".swift", ".scala", ".sh", ".bash", ".sql", ".html", ".jinja2", ".j2",
}
MIN_READ_SIBLINGS = 2
MAX_SIGNALS_PER_FILE = 10
MAX_CODE_FILE_BYTES = 1_000_000
# Ids are often defined in one big catalogue file: read data files well past the code cap.
MAX_DATA_FILE_BYTES = 20_000_000
RETIRED_PREFIXES = ("_superseded", "deprecated")

@dataclass(frozen=True)
class KeySignal:
    file: str
    line: int
    text: str

    def render(self) -> str:
        return f"candidate key_consumers {self.file}:{self.line} {self.text}"


def _load(text: str | None, path: str) -> Any:
    if text is None:
        return None
    try:
        if path.endswith(".json"):
            return json.loads(text)
        return yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return None


def _walk(node: Any, parent: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    """Yield (parent path, key or None, value) for every mapping entry and list item."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield parent, k, v
            yield from _walk(v, parent + (str(k),))
    elif isinstance(node, list):
        for v in node:
            yield parent, None, v
            yield from _walk(v, parent + ("[]",))


def _keys_by_parent(doc: Any) -> dict[tuple[str, ...], set[str]]:
    out: dict[tuple[str, ...], set[str]] = {}
    for parent, k, _ in _walk(doc):
        if isinstance(k, str):
            out.setdefault(parent, set()).add(k)
    return out


def _strings(doc: Any) -> set[str]:
    return {v for _, _, v in _walk(doc) if isinstance(v, str)}


def _key_re(key: str) -> re.Pattern:
    """A mapping key: `"key":` (JSON or quoted YAML) or `key:` at the start of a YAML line."""
    k = re.escape(key)
    return re.compile(rf"([\"']){k}\1\s*:|^\s*(?:-\s+)?{k}\s*:")


def _value_re(value: str) -> re.Pattern:
    """A string value as a whole token: quoted, or bare (YAML)."""
    v = re.escape(value)
    return re.compile(rf"[\"']{v}[\"']|(?<![\w.-]){v}(?![\w.-])")


def _line_of(fc: FileChange, rx: re.Pattern) -> int:
    """The first added line matching `rx`, else the first matching line, else 1. Matching
    the token (not a substring) keeps a key like `id` off an earlier `"identifier"` line."""
    lines = (fc.new_text or "").splitlines()
    added = sorted(
        n for h in fc.hunks for a, b in h.added_ranges for n in range(a, b + 1)
    )
    for n in added:
        if 1 <= n <= len(lines) and rx.search(lines[n - 1]):
            return n
    for i, line in enumerate(lines, 1):
        if rx.search(line):
            return i
    return 1


def _matches(path: str, globs: list[str]) -> bool:
    return any(glob_to_regex(g).search(path) for g in globs)


def _read(root: Path, path: str, limit: int = MAX_CODE_FILE_BYTES) -> str | None:
    p = root / path
    try:
        if p.stat().st_size > limit:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _code_texts(scope: Scope) -> dict[str, str]:
    root = scope.repo_root
    try:
        paths = set(gitutil.ls_files(root))
    except GitError:  # not a git checkout: fall back to the diff only
        paths = set()
    paths |= {fc.path for fc in scope.files if fc.status != "D"}
    out: dict[str, str] = {}
    for p in sorted(paths):
        if PurePosixPath(p).suffix.lower() not in CODE_EXTS:
            continue
        text = _read(root, p)
        if text is not None:
            out[p] = text
    return out


def _reader_re(key: str) -> re.Pattern:
    k = re.escape(key)
    access = rf"[\"']{k}[\"']"
    if re.fullmatch(r"[A-Za-z_]\w*", key):
        access += rf"|\.{k}\b"
    return re.compile(access)


def _is_read(key: str, code: dict[str, str], cache: dict[str, bool]) -> bool:
    if key not in cache:
        rx = _reader_re(key)
        cache[key] = any(rx.search(t) for t in code.values())
    return cache[key]


def _definitions(scope: Scope, excludes: list[str]) -> tuple[set[str], set[str], bool]:
    """Ids defined in the repo's data files: (live, only-retired, complete). `complete` is
    False when a data file was too large to read, so "defined nowhere" cannot be claimed."""
    live: set[str] = set()
    retired: set[str] = set()
    complete = True
    try:
        paths = gitutil.ls_files(scope.repo_root)
    except GitError:
        paths = []
    paths = sorted(set(paths) | {fc.path for fc in scope.files if fc.status != "D"})
    for p in paths:
        if PurePosixPath(p).suffix.lower() not in DATA_EXTS or _matches(p, excludes):
            continue
        text = _read(scope.repo_root, p, MAX_DATA_FILE_BYTES)
        if text is None and (scope.repo_root / p).is_file():
            complete = False
        doc = _load(text, p)
        for parent, k, v in _walk(doc):
            in_retired = any(seg.lower().startswith(RETIRED_PREFIXES) for seg in parent)
            ids = [k] if isinstance(k, str) else []
            if k == "id" and isinstance(v, str):
                ids.append(v)
            for i in ids:
                (retired if in_retired else live).add(i)
    return live, retired - live, complete


def key_consumer_signals(scope: Scope, config: Config) -> list[KeySignal]:
    kc = config.tools.key_consumers
    if not kc.enabled:
        return []
    excludes = list(kc.exclude)
    data_changes = [
        fc for fc in scope.files
        if fc.status != "D" and fc.new_text is not None
        and PurePosixPath(fc.path).suffix.lower() in DATA_EXTS
        and not _matches(fc.path, excludes)
    ]
    if not data_changes:
        return []
    out: list[KeySignal] = []
    code: dict[str, str] | None = None
    read_cache: dict[str, bool] = {}
    id_res = compile_id_patterns(kc.id_patterns)
    defs: tuple[set[str], set[str], bool] | None = None

    for fc in data_changes:
        new = _load(fc.new_text, fc.path)
        old = _load(fc.old_text, fc.path)
        if new is None:
            continue
        found: list[KeySignal] = []

        if old is not None and not _matches(fc.path, list(kc.consumed_whole)):
            new_keys, old_keys = _keys_by_parent(new), _keys_by_parent(old)
            for parent in sorted(new_keys):
                added = sorted(new_keys[parent] - old_keys.get(parent, set()))
                if not added:
                    continue
                if code is None:
                    code = _code_texts(scope)
                siblings = sorted(new_keys[parent] - set(added))
                read = [s for s in siblings if _is_read(s, code, read_cache)]
                if len(read) < MIN_READ_SIBLINGS:
                    continue
                for key in added:
                    if _is_read(key, code, read_cache):
                        continue
                    found.append(KeySignal(
                        fc.path, _line_of(fc, _key_re(key)),
                        f"new key '{key}' has no reader in code, while sibling keys "
                        f"{', '.join(repr(s) for s in read[:3])} are read by name",
                    ))

        if id_res:
            added_values = _strings(new) - (_strings(old) if old is not None else set())
            refs = sorted(v for v in added_values if any(r.fullmatch(v) for r in id_res))
            if refs and defs is None:
                defs = _definitions(scope, excludes)
            for ref in refs:
                live, retired, complete = defs or (set(), set(), True)
                if ref in live:
                    continue
                if ref in retired:
                    why = "is superseded"
                elif complete:
                    why = "is defined in no data file"
                else:
                    continue  # an unread oversized data file may define it
                found.append(KeySignal(
                    fc.path, _line_of(fc, _value_re(ref)), f"id '{ref}' {why}",
                ))

        out += found[:MAX_SIGNALS_PER_FILE]
    return out
