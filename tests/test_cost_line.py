"""Run section Cost line: calls and USD by stage and by model."""

from __future__ import annotations

from preflight_review.model import ModelResult
from preflight_review.render.cost import (
    calls_by_model,
    calls_by_stage,
    cost_line,
    stage_of,
)


def res(model, usd):
    return ModelResult(ok=True, structured={}, subtype="success", cost_usd=usd, model=model)


RESULTS = {
    "triage:a.py": res("haiku", 0.001),
    "finder:correctness@b1": res("sonnet", 0.10),
    "finder:contracts_tests@b2": res("sonnet", 0.20),
    "security@b1": res("sonnet", 0.15),
    "sweep": res("sonnet", 0.12),
    "verifier:abc#v1": res("sonnet", 0.03),
    "verifier:abc#v2": res("sonnet", 0.03),
    "adjudicator:abc#fallback": res("claude-opus-4-7", 0.40),
    "walkthrough": res("haiku", 0.002),
}


def test_stage_of():
    assert stage_of("finder:correctness#retry@b2") == "finder"
    assert stage_of("verifier:x#v3") == "verify" and stage_of("sweep") == "sweep"


def test_calls_by_stage_and_model():
    assert [s for s, _, _ in calls_by_stage(RESULTS)] == [
        "triage", "finder", "security", "sweep", "verify", "adjudicator", "walkthrough",
    ]
    by_model = {m: (n, round(u, 3)) for m, n, u in calls_by_model(RESULTS)}
    assert by_model == {"haiku": (2, 0.003), "sonnet": (6, 0.63), "opus": (1, 0.4)}


def test_cost_line():
    line = cost_line(1.033, RESULTS)
    assert line.startswith("Cost: USD 1.0330 — by stage: triage 1 call $0.00, finder 2 calls $0.30")
    assert "verify 2 calls $0.06" in line
    assert line.endswith("by model: haiku 2 calls $0.00, sonnet 6 calls $0.63, opus 1 call $0.40")
    assert cost_line(0.0, {}) == "Cost: USD 0.0000"


def test_report_run_section_has_cost_line_and_manifest_breakdown(tmp_path):
    import json

    from tests.conftest import branch, commit, make_repo
    from tests.test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE, backend, review

    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "README.md": "hi\n"})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change service")
    run, paths = review(r, backend())
    md = paths["md"].read_text()
    line = next(x for x in md.splitlines() if x.startswith("- Cost: USD "))
    assert "by stage: " in line and "finder " in line and "by model: " in line
    manifest = json.loads(paths["manifest"].read_text())
    assert sum(v["calls"] for v in manifest["calls_by_stage"].values()) == len(run.lane_results)
    assert set(manifest["calls_by_model"]) <= {"haiku", "sonnet", "opus", "fable", "unknown"}


def test_unverified_nits_counted_apart_from_confirmed():
    from preflight_review.findings import Category, Finding, Lane, Severity, Verdict
    from preflight_review.render.report import n_unverified_nits
    from preflight_review.stages.verify import select_for_verification

    def mk(title, sev, lane=Lane.inline, source="llm:correctness"):
        return Finding(file="a.py", start_line=1, end_line=1, title=title, claim="c",
                       severity=sev, category=Category.correctness, lane=lane, source=source)

    nit, triv, tool = (mk("n", Severity.minor, Lane.nitpick), mk("t", Severity.trivial),
                       mk("r", Severity.trivial, source="ruff"))
    select_for_verification([nit, triv, tool], None)
    assert n_unverified_nits([nit, triv, tool]) == 2  # the ruff hit is tool-verified
    assert tool.verdict == Verdict.SKIPPED and not tool.verifier_reason
