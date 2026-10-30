from __future__ import annotations

import pytest

from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.findings import Category, Finding, Lane, Severity, Verdict
from preflight_review.model import FakeBackend
from preflight_review.model.backend import ModelRequest
from preflight_review.errors import BackendError
from preflight_review.scope import resolve_scope
from preflight_review.stages.verify import (
    VerdictCache,
    run_verifier,
    select_for_verification,
    verifier_model,
)
from tests.conftest import branch, commit, make_repo, write

PACKET = Packet(text="PACKET", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="x")


def mk(title="t", sev=Severity.major, cat=Category.correctness, source="llm:correctness",
       file="app/service.py", line=3, sid=None, lane=Lane.inline):
    return Finding(file=file, start_line=line, end_line=line, title=title, claim="c",
                   severity=sev, category=cat, source=source, lane=lane,
                   stable_id=sid or title)


def verdict(v="CONFIRMED", ev=True, adj=None, reason="because"):
    return {"verdict": v, "reason": reason, "severity_adjusted": adj, "confidence": 4,
            "checks_performed": ["read"],
            "evidence": [{"file": "app/service.py", "line": 3, "quote": "q"}] if ev else []}


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feat")
    commit(repo, {"app/service.py": "\n".join(f"x{i} = {i}" for i in range(20)) + "\n"}, "svc")
    scope = resolve_scope(repo, "main")
    cfg = Config()
    cfg.cache_warm_seconds = 0
    return repo, scope, cfg


def run(env, findings, backend, cache=None):
    repo, scope, cfg = env
    return run_verifier(findings, PACKET, cfg, backend, scope, cache or VerdictCache(repo), "SYS")


def test_tier_selection():
    cfg = Config()
    assert verifier_model(mk(), cfg) == "opus"
    assert verifier_model(mk(sev=Severity.minor), cfg) == "sonnet"
    assert verifier_model(mk(cat=Category.maintainability), cfg) == "sonnet"
    cfg.mode = "quick"
    assert verifier_model(mk(), cfg) == "sonnet"


def test_confirmed_with_evidence(env):
    b = FakeBackend({"verifier": verdict()})
    f = mk()
    out = run(env, [f], b)
    assert f.verdict == Verdict.CONFIRMED and f.verifier_reason == "because"
    assert f.verifier_evidence and f.verifier_checks == ["read"]
    assert b.calls[0].role == "verifier" and "PACKET" not in b.calls[0].prompt
    assert "PACKET" in b.calls[0].system_prompt and b.calls[0].system_prompt.startswith("SYS")
    assert out.findings == [f] and "verifier:t" in out.results


def test_confirmed_without_evidence_unknown(env):
    f = mk()
    run(env, [f], FakeBackend({"verifier": verdict(ev=False)}))
    assert f.verdict == Verdict.UNKNOWN and f.verifier_reason.startswith("no evidence quoted; ")


def test_failure_demotes(env):
    f = mk()
    out = run(env, [f], FakeBackend({"verifier": BackendError("boom")}))
    assert f.verdict == Verdict.UNKNOWN and f.severity == Severity.minor
    assert "verifier_failed=True" in f.verifier_reason and out.failures == 1


def test_severity_adjusted_cannot_raise(env):
    f = mk(sev=Severity.minor)
    run(env, [f], FakeBackend({"verifier": verdict(adj="critical")}))
    assert f.severity == Severity.minor
    g = mk(title="g", sev=Severity.major)
    run(env, [g], FakeBackend({"verifier": verdict(adj="minor")}))
    assert g.severity == Severity.minor


def test_cache_hit_and_invalidation(env):
    repo, _, _ = env
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk()], b)
    n = len(b.calls)
    assert (repo / ".preflight/cache/verdicts.json").exists()
    f2 = mk()
    out = run(env, [f2], b, VerdictCache(repo))
    assert len(b.calls) == n and out.cache_hits == 1 and f2.verdict == Verdict.CONFIRMED
    write(repo, "app/service.py", "changed = 1\n")
    out = run(env, [mk()], b, VerdictCache(repo))
    assert len(b.calls) == n + 1 and out.cache_hits == 0


def test_corrupt_cache_is_empty(env):
    repo, _, _ = env
    p = repo / ".preflight/cache/verdicts.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert VerdictCache(repo).get(mk()) is None


def test_passthrough_skipped_without_calls(env):
    b = FakeBackend()
    fs = [mk(title="a", source="gitleaks"), mk(title="b", source="osv")]
    out = run(env, fs, b)
    assert b.calls == [] and all(f.verdict == Verdict.SKIPPED for f in out.findings)


def test_over_cap_unknown(env):
    repo, scope, cfg = env
    cfg.verifier.cap = 1
    b = FakeBackend({"verifier": verdict()})
    hi, lo = mk(title="hi", sev=Severity.critical), mk(title="lo", sev=Severity.minor)
    out = run(env, [lo, hi], b)
    assert hi.verdict == Verdict.CONFIRMED
    assert lo.verdict == Verdict.UNKNOWN and lo.verifier_reason == "not verified: over verifier cap"
    assert len(b.calls) == 1 and out.findings == [lo, hi]


def test_select_ordering():
    a = mk(title="a", sev=Severity.minor)
    b = mk(title="b", sev=Severity.major, lane=Lane.nitpick)
    c = mk(title="c", sev=Severity.major)
    sel, pas, over = select_for_verification([a, b, c], 2)
    assert sel == [c, b] and over == [a] and pas == []


def test_per_call_budget_floor_not_a_tiny_split(env):
    # Real run: 1.50 / (findings + 2) = 0.375 aborted an Opus verifier (~$0.45 per call).
    repo, scope, cfg = env
    cfg.budget_usd = 1.5
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk(sid="a")], b)
    assert b.calls[0].max_budget_usd == pytest.approx(0.75)
    cfg.budget_usd = 0.4
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk(sid="b")], b)
    assert b.calls[0].max_budget_usd == pytest.approx(0.4)


def test_packet_for_routes_each_finding_to_its_packet(env):
    repo, scope, cfg = env
    pa = Packet(text="PKT-A", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="a")
    pb = Packet(text="PKT-B", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="b")
    f1, f2 = mk(title="one"), mk(title="two")
    f1.stable_id, f2.stable_id = "s1", "s2"
    f1.start_line, f2.start_line = 1, 2
    b = FakeBackend({"verifier": verdict()})
    run_verifier([f2, f1], None, cfg, b, scope, VerdictCache(repo), "SYS",
                 packet_for=lambda f: pb if f is f2 else pa)
    assert len(b.calls) == 2
    for c in b.calls:
        assert ("PKT-A" in c.system_prompt) != ("PKT-B" in c.system_prompt)
    assert "PKT-A" in b.calls[0].system_prompt  # sorted by packet sha


def test_no_packet_uses_bare_system_prompt(env):
    repo, scope, cfg = env
    b = FakeBackend({"verifier": verdict()})
    run_verifier([mk()], None, cfg, b, scope, VerdictCache(repo), "SYS")
    assert b.calls[0].system_prompt == "SYS"
