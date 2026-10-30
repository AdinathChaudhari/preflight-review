"""respond / learn commands, driven by one real fake-backend review."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from preflight_review import cli, gitutil
from preflight_review.context.learnings import read_learnings
from preflight_review.errors import PreflightError
from preflight_review.learn import learn, list_learnings
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.respond import respond
from preflight_review.state import load_state

from .conftest import _git, branch, commit, make_repo
from .test_pipeline_e2e import FIXTURES, SERVICE_BAD, SERVICE_BASE, no_gh

BRANCH = "feature/x"
_EMPTY = {"findings": [], "files_read_outside_packet": []}


def fake(mutate=None) -> FakeBackend:
    data = json.loads((FIXTURES / "run1.json").read_text())
    if mutate:
        mutate(data)
    return FakeBackend(data, default=_EMPTY)


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "README.md": "hi\n"})
    branch(r, BRANCH)
    commit(r, {"app/service.py": SERVICE_BAD}, "change service")
    return r


@pytest.fixture
def reviewed(repo):
    run, _ = review(repo, fake())
    return repo, run.policy.report[0].stable_id


def test_respond_wrong_dismisses(reviewed):
    repo, sid = reviewed
    res = respond(repo, "F1", "wrong", "by design")
    assert res.stable_id == sid and res.status == "dismissed" and res.learning_id is None
    fs = load_state(repo).branches[BRANCH].findings[sid]
    assert (fs.status, fs.response, fs.reason) == ("dismissed", "wrong", "by design")


def test_respond_fixed_records_head(reviewed):
    repo, sid = reviewed
    respond(repo, "F1", "fixed")
    fs = load_state(repo).branches[BRANCH].findings[sid]
    assert fs.status == "addressed"
    assert fs.addressed_sha == gitutil.rev_parse("HEAD", repo)


def test_respond_by_stable_id(reviewed):
    repo, sid = reviewed
    assert respond(repo, sid, "intentional", "ok").status == "dismissed"
    assert load_state(repo).branches[BRANCH].findings[sid].response == "intentional"


def test_unknown_ref_raises_and_cli_returns_1(reviewed, monkeypatch, capsys):
    repo, _ = reviewed
    with pytest.raises(PreflightError):
        respond(repo, "F9", "wrong")
    monkeypatch.chdir(repo)
    assert cli.main(["respond", "F9", "wrong"]) == 1
    assert "F9" in capsys.readouterr().err


def test_invalid_label_and_missing_state_raise(reviewed, tmp_path):
    repo, _ = reviewed
    with pytest.raises(PreflightError):
        respond(repo, "F1", "bogus")
    (tmp_path / "o").mkdir()
    other = make_repo(tmp_path / "o", {"a.py": "x = 1\n"})
    with pytest.raises(PreflightError):
        respond(other, "F1", "wrong")


def test_respond_learn_appends_learning(reviewed):
    repo, sid = reviewed
    res = respond(repo, "F1", "wrong", "by design", learn=True)
    assert res.learning_id == "L1" and "committed" in res.message
    text = (repo / ".preflight/learnings.md").read_text()
    assert re.search(rf"- id: L1 · date: .* · path: app/\*\* · from: F1 \({sid}\)", text)
    [l] = read_learnings(repo, None)
    assert l.text.startswith("Do not flag:") and "by design" in l.text


def test_responses_jsonl_line(reviewed):
    repo, sid = reviewed
    respond(repo, "F1", "didnt_matter", "meh")
    lines = (repo / ".preflight/responses.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["stable_id"] == sid and rec["run_id"] == "F1" and rec["label"] == "didnt_matter"
    assert rec["reason"] == "meh" and rec["branch"] == BRANCH and rec["report"]


def test_dismissed_finding_is_previously_reported(reviewed):
    repo, sid = reviewed
    respond(repo, "F1", "wrong", "by design")
    run, _ = review(repo, fake(), full=True)
    assert (sid, "dismissed") in run.policy.previously_reported
    assert all(f.stable_id != sid for f in run.policy.report)


def test_learning_on_base_suppresses_renamed_finding(reviewed):
    repo, sid = reviewed
    respond(repo, "F1", "wrong", "by design", learn=True)
    _git(repo, "checkout", "-q", "main")
    commit(repo, {".preflight/learnings.md": (repo / ".preflight/learnings.md").read_text()}, "learnings")
    _git(repo, "checkout", "-q", BRANCH)

    def rename(d):
        d["finder:correctness"]["findings"][0]["title"] = "Order total gets discount applied twice"

    run, _ = review(repo, fake(rename), full=True)
    assert [lid for _f, lid in run.suppressed] == ["L1"]
    assert run.suppressed[0][0].stable_id != sid
    assert run.policy.report == []


def test_learn_appends_and_increments(repo):
    a = learn(repo, "Do not flag X", path_glob="app/**")
    b = learn(repo, "Do not flag Y")
    assert (a.id, b.id) == ("L1", "L2")
    assert a.source_finding is None and b.path_glob == "**"
    assert [l.id for l in list_learnings(repo)] == ["L1", "L2"]


def test_learn_from_finding(reviewed):
    repo, sid = reviewed
    assert learn(repo, "Do not flag it", from_ref="F1").source_finding == f"F1 ({sid})"


def test_learn_list_cli(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["learn", "--list"]) == 0
    assert capsys.readouterr().out.strip() == "no learnings"
    assert cli.main(["learn", "Do not flag X", "--path", "app/**"]) == 0
    capsys.readouterr()
    assert cli.main(["learn", "--list"]) == 0
    assert capsys.readouterr().out.strip() == "L1  app/**  Do not flag X"


def test_learn_without_text_raises(repo, monkeypatch):
    with pytest.raises(PreflightError):
        learn(repo, "  ")
    monkeypatch.chdir(repo)
    assert cli.main(["learn"]) == 1
