from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from preflight_review.findings import Category, Effort, Finding, Severity, Verdict
from preflight_review.render import write_outputs
from preflight_review.render.json_out import render_json
from preflight_review.render.report import coverage, dropped_records, render_report
from preflight_review.scope import resolve_scope
from preflight_review.stages.policy import PolicyResult
from preflight_review.naming import report_paths

from .conftest import branch, commit, make_repo
from .test_render import _f, _run


@pytest.fixture
def scope(tmp_path):
    repo = make_repo(tmp_path, {"app/x.py": "a = 1\n"})
    branch(repo, "feature/x")
    commit(repo, {"app/x.py": "a = 1\nb = 2\n", "app/y.py": "c = 3\n"}, "change")
    return resolve_scope(repo, "main")


def _scope(scope, paths, filtered=()):
    return replace(scope, files=[replace(scope.files[0], path=p) for p in paths], filtered_out=list(filtered))


def test_incomplete_merge_risk_unknown(scope):
    sc = _scope(scope, ["src/a.py", "docs/b.md", "src/c.py"])
    w = {"summary": "adds x", "merge_risk": "Low", "merge_risk_sentence": "tiny harmless change"}
    run = _run(sc, policy=PolicyResult(), walkthrough=w, outcome="incomplete",
               incomplete_reasons=["coverage: 2 of 3"], reviewed_paths=["src/c.py"],
               not_reviewed=[("src/a.py", "budget"), ("docs/b.md", "budget")])
    md = render_report(run)
    assert "Merge risk: unknown — review incomplete (2 of 3 files not reviewed, incl. 1 source files)" in md
    assert "Merge risk: low" not in md
    assert "tiny harmless change" not in md and "Model merge risk: suppressed" in md
    assert "(Partial: describes only the reviewed part of the change.) adds x" in md
    d = render_json(run)
    assert d["merge_risk"] == "unknown" and d["walkthrough"]["merge_risk"] == "unknown"
    assert d["walkthrough"]["model_merge_risk_suppressed"]["merge_risk"] == "Low"
    assert w["merge_risk"] == "Low"  # input not mutated


def test_incomplete_without_unreviewed_uses_reasons(scope):
    run = _run(scope, policy=PolicyResult(), outcome="incomplete", incomplete_reasons=["verifier failed"])
    assert "Merge risk: unknown — review incomplete (verifier failed)" in render_report(run)


def test_complete_run_keeps_level(scope):
    run = _run(scope, policy=PolicyResult(), outcome="complete")
    assert "Merge risk: low" in render_report(run)
    assert render_json(run)["merge_risk"] == "low"


def test_count_invariant(scope):
    paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    sc = _scope(scope, paths, filtered=["uv.lock"])
    run = _run(sc, reviewed_paths=["a.py"], triaged_out=["b.py"], not_reviewed=[("c.py", "lost")])
    cov = coverage(sc, run.reviewed_paths, run.not_reviewed, run.triaged_out, run.triage_notes)
    assert len(cov.reviewed) + len(cov.not_reviewed) == cov.total == 5
    md = render_report(run)
    assert "reviewed 1/5, not reviewed: 4 (see Not reviewed)" in md
    assert "## Not reviewed (4)" in md
    assert "- e.py — not reviewed (no reason recorded)" in md
    assert "- uv.lock" not in md and "Excluded by path filters (1): uv.lock" in md
    js = render_json(run)["scope"]
    assert js["reviewed"] + len(js["not_reviewed"]) == js["total"] == 5
    assert js["excluded_by_path_filter"] == ["uv.lock"] == js["unreviewed"]
    assert all(e["path"] != "uv.lock" for e in js["not_reviewed"])


def test_risk_tag_and_source_first(scope):
    sc = _scope(scope, ["docs/guide.md", "src/app.py"])
    run = _run(sc, reviewed_paths=[], not_reviewed=[("docs/guide.md", "budget"), ("src/app.py", "budget")])
    md = render_report(run)
    assert "- src/app.py — budget [risk: source]" in md and "- docs/guide.md — budget [risk: docs]" in md
    assert md.index("src/app.py —") < md.index("docs/guide.md —")
    assert "1 source, 1 docs/fixtures" in md
    assert [e["path"] for e in render_json(run)["scope"]["not_reviewed"]][0] == "src/app.py"


def test_dropped_jsonl(scope, tmp_path):
    refuted = _f(stable_id="r1", title="Refuted one", verdict=Verdict.REFUTED)
    pre = _f(stable_id="p1", title="Prefiltered")
    pol = _f(stable_id="n1", title="Nit")
    pr = PolicyResult(dropped=[(refuted, "refuted by verifier"), (pol, "below profile")])
    fd = {"stage": "finder", "lane": "correctness", "batch": 0, "reason": "schema", "candidate": {"title": "bad"}}
    run = _run(scope, policy=pr, finder_dropped=[fd], prefilter_dropped=[(pre, "no evidence")])
    recs = dropped_records(run)
    assert [r["stage"] for r in recs] == ["finder", "prefilter", "verifier", "policy"]
    paths = report_paths(scope.repo_root / ".preflight" / "reviews", run.basename) if False else {
        k: tmp_path / v for k, v in {"md": "r/rev-01.md", "json": "r/rev-01.json",
                                     "manifest": "r/rev-01.manifest.json", "latest": "r/latest.md"}.items()}
    out = write_outputs(run, paths)
    lines = out["dropped"].read_text().splitlines()
    assert out["dropped"].name == "rev-01.dropped.jsonl" and len(lines) == 4
    assert all(isinstance(json.loads(x), dict) for x in lines)
    md = out["md"].read_text()
    assert f"Dropped: 4 (see {run.basename}.dropped.jsonl)" in md
