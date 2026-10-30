"""Triage, walkthrough, manifest and incremental wiring (fake backend only)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from preflight_review.config import Config
from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.scope import resolve_scope
from preflight_review.stages.triage import triage
from preflight_review.stages.walkthrough import walkthrough
from preflight_review.stages.policy import PolicyResult

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
EMPTY = {"findings": [], "files_read_outside_packet": []}
HAIKU_CFG = "version: 1\ntriage:\n  haiku: true\n"
WT = {
    "summary": "Reworks pricing helpers.",
    "changes_by_theme": [{"theme": "Pricing", "files": ["a.py"], "description": "Changes the rate maths."}],
    "effort_1_to_5": 2,
    "merge_risk": "Low",
    "merge_risk_sentence": "Small and local.",
}
A_BASE = "def f(x):\n    return x + 1\n"
B_BASE = "def g(y):\n    return y * 2\n"


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def two_file_repo(tmp_path, cfg=HAIKU_CFG, a_new="def f(x):\n    return x + 2\n", b_new="def g(y):\n    return y * 3\n"):
    r = make_repo(tmp_path, {"a.py": A_BASE, "b.py": B_BASE, ".preflight/config.yaml": cfg})
    branch(r, "feature/x")
    commit(r, {"a.py": a_new, "b.py": b_new}, "change both")
    return r


def haiku_by_path(approved: str):
    def fn(req):
        ok = f"File: {approved}\n" in req.prompt
        return {"decision": "APPROVED" if ok else "NEEDS_REVIEW", "reason": "typo"}

    return fn


def test_whitespace_only_approved_without_model_call(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\n", a_new="def f(x):\n        return   x+1\n")
    scope = resolve_scope(r, "main")
    be = FakeBackend()
    res = triage(scope, Config(), be)
    assert res.per_file == {"a.py": "APPROVED", "b.py": "NEEDS_REVIEW"}
    assert res.notes["a.py"] == "whitespace-only"
    assert be.calls == [] and not res.skip_llm


def test_whitespace_only_not_sent_to_haiku(tmp_path):
    r = two_file_repo(tmp_path, a_new="def f(x):\n        return   x+1\n")
    scope = resolve_scope(r, "main")
    cfg = Config()
    cfg.triage.haiku = True
    be = FakeBackend({"triage": {"decision": "NEEDS_REVIEW", "reason": "logic"}})
    res = triage(scope, cfg, be)
    assert res.per_file["a.py"] == "APPROVED"
    assert [c.role for c in be.calls] == ["triage"] and "File: b.py" in be.calls[0].prompt
    assert list(res.lane_results) == ["triage:b.py"]


def test_haiku_triage_drops_approved_file_from_packet(tmp_path):
    r = two_file_repo(tmp_path)
    be = FakeBackend({"triage": haiku_by_path("a.py")}, default=EMPTY)
    run, paths = review(r, be)
    assert run.triaged_out == ["a.py"]
    finder = [c for c in be.calls if c.role == "finder:correctness"]
    assert finder and "return y * 3" in finder[0].system_prompt and "return x + 2" not in finder[0].system_prompt
    assert "a.py — triaged as trivial" in paths["md"].read_text()
    assert run.cost_usd > 0 and "triage:a.py" in run.lane_results


def test_haiku_failure_keeps_needs_review(tmp_path):
    r = two_file_repo(tmp_path)
    be = FakeBackend({"triage": BackendError("down")}, default=EMPTY)
    run, paths = review(r, be)
    assert run.triaged_out == []
    finder = [c for c in be.calls if c.role == "finder:correctness"][0]
    assert "return x + 2" in finder.system_prompt and "return y * 3" in finder.system_prompt
    assert run.outcome == "complete"


def test_haiku_invalid_decision_is_needs_review(tmp_path):
    r = two_file_repo(tmp_path)
    scope = resolve_scope(r, "main")
    cfg = Config()
    cfg.triage.haiku = True
    res = triage(scope, cfg, FakeBackend({"triage": {"decision": "MAYBE", "reason": "?"}}))
    assert set(res.per_file.values()) == {"NEEDS_REVIEW"}


def test_all_trivial_skips_models(tmp_path):
    r = two_file_repo(tmp_path, a_new="def f(x):\n    return  x + 1\n", b_new="def g(y):\n  return y*2\n")
    be = FakeBackend()
    run, paths = review(r, be)
    assert be.calls == [] and run.outcome == "complete"
    assert any("model stages skipped" in n for n in run.notes)
    md = paths["md"].read_text()
    assert "a.py — triaged as trivial" in md and "b.py — triaged as trivial" in md


def test_walkthrough_rendered(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\nwalkthrough: true\n")
    be = FakeBackend({"walkthrough": WT}, default=EMPTY)
    run, paths = review(r, be)
    md = paths["md"].read_text()
    assert "## Walkthrough" in md and "Reworks pricing helpers." in md
    assert "- **Pricing** (a.py): Changes the rate maths." in md
    assert "Effort: 2/5" in md and "Model merge risk: Low — Small and local." in md
    assert run.walkthrough == WT and "walkthrough" in run.lane_results
    assert [c for c in be.calls if c.role == "walkthrough"][0].tools == []


def test_walkthrough_failure_is_none_and_complete(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\nwalkthrough: true\n")
    run, paths = review(r, FakeBackend({"walkthrough": BackendError("x")}, default=EMPTY))
    assert run.walkthrough is None and run.outcome == "complete"
    assert "## Walkthrough\n\nnone\n" in paths["md"].read_text()


def test_walkthrough_stage_never_raises(tmp_path):
    r = two_file_repo(tmp_path)
    scope = resolve_scope(r, "main")

    class Boom:
        def run(self, req):
            raise RuntimeError("boom")

    assert walkthrough(scope, PolicyResult(), Config(), Boom()) == (None, None)


def test_no_walkthrough_option_skips_call(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\nwalkthrough: true\n")
    be = FakeBackend({"walkthrough": WT}, default=EMPTY)
    run, _ = review(r, be, walkthrough=False)
    assert run.walkthrough is None
    assert all(c.role != "walkthrough" for c in be.calls)


def test_manifest_has_files_read_outside_packet(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\n")
    be = FakeBackend(
        {"finder:correctness": {"findings": [], "files_read_outside_packet": ["lib/x.py"]},
         "security": {"findings": [], "files_read_outside_packet": ["lib/y.py"]}},
        default=EMPTY,
    )
    run, paths = review(r, be)
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["files_read_outside_packet"]["correctness"] == ["lib/x.py"]
    assert manifest["files_read_outside_packet"]["security"] == ["lib/y.py"]
    assert run.files_read_outside_packet["correctness"] == ["lib/x.py"]


def test_incremental_lists_earlier_findings(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "other.py": "x = 1\n"})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change service")
    first, _ = review(r, FakeBackend.from_file(FIXTURES / "run1.json"))
    sid = first.policy.report[0].stable_id
    commit(r, {"other.py": "x = 2\n"}, "touch other")
    run, paths = review(r, FakeBackend(default=EMPTY), incremental=True)
    assert (sid, "open") in run.policy.previously_reported
    assert f"- {sid}: open" in paths["md"].read_text()


def test_all_lanes_failed_is_incomplete(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\n")
    run, _ = review(r, FakeBackend({}, default=None))
    assert run.outcome == "incomplete" and "all lanes failed" in run.incomplete_reasons
