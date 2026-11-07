"""Deep-mode gap sweep: unit behaviour and pipeline wiring."""

from __future__ import annotations

import copy
import json

import pytest

from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import BackendError
from preflight_review.findings import Category, Finding, Lane, Severity
from preflight_review.model import FakeBackend
from preflight_review.scope import resolve_scope
from preflight_review.stages.sweep import known_findings_text, run_sweep, sweep_enabled

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import FIXTURES, SERVICE_BAD, SERVICE_BASE, review

PACKET = Packet(text="PACKET", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="x")

NEW = {
    "file": "app/service.py", "start_line": 9, "end_line": 10, "caused_by": None,
    "lane": "inline", "category": "correctness", "severity": "minor", "effort": "quick_win",
    "title": "total() raises on items without a price", "claim": "KeyError on a missing key.",
    "failure_scenario": "items=[{}] raises KeyError.", "why_it_matters": "w",
    "suggested_fix": "use .get", "bug_class_instances": [], "rule_quote": None,
    "evidence": [{"file": "app/service.py", "line": 10, "quote": "    return sum(...)"}],
}


def mk(title, file="app/service.py", line=3, end=None):
    return Finding(file=file, start_line=line, end_line=end or line, title=title, claim="c",
                   severity=Severity.major, category=Category.correctness,
                   source="llm:correctness", lane=Lane.inline, stable_id=title)


def test_known_findings_text_lists_each_candidate_once():
    text = known_findings_text([mk("B", line=7, end=9), mk("A"), mk("A")])
    assert text.splitlines() == [
        "- app/service.py:3 [correctness] A",
        "- app/service.py:7-9 [correctness] B",
    ]
    assert known_findings_text([]) == "(none)"


def test_sweep_enabled_only_in_deep():
    assert not sweep_enabled(Config())  # unresolved auto: the pipeline resolves it first
    cfg = Config(mode="deep")
    assert sweep_enabled(cfg)
    cfg.sweep = False
    assert not sweep_enabled(cfg)
    assert not sweep_enabled(Config(mode="standard"))


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feat")
    commit(repo, {"app/service.py": SERVICE_BAD}, "svc")
    return resolve_scope(repo, "main"), RepoIndex(repo, cache=False)


def test_run_sweep_lists_known_and_tags_source(env):
    scope, index = env
    b = FakeBackend({"sweep": {"findings": [NEW], "files_read_outside_packet": ["x.py"]}})
    out = run_sweep(PACKET, [mk("Discount applied twice")], Config(mode="deep"), b, scope, "SYS", index,
                    "@b2")
    (call,) = b.calls
    assert call.role == "sweep" and call.model == "sonnet"
    assert call.max_turns == 18 and call.effort == "high" and call.max_budget_usd is None
    assert "app/service.py:3 [correctness] Discount applied twice" in call.prompt
    assert "PACKET" in call.system_prompt and "PACKET" not in call.prompt
    assert [f.source for f in out.findings] == ["llm:sweep"]
    assert set(out.lane_results) == {"sweep@b2"}
    assert out.files_read_outside_packet == {"sweep": ["x.py"]}


def test_run_sweep_failure_is_incomplete(env):
    scope, index = env
    out = run_sweep(PACKET, [], Config(), FakeBackend({"sweep": BackendError("x")}), scope,
                    "SYS", index)
    assert out.incomplete == ["sweep"] and out.findings == []


# --- pipeline -----------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "README.md": "hi\n"})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD, "app/other.py": "y = 2\n"}, "change service")
    return r


def fixture(sweep_findings):
    data = json.loads((FIXTURES / "run1.json").read_text())
    data["sweep"] = {"findings": sweep_findings, "files_read_outside_packet": []}
    return FakeBackend(data)


def test_sweep_new_candidate_is_verified_and_reported(repo):
    be = fixture([NEW])
    run, paths = review(repo, be, mode="deep")
    sweeps = [c for c in be.calls if c.role == "sweep"]
    assert len(sweeps) == 1  # one packet with two files: one sweep, not one per path
    assert "Discount applied twice" in sweeps[0].prompt
    titles = {f.title for f in run.policy.report}
    assert "total() raises on items without a price" in titles
    assert run.outcome == "complete"
    new = next(f for f in run.policy.report if f.source == "llm:sweep")
    assert f"verifier:{new.stable_id}#v1" in run.lane_results
    assert "sweep" in run.lane_results
    assert run.models_used["sweep"] == "sonnet"
    assert "app/service.py" in sweeps[0].prompt and "PACKET" not in sweeps[0].prompt
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["models"]["adjudicator"] == "opus"
    assert (repo / ".preflight" / "runs" / run.basename / "raw" / "sweep@b1.json").exists()


def test_sweep_repeat_of_known_finding_is_dropped(repo):
    data = json.loads((FIXTURES / "run1.json").read_text())
    dup = copy.deepcopy(data["finder:correctness"]["findings"][0])
    be = fixture([dup])
    run, _ = review(repo, be, mode="deep")
    n_verifier = sum(1 for c in be.calls if c.role == "verifier")
    assert n_verifier == 2  # only the original finding's two agreeing votes
    assert len(run.policy.report) == 1
    assert "sweep: 0 new candidate(s) from 1 call over 2 changed file(s)" in run.notes


def test_multi_batch_run_sweeps_once_without_a_packet(tmp_path, monkeypatch):
    from preflight_review import pipeline
    from preflight_review.stages.finders import PACKET_HEADING

    def body(n):
        return "".join(f"def f{i}(x):\n    return x + {i * n}\n" for i in range(40))

    r = make_repo(tmp_path, {"one/a.py": body(1), "two/b.py": body(1), "README.md": "hi\n"})
    branch(r, "feature/x")
    commit(r, {"one/a.py": body(2), "two/b.py": body(3)}, "change")
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 400)
    be = FakeBackend({"sweep": {"findings": [], "files_read_outside_packet": []}},
                     default={"findings": [], "files_read_outside_packet": []})
    run, _ = review(r, be, mode="deep")
    assert run.batches > 1
    sweeps = [c for c in be.calls if c.role == "sweep"]
    assert len(sweeps) == 1  # one per run, not one per batch
    assert PACKET_HEADING not in sweeps[0].system_prompt
    assert "- one/a.py" in sweeps[0].prompt and "- two/b.py" in sweeps[0].prompt
    assert "sweep" in run.lane_results


def test_sweep_skipped_outside_deep(repo):
    be = fixture([NEW])
    run, _ = review(repo, be, mode="standard")
    assert not [c for c in be.calls if c.role == "sweep"]
    assert "sweep" not in run.models_used


def test_sweep_failure_marks_run_incomplete(repo):
    data = json.loads((FIXTURES / "run1.json").read_text())
    data["sweep"] = BackendError("down")
    run, _ = review(repo, FakeBackend(data), mode="deep")
    assert "lane failed: sweep" in run.incomplete_reasons
