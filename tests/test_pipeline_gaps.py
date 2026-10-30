"""G1-G5 pipeline wiring: risk-ordered budget stop, honest counts, dropped/raw persistence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from preflight_review import pipeline
from preflight_review.model import FakeBackend
from preflight_review.pipeline import packet_for_finding

from .conftest import branch, commit, make_repo
from .test_coverage import EMPTY, no_gh, review

SRC = [f"src/app/mod_{i}.py" for i in range(4)]


def _body(i: int, new: bool) -> str:
    rows = []
    for j in range(8):
        rows.append(f"def fn_{i}_{j}(value):")
        rows.append(f"    return value + {j}{'0' if new else ''}  # padding text for bulk {i}")
    return "\n".join(rows) + "\n"


NON_SRC = {
    "docs/a.md": "a\n", "eval/x.json": '{"a": 1}\n', ".claude-plugin/p.json": '{"a": 1}\n',
    "tests/test_m.py": "def test_m():\n    assert 1\n",
}


@pytest.fixture
def mixed_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 300)
    base = {p: _body(i, False) for i, p in enumerate(SRC)}
    base.update(NON_SRC)
    r = make_repo(tmp_path, base)
    branch(r, "feature/mixed")
    new = {p: _body(i, True) for i, p in enumerate(SRC)}
    new.update({
        "docs/a.md": "a changed\n", "eval/x.json": '{"a": 2}\n',
        ".claude-plugin/p.json": '{"a": 2}\n', "tests/test_m.py": "def test_m():\n    assert 2\n",
    })
    commit(r, new, "change")
    return r


def test_budget_stop_reviews_source_first(mixed_repo):
    run, paths = review(mixed_repo, FakeBackend(default=EMPTY), budget_usd=0.08)
    assert run.outcome == "incomplete"
    assert run.reviewed_paths
    assert all(p.startswith("src/") for p in run.reviewed_paths)
    nr = {p for p, _ in run.not_reviewed}
    assert nr
    assert not any(p in nr for p in run.reviewed_paths)
    assert {"docs/a.md", "eval/x.json", ".claude-plugin/p.json"} <= nr


def test_incomplete_run_is_honest_and_counts_match(mixed_repo):
    import json as _json

    run, paths = review(mixed_repo, FakeBackend(default=EMPTY), budget_usd=0.08)
    md = paths["md"].read_text()
    assert "Merge risk: unknown" in md
    data = _json.loads(paths["json"].read_text())
    assert data["merge_risk"] == "unknown"
    sc = data["scope"]
    assert sc["reviewed"] + len(sc["not_reviewed"]) == sc["total"]
    reason = next(r for r in data["incomplete_reasons"] if r.startswith("coverage:"))
    assert f"coverage: {len(sc['not_reviewed'])} of {sc['total']} " in reason


def test_dropped_and_raw_output_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    r = make_repo(tmp_path, {"app/service.py": _body(1, False)})
    branch(r, "feature/x")
    commit(r, {"app/service.py": _body(1, True)}, "change")
    bad = {
        "file": "nope/missing.py", "start_line": 1, "end_line": 1, "caused_by": None,
        "lane": "inline", "category": "correctness", "severity": "major",
        "effort": "quick_win", "title": "ghost", "claim": "c", "failure_scenario": "f",
        "why_it_matters": "w", "suggested_fix": "s",
        "evidence": [{"file": "nope/missing.py", "line": 1, "quote": "x"}],
        "bug_class_instances": [], "rule_quote": None,
    }
    be = FakeBackend(
        {"finder:correctness": {"findings": [bad], "files_read_outside_packet": []}},
        default=EMPTY,
    )
    run, paths = review(r, be)
    dropped = paths["md"].parent / (paths["md"].stem + ".dropped.jsonl")
    assert dropped.exists()
    recs = [json.loads(ln) for ln in dropped.read_text().splitlines() if ln.strip()]
    assert any(x["stage"] == "finder" for x in recs)
    assert "Dropped:" in paths["md"].read_text()
    raw = r / ".preflight" / "runs" / run.basename / "raw" / "correctness@b1.json"
    assert raw.exists()
    assert json.loads(raw.read_text())["role"].startswith("finder:correctness")


def _entry(label, start, length):
    hunk = SimpleNamespace(new_start=start, new_len=length)
    fc = SimpleNamespace(path="a.py", hunks=[hunk])
    return (label, SimpleNamespace(files=[fc]))


def test_packet_for_finding_picks_covering_batch():
    entries = [_entry("p1", 1, 10), _entry("p2", 50, 10)]
    f = SimpleNamespace(file="a.py", start_line=55)
    assert packet_for_finding(f, entries) == "p2"
    assert packet_for_finding(SimpleNamespace(file="a.py", start_line=999), entries) == "p1"
    assert packet_for_finding(f, []) is None
    assert packet_for_finding(f, None) is None
