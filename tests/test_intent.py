"""Intent gathering."""

from __future__ import annotations

import json

from preflight_review.config import Config
from preflight_review.context.intent import gather_intent
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit, make_repo


def _setup(tmp_path, branch_name="feature/ABC-123-fix", subjects=("fixes #42",)):
    repo = make_repo(tmp_path, {"a.py": "1\n"})
    branch(repo, branch_name)
    for i, s in enumerate(subjects):
        commit(repo, {"a.py": f"{i + 2}\n"}, s)
    return repo, resolve_scope(repo, "main")


def test_subjects_newest_first_and_capped(tmp_path):
    repo, scope = _setup(tmp_path, subjects=[f"c{i}" for i in range(35)])
    it = gather_intent(scope, Config())
    assert len(it.commit_subjects) == 30
    assert it.commit_subjects[0] == "c34"


def test_issue_refs_ordered_unique(tmp_path):
    repo, scope = _setup(tmp_path, subjects=["fixes #42", "again #42 and ABC-123"])
    it = gather_intent(scope, Config())
    assert it.issue_refs == ["ABC-123", "#42"]
    assert it.branch == "feature/ABC-123-fix"
    assert gather_intent(scope, Config()).issue_refs == it.issue_refs


def test_intent_md_read(tmp_path):
    repo, scope = _setup(tmp_path)
    assert gather_intent(scope, Config()).intent_md is None
    (repo / ".preflight").mkdir()
    (repo / ".preflight" / "intent.md").write_text("Goal: fix it\n")
    assert gather_intent(scope, Config()).intent_md == "Goal: fix it\n"


def test_gh_runner_not_called_when_issues_disabled(tmp_path):
    repo, scope = _setup(tmp_path)
    calls = []
    it = gather_intent(scope, Config(), gh_runner=lambda *a, **k: calls.append(a))
    assert calls == [] and it.issues == []


def test_gh_runner_called_when_enabled_and_failures_recorded(tmp_path):
    repo, scope = _setup(tmp_path, subjects=["fixes #42", "refs #7"])
    cfg = Config()
    cfg.intent.issues = True
    calls = []

    def runner(args, cwd=None):
        calls.append(args)
        if args[3] == "7":
            raise RuntimeError("not found")
        return json.dumps({"title": "Bug", "body": "details"})

    it = gather_intent(scope, cfg, gh_runner=runner)
    assert [c[3] for c in calls] == ["7", "42"] or [c[3] for c in calls] == ["42", "7"]
    by_ref = {i["ref"]: i for i in it.issues}
    assert by_ref["#42"]["title"] == "Bug"
    assert "error" in by_ref["#7"]


def test_issue_fetch_capped_and_truncated(tmp_path):
    from preflight_review.context.intent import MAX_ISSUE_CHARS, MAX_ISSUES

    repo, scope = _setup(tmp_path, subjects=["fixes #1 #2", "refs #3 #4 #5"])
    cfg = Config()
    cfg.intent.issues = True
    calls = []

    def runner(args, cwd=None):
        calls.append(args[3])
        return json.dumps({"title": "T", "body": "x" * 5000})

    it = gather_intent(scope, cfg, gh_runner=runner)
    assert MAX_ISSUES == 3 and len(calls) == 3 and len(it.issues) == 3
    for issue in it.issues:
        assert len(issue["body"]) == MAX_ISSUE_CHARS + 1 and issue["body"].endswith("…")


def test_short_issue_body_untouched(tmp_path):
    repo, scope = _setup(tmp_path)
    cfg = Config()
    cfg.intent.issues = True
    it = gather_intent(scope, cfg, gh_runner=lambda a, cwd=None: json.dumps({"title": "T", "body": "ok"}))
    assert it.issues[0]["body"] == "ok"
