from __future__ import annotations

import pytest

from preflight_review.errors import ScopeError
from preflight_review.scope import (
    changed_line_ranges,
    glob_to_regex,
    is_in_diff,
    matches_path_filters,
    only_docs_or_lockfiles,
    resolve_scope,
)

from .conftest import branch, commit, make_repo, write

TEN = "".join(f"line{i}\n" for i in range(1, 11))


def _feature(tmp_path, files=None):
    repo = make_repo(tmp_path, files or {"a.py": TEN, "README.md": "hi\n"})
    branch(repo, "feature/x")
    return repo


def _by_path(scope):
    return {f.path: f for f in scope.files}


def test_modified_file_hunks_and_texts(tmp_path):
    repo = _feature(tmp_path)
    new = TEN.replace("line5\n", "LINE5\nextra\n")
    commit(repo, {"a.py": new}, "edit")
    s = resolve_scope(repo, "main")
    f = _by_path(s)["a.py"]
    assert f.status == "M" and not f.is_binary and f.old_path is None
    assert len(f.hunks) == 1
    h = f.hunks[0]
    assert (h.old_start, h.old_len, h.new_start, h.new_len) == (2, 7, 2, 8)
    assert h.added_ranges == [(5, 6)]
    assert "-line5" in h.lines and "+LINE5" in h.lines and " line4" in h.lines
    assert f.added_lines == 2 and f.deleted_lines == 1
    assert f.new_text == new and f.old_text == TEN
    assert s.branch == "feature/x" and s.repo_name == "repo"
    assert s.base_sha != s.head_sha and not s.incremental


def test_added_file(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"new.py": "x = 1\ny = 2\n"}, "add")
    f = _by_path(resolve_scope(repo, "main"))["new.py"]
    assert f.status == "A" and f.old_text is None and f.new_text == "x = 1\ny = 2\n"
    assert f.hunks[0].added_ranges == [(1, 2)] and f.added_lines == 2


def test_deleted_file(tmp_path):
    repo = _feature(tmp_path)
    (repo / "a.py").unlink()
    commit(repo, {}, "del")
    f = _by_path(resolve_scope(repo, "main"))["a.py"]
    assert f.status == "D" and f.new_text is None and f.old_text == TEN
    assert f.deleted_lines == 10 and f.added_lines == 0
    assert f.hunks[0].added_ranges == []


def test_renamed_file(tmp_path):
    repo = _feature(tmp_path)
    (repo / "a.py").unlink()
    commit(repo, {"b.py": TEN.replace("line5", "five")}, "rename")
    s = resolve_scope(repo, "main")
    f = _by_path(s)["b.py"]
    assert f.status == "R" and f.old_path == "a.py"
    assert f.old_text == TEN and "five" in f.new_text
    assert "a.py" not in _by_path(s)


def test_binary_file(tmp_path):
    repo = _feature(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x00binary")
    commit(repo, {}, "bin")
    f = _by_path(resolve_scope(repo, "main"))["blob.bin"]
    assert f.is_binary and f.hunks == [] and f.new_text is None and f.old_text is None


def test_path_with_spaces(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"dir x/my file.py": "a = 1\n"}, "sp")
    f = _by_path(resolve_scope(repo, "main"))["dir x/my file.py"]
    assert f.status == "A" and f.new_text == "a = 1\n"


def test_untracked_only_with_worktree(tmp_path):
    repo = _feature(tmp_path)
    write(repo, "u.py", "a\nb\n")
    s = resolve_scope(repo, "main", include_worktree=True)
    f = _by_path(s)["u.py"]
    assert s.untracked == ["u.py"] and f.status == "A"
    assert f.hunks[0].added_ranges == [(1, 2)] and f.added_lines == 2
    s2 = resolve_scope(repo, "main", include_worktree=False)
    assert "u.py" not in _by_path(s2) and s2.untracked == []


def test_committed_only_ignores_uncommitted_edits(tmp_path):
    repo = _feature(tmp_path)
    write(repo, "a.py", TEN + "dirty\n")
    wt = resolve_scope(repo, "main", include_worktree=True)
    co = resolve_scope(repo, "main", include_worktree=False)
    assert "a.py" in _by_path(wt) and "dirty" in _by_path(wt)["a.py"].new_text
    assert co.files == []
    assert wt.diff_hash != co.diff_hash


def test_path_filters_exclude_and_include(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"uv.lock": "x\n", "sub/p.lock": "y\n", "src/m.py": "m\n", "d.md": "d\n"}, "c")
    s = resolve_scope(repo, "main", path_filters=["!**/*.lock"])
    assert sorted(_by_path(s)) == ["d.md", "src/m.py"]
    assert sorted(s.filtered_out) == ["sub/p.lock", "uv.lock"]
    s = resolve_scope(repo, "main", path_filters=["src/**"])
    assert sorted(_by_path(s)) == ["src/m.py"]
    assert "d.md" in s.filtered_out


