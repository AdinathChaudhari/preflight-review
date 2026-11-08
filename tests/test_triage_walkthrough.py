"""Triage, walkthrough, manifest and incremental wiring (fake backend only)."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from preflight_review.config import Config
from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.scope import resolve_scope
from preflight_review.stages.policy import PolicyResult
from preflight_review.stages.triage import triage
from preflight_review.stages.walkthrough import walkthrough

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x.py", "symbol_or_hunk": "f", "inputs_considered": "none", "result": "no issue"}],
}
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


def listed_paths(req):
    return re.findall(r"^### (.+)$", req.prompt, flags=re.MULTILINE)


def haiku_by_path(*approved: str):
    def fn(req):
        return {"decisions": [
            {"path": p, "decision": "APPROVED" if p in approved else "NEEDS_REVIEW", "reason": "typo"}
            for p in listed_paths(req)
        ]}

    return fn


def all_decision(decision: str):
    return lambda req: {"decisions": [{"path": p, "decision": decision, "reason": "r"} for p in listed_paths(req)]}


def test_whitespace_only_approved_without_model_call(tmp_path):
    r = two_file_repo(tmp_path, cfg="version: 1\n", a_new="def f(x):\n        return   x+1\n")
    scope = resolve_scope(r, "main")
    be = FakeBackend()
    res = triage(scope, Config(), be)
    assert res.per_file == {"a.py": "APPROVED", "b.py": "NEEDS_REVIEW"}
    assert res.notes["a.py"] == "whitespace-only"
    # Haiku triage is on by default: only the undecided file gets a call.
    assert [c.model for c in be.calls] == ["haiku"] and "### b.py" in be.calls[0].prompt and "### a.py" not in be.calls[0].prompt
    assert not res.skip_llm


def test_whitespace_only_not_sent_to_haiku(tmp_path):
    r = two_file_repo(tmp_path, a_new="def f(x):\n        return   x+1\n")
    scope = resolve_scope(r, "main")
    cfg = Config()
    cfg.triage.haiku = True
    be = FakeBackend({"triage": all_decision("NEEDS_REVIEW")})
    res = triage(scope, cfg, be)
    assert res.per_file["a.py"] == "APPROVED"
    assert [c.role for c in be.calls] == ["triage"] and "### b.py" in be.calls[0].prompt and "### a.py" not in be.calls[0].prompt
    assert list(res.lane_results) == ["triage:g1"]


def test_haiku_triage_drops_approved_file_from_packet(tmp_path):
    r = two_file_repo(tmp_path)
    be = FakeBackend({"triage": haiku_by_path("a.py")}, default=EMPTY)
    run, paths = review(r, be)
    assert run.triaged_out == ["a.py"]
    finder = [c for c in be.calls if c.role == "finder:correctness"]
    assert finder and "return y * 3" in finder[0].system_prompt and "return x + 2" not in finder[0].system_prompt
    assert "a.py — triaged as trivial" in paths["md"].read_text()
    assert run.cost_usd > 0 and "triage:g1" in run.lane_results


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
    res = triage(scope, cfg, FakeBackend({"triage": lambda req: {"decisions": [{"path": p, "decision": "MAYBE", "reason": "?"} for p in listed_paths(req)]}}))
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


def test_haiku_never_approves_doc_files_unless_docs_review_off(tmp_path):
    r = make_repo(tmp_path, {"a.py": A_BASE, "PLAN.md": "8 backups\n"})
    branch(r, "feature/x")
    commit(r, {"a.py": "def f(x):\n    return x + 2\n", "PLAN.md": "7 backups\n"}, "c")
    scope = resolve_scope(r, "main")
    cfg = Config()
    cfg.triage.haiku = True
    be = FakeBackend({"triage": all_decision("APPROVED")})
    res = triage(scope, cfg, be)
    assert res.per_file == {"a.py": "APPROVED", "PLAN.md": "NEEDS_REVIEW"}
    assert [c.prompt for c in be.calls if "### PLAN.md" in c.prompt] == []
    cfg.docs_review = "off"
    res = triage(scope, cfg, FakeBackend({"triage": all_decision("APPROVED")}))
    assert res.per_file["PLAN.md"] == "APPROVED"


def test_workflow_change_does_not_send_unrelated_docs_past_haiku(tmp_path):
    cfg = "version: 1\ndocs_review: \"off\"\ntriage:\n  haiku: true\n"
    r = make_repo(tmp_path, {
        "a.py": A_BASE, "PLAN.md": "8 backups\n", ".github/workflows/ci.yml": "on: push\n",
        ".preflight/config.yaml": cfg,
    })
    branch(r, "feature/x")
    commit(r, {"PLAN.md": "7 backups\n", ".github/workflows/ci.yml": "on: [push]\n"}, "c")
    be = FakeBackend({"triage": haiku_by_path("PLAN.md")}, default=EMPTY)
    run, _ = review(r, be)
    assert any("### PLAN.md" in c.prompt for c in be.calls if c.role == "triage")
    assert "PLAN.md" in run.triaged_out


# --- batched triage ---------------------------------------------------------

def many_files_repo(tmp_path, n, body=lambda i: f"def f{i}(x):\n    return x + 2\n"):
    base = {f"m{i:03d}.py": f"def f{i}(x):\n    return x + 1\n" for i in range(n)}
    r = make_repo(tmp_path, base)
    branch(r, "feature/x")
    commit(r, {f"m{i:03d}.py": body(i) for i in range(n)}, "change all")
    return r


def haiku_cfg(**kw):
    cfg = Config()
    cfg.triage.haiku = True
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_45_files_make_three_calls(tmp_path):
    r = many_files_repo(tmp_path, 45)
    be = FakeBackend({"triage": all_decision("NEEDS_REVIEW")})
    res = triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert sorted(len(listed_paths(c)) for c in be.calls) == [5, 20, 20]
    assert list(res.lane_results) == ["triage:g1", "triage:g2", "triage:g3"]
    assert all(c.role == "triage" and c.model == "haiku" and c.tools == [] for c in be.calls)
    assert set(res.per_file.values()) == {"NEEDS_REVIEW"}
    # plan order: g1 holds the first 20 paths
    assert [f"m{i:03d}.py" for i in range(20)] in [listed_paths(c) for c in be.calls]


def test_group_approves_only_cosmetic(tmp_path):
    r = make_repo(tmp_path, {"a.py": A_BASE, "b.py": "x = 1  # tpyo\n"})
    branch(r, "feature/x")
    commit(r, {"a.py": "def f(x):\n    return x + 2\n", "b.py": "x = 1  # typo\n"}, "c")
    be = FakeBackend({"triage": haiku_by_path("b.py")})
    res = triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert len(be.calls) == 1
    assert res.per_file == {"a.py": "NEEDS_REVIEW", "b.py": "APPROVED"}
    assert res.notes["b.py"] == "typo" and "a.py" not in res.notes


def test_empty_reason_falls_back_to_haiku_note(tmp_path):
    r = two_file_repo(tmp_path)
    fn = lambda req: {"decisions": [{"path": "a.py", "decision": "APPROVED", "reason": ""}]}
    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": fn}))
    assert res.per_file["a.py"] == "APPROVED" and res.notes["a.py"] == "haiku"


def test_missing_path_is_needs_review(tmp_path):
    r = two_file_repo(tmp_path)
    fn = lambda req: {"decisions": [{"path": "a.py", "decision": "APPROVED", "reason": "ok"}]}
    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": fn}))
    assert res.per_file == {"a.py": "APPROVED", "b.py": "NEEDS_REVIEW"}


def test_unknown_path_ignored(tmp_path):
    r = two_file_repo(tmp_path)
    fn = lambda req: {"decisions": [{"path": "zzz.py", "decision": "APPROVED", "reason": "ok"}]}
    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": fn}))
    assert set(res.per_file.values()) == {"NEEDS_REVIEW"}


@pytest.mark.parametrize("answer", [
    {}, {"decisions": "APPROVED"}, {"decisions": None}, {"decisions": ["a.py"]}, ["a.py"], "APPROVED",
    {"decision": "APPROVED", "reason": "single-file shape"},
])
def test_malformed_structured_is_needs_review(tmp_path, answer):
    r = two_file_repo(tmp_path)
    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": answer}))
    assert set(res.per_file.values()) == {"NEEDS_REVIEW"}


def test_duplicate_path_is_needs_review(tmp_path):
    r = two_file_repo(tmp_path)
    fn = lambda req: {"decisions": [
        {"path": "a.py", "decision": "APPROVED", "reason": "ok"},
        {"path": "a.py", "decision": "NEEDS_REVIEW", "reason": "no"},
        {"path": "b.py", "decision": "APPROVED", "reason": "ok"},
    ]}
    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": fn}))
    assert res.per_file == {"a.py": "NEEDS_REVIEW", "b.py": "APPROVED"}


def test_failed_group_does_not_affect_others(tmp_path):
    r = many_files_repo(tmp_path, 25)

    def fn(req):
        if "### m000.py" in req.prompt:
            return BackendError("down")
        return all_decision("APPROVED")(req)

    be = FakeBackend({"triage": fn})
    res = triage(resolve_scope(r, "main"), haiku_cfg(), be)
    first = {f"m{i:03d}.py" for i in range(20)}
    assert {p for p, d in res.per_file.items() if d == "NEEDS_REVIEW"} == first
    assert all(d == "APPROVED" for p, d in res.per_file.items() if p not in first)


def test_raising_backend_is_needs_review(tmp_path):
    r = two_file_repo(tmp_path)

    class Boom:
        def run(self, req):
            raise RuntimeError("boom")

    res = triage(resolve_scope(r, "main"), haiku_cfg(), Boom())
    assert set(res.per_file.values()) == {"NEEDS_REVIEW"} and res.lane_results == {}


def test_char_cap_splits_group(tmp_path, monkeypatch):
    from preflight_review.stages import triage as tmod

    r = many_files_repo(tmp_path, 4)
    monkeypatch.setattr(tmod, "GROUP_MAX_CHARS", 60)
    be = FakeBackend({"triage": all_decision("NEEDS_REVIEW")})
    triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert len(be.calls) > 1
    assert sorted(p for c in be.calls for p in listed_paths(c)) == [f"m{i:03d}.py" for i in range(4)]


def test_oversized_file_gets_own_group():
    from types import SimpleNamespace

    from preflight_review.stages.triage import GROUP_MAX_CHARS, _groups

    fcs = [SimpleNamespace(path=p) for p in "abc"]
    items = [(fcs[0], "x" * 10), (fcs[1], "y" * (GROUP_MAX_CHARS + 1)), (fcs[2], "z" * 10)]
    assert [[fc.path for fc, _ in g] for g in _groups(items)] == [["a"], ["b"], ["c"]]


def test_diff_with_backticks_cannot_break_fence():
    from preflight_review.stages.triage import _fenced

    out = _fenced("+x = '```'\n+y")
    assert out.startswith("````diff\n") and out.endswith("\n````")


def test_group_answers_in_reverse_order_still_deterministic(tmp_path):
    r = many_files_repo(tmp_path, 45)
    lock = threading.Lock()
    done = []

    def fn(req):
        first = listed_paths(req)[0]
        # Later groups finish first.
        time.sleep({"m000.py": 0.15, "m020.py": 0.08, "m040.py": 0.0}[first])
        with lock:
            done.append(first)
        return haiku_by_path("m000.py", "m020.py", "m040.py")(req)

    res = triage(resolve_scope(r, "main"), haiku_cfg(), FakeBackend({"triage": fn}))
    assert done == ["m040.py", "m020.py", "m000.py"]
    assert list(res.per_file) == [f"m{i:03d}.py" for i in range(45)]
    assert list(res.lane_results) == ["triage:g1", "triage:g2", "triage:g3"]
    assert [p for p, d in res.per_file.items() if d == "APPROVED"] == ["m000.py", "m020.py", "m040.py"]


def test_prose_docs_never_sent_unless_docs_review_off(tmp_path):
    r = make_repo(tmp_path, {"a.py": A_BASE, "PLAN.md": "8 backups\n"})
    branch(r, "feature/x")
    commit(r, {"a.py": "def f(x):\n    return x + 2\n", "PLAN.md": "7 backups\n"}, "c")
    be = FakeBackend({"triage": all_decision("APPROVED")})
    triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert all("### PLAN.md" not in c.prompt for c in be.calls)
    be = FakeBackend({"triage": all_decision("APPROVED")})
    triage(resolve_scope(r, "main"), haiku_cfg(docs_review="off"), be)
    assert any("### PLAN.md" in c.prompt for c in be.calls)


def test_no_calls_when_all_files_are_prose_docs(tmp_path):
    r = make_repo(tmp_path, {"a.py": A_BASE, "PLAN.md": "8 backups\n"})
    branch(r, "feature/x")
    commit(r, {"PLAN.md": "7 backups\n"}, "c")
    be = FakeBackend({"triage": all_decision("APPROVED")})
    res = triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert be.calls == [] and res.per_file == {"PLAN.md": "NEEDS_REVIEW"}


def test_budget_cap_scales_with_group_size(tmp_path):
    r = many_files_repo(tmp_path, 3)
    be = FakeBackend({"triage": all_decision("NEEDS_REVIEW")})
    triage(resolve_scope(r, "main"), haiku_cfg(), be)
    assert be.calls[0].max_budget_usd is None
    be = FakeBackend({"triage": all_decision("NEEDS_REVIEW")})
    triage(resolve_scope(r, "main"), haiku_cfg(budget_usd=2.0), be)
    assert be.calls[0].max_budget_usd == pytest.approx(0.15)
