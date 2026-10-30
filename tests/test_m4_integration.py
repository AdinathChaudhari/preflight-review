"""M4 integration tests: retrieval wiring (learnings cap/rerank, history) and packet determinism."""

from __future__ import annotations

import json
import re
import subprocess

from preflight_review import cli
from preflight_review.context.learnings import Learning, format_learning
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

EMPTY = {"findings": [], "files_read_outside_packet": []}
HEADING = "# Prior review comments in these files"


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def _learnings_text(n=25):
    items = []
    for i in range(1, n + 1):
        text = f"learning number {i} about pricing rule {i}"
        if i == 7:
            text += " and how to handle a claim"
        items.append(format_learning(Learning(f"L{i:03d}", "2026-10-30", "app/**", text, None, 0)))
    return "# Learnings\n\n" + "\n".join(items) + "\n"


def _repo(tmp_path, rerank=True, learnings=True):
    cfg = f"version: 1\nlenses: [correctness]\nsecurity_lane: false\nlearnings:\n  rerank: {str(rerank).lower()}\n  cap: 20\n"
    files = {"app/service.py": SERVICE_BASE, ".preflight/config.yaml": cfg}
    if learnings:
        files[".preflight/learnings.md"] = _learnings_text()
    repo = make_repo(tmp_path, files)
    branch(repo, "feature/x")
    commit(repo, {"app/service.py": SERVICE_BAD}, "change service")
    return repo


def _review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def _finder_prompt(be):
    calls = [c for c in be.calls if c.role == "finder:correctness"]
    assert calls
    return calls[0].system_prompt + "\n" + calls[0].prompt


def _learnings_section(prompt):
    m = re.search(r"# Learnings\n(.*?)(?=\n# |\Z)", prompt, re.S)
    assert m, prompt
    return m.group(1)


def _count_entries(section):
    return len(re.findall(r"^\[L\d+\]", section, re.M))


def test_learn_list_shows_all_25(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cli.main(["learn", "--list"]) == 0
    out = capsys.readouterr().out
    assert len(re.findall(r"L\d{3}", out)) >= 25
    for i in range(1, 26):
        assert f"L{i:03d}" in out


def test_rerank_caps_at_20_and_calls_once(tmp_path):
    repo = _repo(tmp_path, rerank=True)
    ids = [f"L{i:03d}" for i in range(25, 0, -1)]
    be = FakeBackend(responses={"rerank": {"ranked_ids": ids}}, default=EMPTY)
    run, _ = _review(repo, be)
    assert [c.role for c in be.calls].count("rerank") == 1
    sec = _learnings_section(_finder_prompt(be))
    assert _count_entries(sec) == 20
    assert any("learnings: 20 of 25 (cap 20, rerank on)" in n for n in run.notes)


def test_no_rerank_when_disabled(tmp_path):
    repo = _repo(tmp_path, rerank=False)
    be = FakeBackend(default=EMPTY)
    run, _ = _review(repo, be)
    assert "rerank" not in [c.role for c in be.calls]
    assert _count_entries(_learnings_section(_finder_prompt(be))) == 20
    assert any("rerank off" in n for n in run.notes)


def test_history_section_present_only_with_history_file(tmp_path):
    repo = _repo(tmp_path, rerank=False, learnings=False)
    be = FakeBackend(default=EMPTY)
    _review(repo, be)
    assert HEADING not in _finder_prompt(be)

    rec = {
        "repo": "acme/shop", "pr": 12, "comment_id": 99, "created_at": "2026-10-01T00:00:00Z",
        "file": "app/service.py", "start_line": 5, "end_line": 6, "severity": "major",
        "category": "bug", "title": "Discount rate is not validated", "body_excerpt": "rate may exceed 1",
        "is_nitpick": False, "outcome": "resolved", "head_sha": "a" * 40, "base_sha": "b" * 40,
        "agent_prompt": "",
    }
    (repo / ".preflight" / "history.jsonl").write_text(json.dumps(rec) + "\n")
    be2 = FakeBackend(default=EMPTY)
    _review(repo, be2, full=True)
    prompt = _finder_prompt(be2)
    assert HEADING in prompt
    assert "Discount rate is not validated" in prompt


def test_packet_determinism(tmp_path):
    repo = _repo(tmp_path, rerank=False)
    r1, _ = _review(repo, FakeBackend(default=EMPTY), full=True)
    r2, _ = _review(repo, FakeBackend(default=EMPTY), full=True)
    assert r1.packet is not None and r1.packet.sha == r2.packet.sha
