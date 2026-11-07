"""Security lane: trace validation, verdict rule, routing, and e2e with a fake backend."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from preflight_review import prompts
from preflight_review.config import Config
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import SchemaError
from preflight_review.findings import Category, Verdict
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.scope import resolve_scope
from preflight_review.stages.security import (
    run_security_lane,
    security_verdict_rule,
    security_verifier_prompt,
)
from preflight_review.stages.verify import VerdictCache, run_verifier

from .conftest import branch, commit, make_repo

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
API_BASE = "def health():\n    return 'ok'\n"
API_BAD = '''import db


def get_customer(request):
    customer_id = request.args["id"]
    cur = db.cursor()
    cur.execute(f"SELECT * FROM customers WHERE id = {customer_id}")
    return cur.fetchall()
'''


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def fixture_data() -> dict:
    return json.loads((FIXTURES / "security.json").read_text())


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {"app/api.py": API_BASE, "README.md": "hi\n"})
    branch(r, "feature/sec")
    commit(r, {"app/api.py": API_BAD}, "add customer lookup")
    return r


@pytest.fixture
def ctx(repo):
    config = Config(mode="deep")
    scope = resolve_scope(repo, "main")
    index = RepoIndex(repo, cache=False)
    packet = SimpleNamespace(text="PACKET TEXT")
    return config, scope, index, packet


def raw_finding(**over) -> dict:
    f = copy.deepcopy(fixture_data()["security"]["findings"][0])
    f.update(over)
    return f


def lane(ctx, findings=None, **resp):
    config, scope, index, packet = ctx
    payload = {"findings": findings if findings is not None else [], "files_read_outside_packet": []}
    be = FakeBackend({"security": resp.get("security", payload)})
    return run_security_lane(packet, config, be, scope, "SYS", index), be


def test_traceless_finding_dropped(ctx):
    out, _ = lane(ctx, [raw_finding(trace=[])])
    assert out.findings == []
    assert out.dropped[0][1] == "no_trace"


def test_hop_missing_line_dropped(ctx):
    bad = [{"file": "app/api.py", "note": "x"}]
    out, _ = lane(ctx, [raw_finding(trace=bad)])
    assert out.findings == [] and out.dropped[0][1] == "no_trace"


def test_traced_finding_kept(ctx):
    out, be = lane(ctx, [raw_finding()])
    assert len(out.findings) == 1, out.dropped
    f = out.findings[0]
    assert f.source == "llm:security" and f.category == Category.security
    assert len(f.trace) == 2 and f.confidence == 0.92
    assert "OR 1=1" in f.failure_scenario  # exploit_scenario mapped, no SchemaError
    assert be.calls[0].role == "security" and be.calls[0].model == ctx[0].models.security
    assert be.calls[0].max_turns == 18 and be.calls[0].effort == "high"  # default mode deep
    assert be.calls[0].max_budget_usd is None


def test_deep_mode_turns(ctx):
    config, scope, index, packet = ctx
    config.mode = "deep"
    be = FakeBackend({"security": {"findings": [], "files_read_outside_packet": []}})
    run_security_lane(packet, config, be, scope, "SYS", index)
    assert be.calls[0].max_turns == 18
    config.mode, config.budget_usd = "standard", 1.5
    be = FakeBackend({"security": {"findings": [], "files_read_outside_packet": []}})
    run_security_lane(packet, config, be, scope, "SYS", index)
    assert be.calls[0].max_turns == 8 and be.calls[0].effort == "medium"
    assert be.calls[0].max_budget_usd == pytest.approx(0.5)


@pytest.mark.parametrize(
    "conf,verdict,expected",
    [(7, "CONFIRMED", Verdict.UNKNOWN), (8, "CONFIRMED", Verdict.CONFIRMED),
     (3, "REFUTED", Verdict.REFUTED), (9, "UNKNOWN", Verdict.UNKNOWN)],
)
def test_verdict_rule(conf, verdict, expected):
    assert security_verdict_rule({"verdict": verdict, "confidence_10": conf}) == expected


def test_lane_failure_marks_incomplete(ctx):
    config, scope, index, packet = ctx
    be = FakeBackend.from_file(_write(scope.repo_root.parent, {"security": {"__error__": "down"}}))
    out = run_security_lane(packet, config, be, scope, "SYS", index)
    assert out.incomplete == ["security"] and not out.lane_results["security"].ok


def _write(d: Path, data: dict) -> Path:
    p = Path(d) / "fake_err.json"
    p.write_text(json.dumps(data))
    return p


def test_prompts_render():
    text = prompts.render("security")
    assert "Medium-confidence is fine — a separate adjudication pass will filter; your job is RECALL" in text
    assert "pickle.loads" in text and "{{" not in text
    raw = prompts.load_prompt("verifier_security")
    assert raw.count("HARD EXCLUSIONS") == 1
    assert "{{" not in prompts.render("verifier_security", finding_json="{}")
    with pytest.raises(SchemaError):
        prompts.render("verifier_security")


def test_verifier_routes_security_to_sonnet(ctx, repo):
    config, scope, index, packet = ctx
    out, _ = lane(ctx, [raw_finding()])
    be = FakeBackend({"verifier:security": fixture_data()["verifier:security"]})
    vout = run_verifier(out.findings, packet, config, be, scope, VerdictCache(repo), "SYS")
    call = be.calls[0]
    assert call.role == "verifier:security" and call.model == config.models.verifier == "sonnet"
    assert vout.findings[0].verdict == Verdict.CONFIRMED


def test_verifier_downgrades_low_confidence(ctx, repo):
    config, scope, index, packet = ctx
    out, _ = lane(ctx, [raw_finding()])
    resp = fixture_data()["verifier:security"]
    resp["confidence_10"] = 7
    be = FakeBackend({"verifier:security": resp})
    vout = run_verifier(out.findings, packet, config, be, scope, VerdictCache(repo), "SYS")
    f = vout.findings[0]
    assert f.verdict == Verdict.UNKNOWN and f.verifier_reason.endswith("(confidence_10 < 8)")


def test_security_verifier_prompt_contains_finding(ctx):
    out, _ = lane(ctx, [raw_finding()])
    text = security_verifier_prompt(out.findings[0])
    assert "SQL injection" in text and "PACKET TEXT" not in text


def test_pipeline_e2e_report_has_trace(repo):
    opts = ReviewOptions(repo_root=repo, base="main")
    run, paths = run_review(opts, backend=FakeBackend.from_file(FIXTURES / "security.json"), gh_runner=no_gh)
    md = paths["md"].read_text()
    assert "SQL injection via f-string query on customer lookup" in md
    assert "Trace:" in md
    assert "  1. app/api.py:4 — customer_id arrives from the request query string" in md
    assert run.outcome == "complete"
    assert "security" in run.models_used


def test_security_lane_off_makes_no_call(repo, monkeypatch):
    from preflight_review import pipeline

    monkeypatch.setattr(pipeline, "load_config", lambda *a, **k: Config(security_lane=False))
    be = FakeBackend.from_file(FIXTURES / "security.json")
    run, _ = run_review(ReviewOptions(repo_root=repo, base="main"), backend=be, gh_runner=no_gh)
    assert all(c.role != "security" for c in be.calls)
    assert "security" not in run.models_used


def test_security_lane_suffix_and_system_packet(ctx):
    config, scope, index, packet = ctx
    be = FakeBackend({"security": {"findings": [], "files_read_outside_packet": []}})
    out = run_security_lane(packet, config, be, scope, "SYS", index, lane_suffix="@b2")
    assert set(out.lane_results) == {"security@b2"} and be.calls[0].role == "security"
    assert "PACKET TEXT" in be.calls[0].system_prompt and "PACKET TEXT" not in be.calls[0].prompt


def test_security_verifier_overrides_keep_refuted_classes():
    text = prompts.load_prompt("verifier_security")
    over = text.index("OVERRIDES - these take precedence")
    assert over < text.index("Apply the filtering above")
    for needle in ("Committed secret or environment files", "(E2)", "P3 (environment",
                   "E14 covers", "Wildcard CORS with credentials", "(E7)",
                   "CI, deploy and cluster trust boundaries", "precedents P7 and P12",
                   "Exclusions E6 and E17", "lifts only E6, E17, P7 and P12, not E7",
                   "Credentials inside logged URLs", "P1 (\"logging"):
        assert needle in text[over:], needle
    head = text[:over]  # exclusions and precedents carry distinct labels, so "7" is never ambiguous
    assert "> E7. A lack of hardening" in head and "> P7. Most vulnerabilities in github" in head
    assert "> E17. Insecure documentation" in head and "> 16." not in head
    finder = prompts.load_prompt("security")
    assert "does not cover files committed to the repository" in finder
    assert "together with credentials" in finder


@pytest.mark.parametrize("conf", ["9", 9.0])
def test_string_or_float_confidence_survives_vote_aggregation(ctx, repo, conf):
    from preflight_review.stages.verify import aggregate, score_vote

    out, _ = lane(ctx, [raw_finding()])
    f = out.findings[0]
    resp = fixture_data()["verifier:security"]
    resp["confidence_10"] = conf
    vote = score_vote(f, resp)
    assert vote.verdict == Verdict.CONFIRMED and vote.confidence_10 == 9
    agg = aggregate(f, vote.verdict, [vote], vote.reason)
    assert agg["confidence_10"] == 9
    assert score_vote(f, agg).verdict == Verdict.CONFIRMED  # re-scored aggregate holds
