"""Hunk rendering: enclosing-function extension, numbering, overlaps."""

from __future__ import annotations

import re

from preflight_review.context.hunks import render_diff, render_file_hunks
from preflight_review.context.symbols import RepoIndex
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit
from tests.test_symbols import FILES, SERVICE, fixture_repo


def _change(tmp_path, files, msg="change"):
    repo = fixture_repo(tmp_path)
    branch(repo, "feature")
    commit(repo, files, msg)
    scope = resolve_scope(repo, "main")
    return repo, scope, RepoIndex(repo, cache=False)


def _fc(scope, path):
    return next(f for f in scope.files if f.path == path)


def _plus_lines_consistent(rendered_text: str, new_file_lines: list[str]) -> int:
    n_checked = 0
    for ln in rendered_text.split("\n"):
        m = re.match(r"^(\d+) \+(.*)$", ln)
        if m:
            assert new_file_lines[int(m[1]) - 1] == m[2]
            n_checked += 1
    return n_checked


def test_modified_line_extends_to_whole_function(tmp_path):
    new = SERVICE.replace("discount = 10", "discount = 15")
    repo, scope, idx = _change(tmp_path, {"app/service.py": new})
    (rh,) = render_file_hunks(_fc(scope, "app/service.py"), idx)
    lines = new.split("\n")
    start = next(i for i, l in enumerate(lines, 1) if l.startswith("def compute_total"))
    end = next(i for i, l in enumerate(lines, 1) if l == "    return total")
    assert (rh.start, rh.end) == (start, end)
    assert "(enclosing: compute_total)" in rh.text.split("\n")[0]
    assert rh.text.split("\n")[1] == "__new hunk__"
    assert f"{start} " in rh.text and f"{end} " in rh.text
    assert "__old hunk__" in rh.text  # a replaced line has a deletion
    assert _plus_lines_consistent(rh.text, lines) == 1


def test_plus_line_numbers_match_new_file(tmp_path):
    new = SERVICE.replace("discount = 10", "discount = 15\n        extra = 1")
    repo, scope, idx = _change(tmp_path, {"app/service.py": new})
    text = render_diff(scope, idx)
    assert _plus_lines_consistent(text, new.split("\n")) == 2
    assert text.startswith("## File: 'app/service.py'")


def test_inside_big_fn_only_pad(tmp_path):
    new = SERVICE.replace("    v60 = 60", "    v60 = 600")
    repo, scope, idx = _change(tmp_path, {"app/service.py": new})
    (rh,) = render_file_hunks(_fc(scope, "app/service.py"), idx, pad=15)
    lines = new.split("\n")
    n = next(i for i, l in enumerate(lines, 1) if l == "    v60 = 600")
    hunk = _fc(scope, "app/service.py").hunks[0]
    assert rh.start == hunk.new_start - 15
    assert rh.end == hunk.new_start + hunk.new_len - 1 + 15
    assert rh.start <= n <= rh.end
    assert "(enclosing: big_fn)" in rh.text.split("\n")[0]
    assert _plus_lines_consistent(rh.text, lines) == 1


def test_deletion_renders_old_hunk(tmp_path):
    new = SERVICE.replace("    discount = 0\n", "")
    repo, scope, idx = _change(tmp_path, {"app/service.py": new})
    (rh,) = render_file_hunks(_fc(scope, "app/service.py"), idx)
    head, _, old = rh.text.partition("__old hunk__\n")
    assert old.strip() == "-    discount = 0"
    assert "discount = 0" not in head.split("__new hunk__")[1]


def test_deleted_file_message(tmp_path):
    repo = fixture_repo(tmp_path)
    branch(repo, "feature")
    (repo / "app" / "api.py").unlink()
    commit(repo, {}, "rm api")
    scope = resolve_scope(repo, "main")
    (rh,) = render_file_hunks(_fc(scope, "app/api.py"), RepoIndex(repo, cache=False))
    assert rh.text == "## File 'app/api.py' was deleted"
    assert "was deleted" in render_diff(scope, RepoIndex(repo, cache=False))


def test_two_close_hunks_do_not_overlap(tmp_path):
    new = SERVICE.replace("total = 0\n", "total = 0.0\n", 1).replace(
        "discount = 10", "discount = 15"
    )
    repo, scope, idx = _change(tmp_path, {"app/service.py": new})
    fc = _fc(scope, "app/service.py")
    rendered = render_file_hunks(fc, idx)
    seen: list[int] = []
    for rh in rendered:
        nums = [int(m[1]) for m in re.finditer(r"^(\d+) [+ ]", rh.text, re.M)]
        seen += nums
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen)
    assert _plus_lines_consistent("\n".join(r.text for r in rendered), new.split("\n")) == 2


def test_non_python_uses_pad_only(tmp_path):
    readme = "\n".join(f"line {i}" for i in range(1, 61)) + "\n"
    repo = fixture_repo(tmp_path)
    commit(repo, {"README.md": readme}, "readme")
    branch(repo, "feature")
    commit(repo, {"README.md": readme.replace("line 30\n", "LINE 30\n")}, "edit")
    scope = resolve_scope(repo, "main")
    (rh,) = render_file_hunks(_fc(scope, "README.md"), RepoIndex(repo, cache=False), pad=5)
    hunk = _fc(scope, "README.md").hunks[0]
    assert rh.start == hunk.new_start - 5
    assert "enclosing" not in rh.text.split("\n")[0]
    assert "30 +LINE 30" in rh.text


def test_unparsable_python_falls_back_to_pad(tmp_path):
    broken = "def oops(:\n" + "\n".join(f"    x{i} = {i}" for i in range(40)) + "\n"
    repo, scope, idx = _change(tmp_path, {"app/broken.py": broken})
    (rh,) = render_file_hunks(_fc(scope, "app/broken.py"), idx)
    assert "enclosing" not in rh.text.split("\n")[0]


def test_render_diff_deterministic(tmp_path):
    new = SERVICE.replace("discount = 10", "discount = 15")
    repo, scope, idx = _change(
        tmp_path, {"app/service.py": new, "app/api.py": FILES["app/api.py"] + "\nX = 1\n"}
    )
    a = render_diff(scope, idx)
    b = render_diff(scope, RepoIndex(repo, cache=False))
    assert a == b and a.index("app/api.py") < a.index("app/service.py")
