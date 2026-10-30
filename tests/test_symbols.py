"""Symbol index, on-disk cache and repo grep."""

from __future__ import annotations

import json

from preflight_review.context.search import grep_repo
from preflight_review.context.symbols import RepoIndex, index_file

from tests.conftest import make_repo, write

_BIG = "def big_fn():\n" + "\n".join(f"    v{i} = {i}" for i in range(118)) + "\n    return v0\n"

SERVICE = '''"""Service."""

import os.path
from decimal import Decimal as D
from . import helpers


def _round(x):
    return round(x, 2)


def compute_total(items):
    """Sum item prices."""
    total = 0
    for item in items:
        price = item["price"]
        qty = item.get("qty", 1)
        total += price * qty
    discount = 0
    if total > 100:
        discount = 10
    total -= discount
    total = _round(total)
    return total


class Cart:
    def __init__(self):
        self.items = []

    @staticmethod
    def helper():
        def inner():
            return 1
        return inner()

    def add(self, item):
        self.items.append(item)
        return len(self.items)


''' + _BIG

API = '''from app.service import Cart, compute_total


def checkout(items):
    cart = Cart()
    cart.add(items[0])
    return compute_total(items)
'''

TEST_SERVICE = '''from app.service import compute_total


def test_total():
    assert compute_total([]) == 0
'''

FILES = {
    "app/__init__.py": "",
    "app/service.py": SERVICE,
    "app/api.py": API,
    "tests/test_service.py": TEST_SERVICE,
    "app/broken.py": "def oops(:\n    pass\n",
    "README.md": "# demo\ncompute_total( is documented here\n",
}


def fixture_repo(tmp_path):
    return make_repo(tmp_path, FILES)


def _line_of(text: str, needle: str) -> int:
    for n, ln in enumerate(text.split("\n"), start=1):
        if needle in ln:
            return n
    raise AssertionError(needle)


def test_symbols_qualnames_kinds_spans():
    fi = index_file("app/service.py", SERVICE)
    by = {s.qualname: s for s in fi.symbols}
    assert by["compute_total"].kind == "function"
    assert by["Cart"].kind == "class"
    assert by["Cart.add"].kind == "method"
    assert by["Cart.helper.inner"].kind == "function"
    assert by["compute_total"].start_line == _line_of(SERVICE, "def compute_total")
    assert by["compute_total"].end_line == _line_of(SERVICE, "return total")
    assert by["compute_total"].signature == "def compute_total(items):"
    assert by["compute_total"].docstring_first_line == "Sum item prices."
    assert by["big_fn"].end_line - by["big_fn"].start_line + 1 == 120


def test_decorator_included_in_start_line():
    fi = index_file("app/service.py", SERVICE)
    helper = next(s for s in fi.symbols if s.qualname == "Cart.helper")
    assert helper.start_line == _line_of(SERVICE, "@staticmethod")
    assert helper.signature == "def helper():"


def test_async_and_multiline_signature():
    text = "async def go(a,\n          b):\n    return a\n"
    (s,) = index_file("x.py", text).symbols
    assert s.kind == "function" and s.name == "go"
    assert s.signature == "async def go(a, b):"


def test_calls_with_enclosing_qualname():
    fi = index_file("app/service.py", SERVICE)
    calls = {(n, enc) for n, _ln, enc in fi.calls}
    assert ("_round", "compute_total") in calls
    assert ("get", "compute_total") in calls
    assert ("append", "Cart.add") in calls
    assert ("inner", "Cart.helper") in calls
    api = index_file("app/api.py", API)
    assert ("add", "checkout") in {(n, e) for n, _l, e in api.calls}
    line = next(ln for n, ln, _ in api.calls if n == "compute_total")
    assert line == _line_of(API, "return compute_total")


def test_imports_map():
    fi = index_file("app/service.py", SERVICE)
    assert fi.imports["os"] == "os"
    assert fi.imports["D"] == "decimal.Decimal"
    assert fi.imports["helpers"] == ".helpers"


def test_syntax_error_gives_empty_index():
    fi = index_file("app/broken.py", FILES["app/broken.py"])
    assert fi.symbols == [] and fi.calls == [] and fi.imports == {}
    assert index_file("x.py", "x = \0").symbols == []


