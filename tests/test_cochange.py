"""Git co-change mining."""

from __future__ import annotations

import pytest

from preflight_review.context.cochange import mine_cochange

from tests.conftest import commit, make_repo


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {"seed.txt": "s\n"})
    for i in range(4):
        commit(r, {"a.py": f"a{i}\n", "b.py": f"b{i}\n"}, f"ab{i}")
    for i in range(2):
        commit(r, {"a.py": f"alone{i}\n"}, f"a{i}")
    return r


def test_companion_support_and_confidence(repo):
    (c,) = mine_cochange(repo, ["a.py"])
    assert (c.companion, c.support, c.total) == ("b.py", 4, 6)
    assert c.confidence == pytest.approx(4 / 6)


def test_big_commit_ignored(repo):
    files = {f"bulk/f{i}.py": "x\n" for i in range(38)}
    files["a.py"] = "bulk\n"
    files["c.py"] = "c\n"
    commit(repo, files, "bulk")
    comps = mine_cochange(repo, ["a.py"])
    assert [c.companion for c in comps] == ["b.py"]
    assert comps[0].total == 6


def test_lockfiles_dropped(tmp_path):
    r = make_repo(tmp_path, {"s": "s\n"})
    for i in range(4):
        commit(r, {"a.py": f"{i}\n", "uv.lock": f"{i}\n", "x.lock": f"{i}\n",
                   "package-lock.json": f"{i}\n"}, f"c{i}")
    assert mine_cochange(r, ["a.py"]) == []


def test_companions_exclude_changed_paths(repo):
    assert mine_cochange(repo, ["a.py", "b.py"]) == []


def test_cap_five_per_file(tmp_path):
    r = make_repo(tmp_path, {"s": "s\n"})
    for i in range(3):
        files = {f"c{j}.py": f"{i}\n" for j in range(8)}
        files["a.py"] = f"{i}\n"
        commit(r, files, f"c{i}")
    comps = mine_cochange(r, ["a.py"])
    assert len(comps) == 5
    assert [c.companion for c in comps] == [f"c{j}.py" for j in range(5)]
