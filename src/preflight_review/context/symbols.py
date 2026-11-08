"""stdlib-ast symbol index with an on-disk cache keyed by (mtime_ns, size)."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from preflight_review.gitutil import ls_files
from preflight_review.scope import matches_path_filters

CACHE_VERSION = 1


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # "function" | "class" | "method"
    file: str
    start_line: int  # includes decorators
    end_line: int
    signature: str
    docstring_first_line: str | None = None


@dataclass
class FileIndex:
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    # (callee simple name, line, enclosing qualname)
    calls: list[tuple[str, int, str | None]] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)


def _signature(node: ast.AST, lines: list[str]) -> str:
    body = node.body[0]
    first = node.lineno
    if body.lineno == first:
        return lines[first - 1][: body.col_offset].strip()
    seg = lines[first - 1 : body.lineno - 1]
    return " ".join(s.strip() for s in seg if s.strip())


def _doc_first_line(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    return doc.strip().splitlines()[0].strip() or None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str]):
        self.path = path
        self.lines = lines
        self.symbols: list[Symbol] = []
        self.calls: list[tuple[str, int, str | None]] = []
        self.imports: dict[str, str] = {}
        self.stack: list[tuple[str, str]] = []  # (qualname, kind)

    def _enclosing(self) -> str | None:
        return self.stack[-1][0] if self.stack else None

    def _visit_all(self, nodes) -> None:
        for n in nodes:
            if n is not None:
                self.visit(n)

    def _define(self, node, kind: str) -> None:
        parent = self.stack[-1] if self.stack else None
        qual = f"{parent[0]}.{node.name}" if parent else node.name
        if kind == "function" and parent and parent[1] == "class":
            kind = "method"
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        self.symbols.append(
            Symbol(
                name=node.name,
                qualname=qual,
                kind=kind,
                file=self.path,
                start_line=start,
                end_line=node.end_lineno or node.lineno,
                signature=_signature(node, self.lines),
                docstring_first_line=_doc_first_line(node),
            )
        )
        # decorators, defaults, bases evaluate in the outer scope
        self._visit_all(node.decorator_list)
        if kind == "class":
            self._visit_all(node.bases)
            self._visit_all(k.value for k in node.keywords)
        else:
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
        self.stack.append((qual, "class" if kind == "class" else "function"))
        self._visit_all(node.body)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._define(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._define(node, "class")

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            self.calls.append((f.id, f.end_lineno or f.lineno, self._enclosing()))
        elif isinstance(f, ast.Attribute):
            self.calls.append((f.attr, f.end_lineno or f.lineno, self._enclosing()))
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            if a.asname:
                self.imports[a.asname] = a.name
            else:
                top = a.name.split(".")[0]
                self.imports[top] = top

    def visit_ImportFrom(self, node):
        mod = "." * (node.level or 0) + (node.module or "")
        for a in node.names:
            if a.name == "*":
                continue
            joined = f"{mod}{a.name}" if mod.endswith(".") else f"{mod}.{a.name}"
            self.imports[a.asname or a.name] = joined


def index_file(path: str, text: str) -> FileIndex:
    """Index one Python file's text. Never raises; unparsable text -> empty index."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return FileIndex(path=path)
    v = _Visitor(path, text.split("\n"))
    try:
        v.visit(tree)
    except RecursionError:
        return FileIndex(path=path)
    return FileIndex(path=path, symbols=v.symbols, calls=v.calls, imports=v.imports)


def _file_index_from_dict(d: dict) -> FileIndex:
    return FileIndex(
        path=d["path"],
        symbols=[Symbol(**s) for s in d["symbols"]],
        calls=[(c[0], int(c[1]), c[2]) for c in d["calls"]],
        imports=dict(d["imports"]),
    )


class RepoIndex:
    def __init__(
        self,
        repo_root: Path,
        path_filters: list[str] | None = None,
        cache: bool = True,
    ):
        self.repo_root = Path(repo_root)
        self.path_filters = path_filters
        self.cache = cache
        self.parse_count = 0
        self._memo: dict[str, FileIndex] = {}
        self._entries: dict[str, dict] | None = None  # loaded cache entries
        self._dirty = False
        # Packets are reviewed in parallel: finder threads resolve enclosing symbols while
        # other threads parse. One re-entrant lock guards the memo, the cache and the counter.
        self._lock = threading.RLock()

    @property
    def cache_path(self) -> Path:
        return self.repo_root / ".preflight" / "cache" / "ast-index.json"

    def _load_entries(self) -> dict[str, dict]:
        with self._lock:
            return self._load_entries_locked()

    def _load_entries_locked(self) -> dict[str, dict]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict] = {}
        if self.cache:
            try:
                data = json.loads(self.cache_path.read_text())
                if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
                    raw = data.get("files")
                    if isinstance(raw, dict):
                        entries = raw
            except (OSError, ValueError):
                entries = {}
        self._entries = entries
        return entries

    def get(self, path: str) -> FileIndex:
        with self._lock:
            return self._get_locked(path)

    def _get_locked(self, path: str) -> FileIndex:
        if path in self._memo:
            return self._memo[path]
        full = self.repo_root / path
        try:
            st = full.stat()
            data = full.read_bytes() if not self.cache else None
        except OSError:
            fi = FileIndex(path=path)
            self._memo[path] = fi
            return fi
        key = [st.st_mtime_ns, st.st_size]
        if self.cache:
            entry = self._load_entries().get(path)
            if isinstance(entry, dict) and entry.get("key") == key:
                try:
                    fi = _file_index_from_dict(entry["index"])
                    self._memo[path] = fi
                    return fi
                except (KeyError, TypeError, ValueError, IndexError):
                    pass
            try:
                data = full.read_bytes()
            except OSError:
                fi = FileIndex(path=path)
                self._memo[path] = fi
                return fi
        fi = self._parse(path, data.decode("utf-8", errors="replace"))
        if self.cache:
            self._load_entries()[path] = {"key": key, "index": asdict(fi)}
            self._dirty = True
        self._memo[path] = fi
        return fi

    def _parse(self, path: str, text: str) -> FileIndex:
        self.parse_count += 1
        return index_file(path, text)

    def index_text(self, path: str, text: str) -> FileIndex:
        """Index non-worktree text (e.g. old_text). Never cached."""
        with self._lock:
            return self._parse(path, text)

    def save(self) -> None:
        """Atomically write the cache if anything changed."""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        if not self.cache or not self._dirty or self._entries is None:
            return
        target = self.cache_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".ast-index-")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump({"version": CACHE_VERSION, "files": self._entries}, fh)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._dirty = False
        except OSError:
            pass  # cache is best-effort

    def python_files(self) -> list[str]:
        files = ls_files(self.repo_root, ["*.py"])
        if self.path_filters:
            files = [f for f in files if matches_path_filters(f, self.path_filters)]
        return sorted(files)

    def definitions(self, name: str) -> list[Symbol]:
        out: list[Symbol] = []
        for path in self.python_files():
            for s in self.get(path).symbols:
                if s.name == name or s.qualname == name:
                    out.append(s)
        return out

    def enclosing_symbol(self, path: str, line: int) -> Symbol | None:
        with self._lock:
            symbols = list(self.get(path).symbols)
        best: Symbol | None = None
        for s in symbols:
            if s.start_line <= line <= s.end_line:
                if best is None or (s.end_line - s.start_line, -s.start_line) <= (
                    best.end_line - best.start_line,
                    -best.start_line,
                ):
                    best = s
        return best