def test_enclosing_symbol_innermost(tmp_path):
    repo = fixture_repo(tmp_path)
    idx = RepoIndex(repo, cache=False)
    inner_line = _line_of(SERVICE, "return 1")
    assert idx.enclosing_symbol("app/service.py", inner_line).qualname == "Cart.helper.inner"
    add_line = _line_of(SERVICE, "self.items.append")
    assert idx.enclosing_symbol("app/service.py", add_line).qualname == "Cart.add"
    assert idx.enclosing_symbol("app/service.py", 1) is None


def test_definitions_across_files(tmp_path):
    repo = fixture_repo(tmp_path)
    write(repo, "app/other.py", "def compute_total(x):\n    return x\n")
    from tests.conftest import commit

    commit(repo, {}, "add other")
    idx = RepoIndex(repo, cache=False)
    defs = idx.definitions("compute_total")
    assert sorted(d.file for d in defs) == ["app/other.py", "app/service.py"]
    assert [d.file for d in idx.definitions("Cart.add")] == ["app/service.py"]
    assert idx.python_files() == sorted(idx.python_files())
    assert "README.md" not in idx.python_files()


def test_path_filters_limit_python_files(tmp_path):
    repo = fixture_repo(tmp_path)
    idx = RepoIndex(repo, path_filters=["!tests/**"], cache=False)
    assert "tests/test_service.py" not in idx.python_files()
    assert "app/service.py" in idx.python_files()


def test_cache_hit_and_invalidation(tmp_path):
    repo = fixture_repo(tmp_path)
    first = RepoIndex(repo)
    first.definitions("compute_total")
    n_files = len(first.python_files())
    assert first.parse_count == n_files
    first.save()
    assert (repo / ".preflight" / "cache" / "ast-index.json").exists()

    second = RepoIndex(repo)
    defs = second.definitions("compute_total")
    assert second.parse_count == 0
    assert defs[0].qualname == "compute_total"

    write(repo, "app/service.py", SERVICE + "\n\ndef extra():\n    return 1\n")
    third = RepoIndex(repo)
    assert [s.name for s in third.definitions("extra")] == ["extra"]
    assert third.parse_count == 1


def test_corrupt_cache_ignored(tmp_path):
    repo = fixture_repo(tmp_path)
    cache = repo / ".preflight" / "cache" / "ast-index.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    idx = RepoIndex(repo)
    assert idx.definitions("compute_total")
    assert idx.parse_count > 0
    idx.save()
    json.loads(cache.read_text())  # rewritten as valid JSON

    cache.write_text(json.dumps({"version": 1, "files": {"app/api.py": {"key": [0, 0], "index": 5}}}))
    assert RepoIndex(repo).get("app/api.py").symbols[0].name == "checkout"


def test_index_text_not_cached(tmp_path):
    repo = fixture_repo(tmp_path)
    idx = RepoIndex(repo)
    fi = idx.index_text("old.py", "def a():\n    pass\n")
    assert [s.name for s in fi.symbols] == ["a"]
    assert idx.parse_count == 1
    idx.save()
    assert not (repo / ".preflight" / "cache" / "ast-index.json").exists()


def test_grep_python_path(tmp_path):
    repo = fixture_repo(tmp_path)
    hits = grep_repo(repo, r"\bcompute_total\(", use_rg=False)
    assert [(p, ln) for p, ln, _ in hits] == [
        ("app/api.py", _line_of(API, "return compute_total")),
        ("app/service.py", _line_of(SERVICE, "def compute_total")),
        ("tests/test_service.py", _line_of(TEST_SERVICE, "assert compute_total")),
    ]
    assert hits[0][2] == "    return compute_total(items)"


def test_grep_globs_and_max_hits(tmp_path):
    repo = fixture_repo(tmp_path)
    md = grep_repo(repo, r"compute_total", globs=["*.md"], use_rg=False)
    assert [(p, ln) for p, ln, _ in md] == [("README.md", 2)]
    assert len(grep_repo(repo, r"compute_total", use_rg=False, max_hits=2)) == 2
    assert grep_repo(repo, r"(", use_rg=False) == []
