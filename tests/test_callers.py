"""changed_symbols, callees, callers, tests_touching."""

from __future__ import annotations

from preflight_review.context.callers import (
    callees,
    callers,
    changed_symbols,
    tests_touching as find_tests,
)
from preflight_review.context.symbols import RepoIndex
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit
from tests.test_symbols import API, SERVICE, fixture_repo


def _scope_after(tmp_path, files):
    repo = fixture_repo(tmp_path)
    branch(repo, "feature")
    commit(repo, files, "change")
    return repo, resolve_scope(repo, "main"), RepoIndex(repo, cache=False)


def test_changed_symbols_finds_modified_function(tmp_path):
    new = SERVICE.replace("discount = 10", "discount = 15")
    repo, scope, idx = _scope_after(tmp_path, {"app/service.py": new})
    got = {(s.file, s.qualname) for s in changed_symbols(scope, idx)}
    assert got == {("app/service.py", "compute_total")}


def test_changed_symbols_reports_method_not_class(tmp_path):
    new = SERVICE.replace("return len(self.items)", "return len(self.items) + 0")
    repo, scope, idx = _scope_after(tmp_path, {"app/service.py": new})
    assert [s.qualname for s in changed_symbols(scope, idx)] == ["Cart.add"]


def test_changed_symbols_includes_deleted_function(tmp_path):
    new = SERVICE.replace("def _round(x):\n    return round(x, 2)\n\n\n", "").replace(
        "total = _round(total)", "total = round(total, 2)"
    )
    repo, scope, idx = _scope_after(tmp_path, {"app/service.py": new})
    got = {s.qualname for s in changed_symbols(scope, idx)}
    assert {"_round", "compute_total"} <= got


def test_changed_symbols_deleted_file(tmp_path):
    repo = fixture_repo(tmp_path)
    branch(repo, "feature")
    (repo / "app" / "api.py").unlink()
    commit(repo, {}, "rm")
    scope = resolve_scope(repo, "main")
    got = [(s.file, s.qualname) for s in changed_symbols(scope, RepoIndex(repo, cache=False))]
    assert got == [("app/api.py", "checkout")]


def test_callees_contains_helper(tmp_path):
    repo = fixture_repo(tmp_path)
    idx = RepoIndex(repo, cache=False)
    (sym,) = idx.definitions("compute_total")
    names = [c.qualname for c in callees(sym, idx)]
    assert "_round" in names
    assert "compute_total" not in names


def test_callers_find_api_and_tests_not_definition(tmp_path):
    new = SERVICE.replace("discount = 10", "discount = 15")
    repo, scope, idx = _scope_after(tmp_path, {"app/service.py": new})
    (sym,) = idx.definitions("compute_total")
    sites = callers(sym, idx, scope)
    assert [(c.file, c.enclosing) for c in sites] == [
        ("app/api.py", "checkout"),
        ("tests/test_service.py", "test_total"),
    ]
    assert all("def compute_total" not in c.snippet.split("\n")[len(c.snippet.split("\n")) // 2]
               for c in sites)
    api_line = next(i for i, l in enumerate(API.split("\n"), 1) if "return compute_total" in l)
    assert sites[0].line == api_line
    assert f"{api_line} " in sites[0].snippet


def test_callers_prefer_files_not_in_diff(tmp_path):
    repo, scope, idx = _scope_after(
        tmp_path, {"app/api.py": API + "\n\ndef again(i):\n    return compute_total(i)\n"}
    )
    (sym,) = idx.definitions("compute_total")
    files = [c.file for c in callers(sym, idx, scope)]
    assert files == ["tests/test_service.py", "app/api.py", "app/api.py"]
    assert [c.file for c in callers(sym, idx, scope, per_symbol=1)] == ["tests/test_service.py"]


def test_callers_ignores_non_calls(tmp_path):
    repo, scope, idx = _scope_after(
        tmp_path, {"app/note.py": "# compute_total( is great\nx = 'compute_total('\n"}
    )
    (sym,) = idx.definitions("compute_total")
    assert "app/note.py" not in [c.file for c in callers(sym, idx, scope)]


def test_tests_touching(tmp_path):
    repo = fixture_repo(tmp_path)
    idx = RepoIndex(repo, cache=False)
    syms = idx.definitions("compute_total")
    assert find_tests(syms, repo) == ["tests/test_service.py"]
    assert find_tests([], repo) == []
    assert find_tests(idx.definitions("checkout"), repo) == []