def test_matches_path_filters_and_globs():
    assert matches_path_filters("a/b/c.lock", ["!**/*.lock"]) is False
    assert matches_path_filters("c.lock", ["!**/*.lock"]) is False
    assert matches_path_filters("c.py", ["!**/*.lock"]) is True
    assert matches_path_filters("c.py", []) is True
    assert matches_path_filters("src/a.py", ["src/**", "!src/gen/**"]) is True
    assert matches_path_filters("src/gen/a.py", ["src/**", "!src/gen/**"]) is False
    assert matches_path_filters("lib/a.py", ["src/**"]) is False
    assert glob_to_regex("src/**/x.py").match("src/x.py")
    assert glob_to_regex("src/**/x.py").match("src/a/b/x.py")
    assert not glob_to_regex("src/*.py").match("src/a/x.py")
    assert glob_to_regex("a?.py").match("ab.py") and not glob_to_regex("a?.py").match("a/.py")
    assert glob_to_regex("dist/").match("dist/x/y.js")
    assert glob_to_regex("*.md").match("docs/readme.md")


def test_diff_hash_ignores_trailing_whitespace_only_edit(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"a.py": TEN.replace("line5\n", "NEW\n")}, "edit")
    h1 = resolve_scope(repo, "main").diff_hash
    # trailing whitespace on a changed (added) line and on an untouched line
    write(repo, "a.py", TEN.replace("line5\n", "NEW  \n").replace("line8\n", "line8 \n"))
    h2 = resolve_scope(repo, "main").diff_hash
    assert h1 == h2
    write(repo, "a.py", TEN.replace("line5\n", "DIFFERENT\n"))
    assert resolve_scope(repo, "main").diff_hash != h1


def test_diff_hash_ignores_line_shifts(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"a.py": TEN.replace("line9\n", "NEW\n")}, "edit")
    h1 = resolve_scope(repo, "main").diff_hash
    commit(repo, {"a.py": "pre\n" + TEN.replace("line9\n", "NEW\n")}, "shift")
    h2 = resolve_scope(repo, "main").diff_hash
    assert h1 != h2  # the added "pre" line is a real change
    # pure shift: same added/removed text, different numbers
    (tmp_path / "o").mkdir()
    repo2 = make_repo(tmp_path / "o", {"a.py": TEN})
    branch(repo2, "f")
    commit(repo2, {"a.py": TEN.replace("line9\n", "NEW\n")}, "e")
    h3 = resolve_scope(repo2, "main").diff_hash
    assert h3 == h1


def test_incremental_since_sha(tmp_path):
    repo = _feature(tmp_path)
    c1 = commit(repo, {"one.py": "1\n"}, "one")
    commit(repo, {"two.py": "2\n"}, "two")
    s = resolve_scope(repo, "main", since_sha=c1)
    assert s.incremental and s.diff_from == c1
    assert sorted(_by_path(s)) == ["two.py"]


def test_non_ancestor_since_falls_back(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"one.py": "1\n"}, "one")
    s = resolve_scope(repo, "main", since_sha="0" * 40)
    assert not s.incremental and s.diff_from == s.base_sha
    assert "one.py" in _by_path(s)
    # real commit that is not an ancestor of HEAD
    (tmp_path / "x").mkdir()
    repo2 = _feature(tmp_path / "x")
    sha_feature = commit(repo2, {"f.py": "f\n"}, "f")
    from .conftest import _git

    _git(repo2, "checkout", "-q", "main")
    branch(repo2, "other")
    commit(repo2, {"o.py": "o\n"}, "o")
    s2 = resolve_scope(repo2, "main", since_sha=sha_feature)
    assert not s2.incremental


def test_base_ref_fallback_and_origin(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"n.py": "n\n"}, "n")
    s = resolve_scope(repo, "origin/main")  # no remote: stripped name used
    assert s.base_ref == "main" and "n.py" in _by_path(s)
    s = resolve_scope(repo, "main")
    assert s.base_ref == "main"


def test_unresolvable_base_names_refs(tmp_path):
    repo = _feature(tmp_path)
    with pytest.raises(ScopeError) as ei:
        resolve_scope(repo, "nope")
    assert "nope" in str(ei.value) and "origin/nope" in str(ei.value)


def test_ranges_and_is_in_diff(tmp_path):
    repo = _feature(tmp_path)
    commit(repo, {"a.py": TEN.replace("line5\n", "A\nB\n").replace("line9\n", "Z\n")}, "e")
    s = resolve_scope(repo, "main")
    r = changed_line_ranges(s)
    assert r["a.py"] == [(5, 6), (10, 10)]
    assert is_in_diff(s, "a.py", 6) and is_in_diff(s, "a.py", 10)
    assert not is_in_diff(s, "a.py", 7) and not is_in_diff(s, "zzz.py", 1)


def test_only_docs_or_lockfiles(tmp_path):
    repo = _feature(tmp_path)
    assert only_docs_or_lockfiles(resolve_scope(repo, "main")) is True  # empty
    commit(repo, {"docs/x.md": "m\n", "uv.lock": "l\n", "LICENSE-MIT": "l\n"}, "d")
    assert only_docs_or_lockfiles(resolve_scope(repo, "main")) is True
    commit(repo, {"code.py": "c\n"}, "c")
    assert only_docs_or_lockfiles(resolve_scope(repo, "main")) is False
