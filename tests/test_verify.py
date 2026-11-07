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
    Vote,
    run_verifier,
    score_vote,
    UNVERIFIED_NIT,
    select_for_verification,
    tally,
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
    cfg = Config(mode="deep")
    cfg.cache_warm_seconds = 0
    return repo, scope, cfg


def run(env, findings, backend, cache=None):
    repo, scope, cfg = env
    return run_verifier(findings, PACKET, cfg, backend, scope, cache or VerdictCache(repo), "SYS")


def test_every_vote_runs_on_sonnet():
    cfg = Config(mode="deep")
    for f in (mk(), mk(sev=Severity.critical), mk(cat=Category.security),
              mk(source="llm:rules_compliance")):
        assert verifier_model(f, cfg) == "sonnet"
    cfg.mode = "quick"
    assert verifier_model(mk(sev=Severity.critical), cfg) == "sonnet"


def test_confirmed_with_evidence(env):
    b = FakeBackend({"verifier": verdict()})
    f = mk()
    out = run(env, [f], b)
    # Early stop: the first two votes agree, so no third call.
    assert f.verdict == Verdict.CONFIRMED
    assert f.verifier_reason == "votes 2/2 CONFIRMED (sonnet): because"
    assert f.verifier_evidence and f.verifier_checks == ["read"]
    assert len(b.calls) == 2 and all(c.role == "verifier" and c.model == "sonnet" for c in b.calls)
    assert "PACKET" not in b.calls[0].prompt
    assert "PACKET" in b.calls[0].system_prompt and b.calls[0].system_prompt.startswith("SYS")
    assert out.findings == [f]
    assert set(out.results) == {"verifier:t#v1", "verifier:t#v2"}


def test_single_vote_keeps_plain_key_and_reason(env):
    _repo, _scope, cfg = env
    cfg.verifier.votes = 1
    b = FakeBackend({"verifier": verdict()})
    f = mk()
    out = run(env, [f], b)
    assert len(b.calls) == 1 and f.verifier_reason == "because"
    assert set(out.results) == {"verifier:t"}


def test_confirmed_without_evidence_unknown(env):
    f = mk()
    run(env, [f], FakeBackend({"verifier": verdict(ev=False)}))
    assert f.verdict == Verdict.UNKNOWN
    assert f.verifier_reason == (
        "votes 0/2 CONFIRMED, 2 UNKNOWN (sonnet): no evidence quoted; because"
    )


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
    assert n == 2 and (repo / ".preflight/cache/verdicts.json").exists()
    f2 = mk()
    out = run(env, [f2], b, VerdictCache(repo))
    assert len(b.calls) == n and out.cache_hits == 1 and f2.verdict == Verdict.CONFIRMED
    assert f2.verifier_reason == "votes 2/2 CONFIRMED (sonnet): because"
    write(repo, "app/service.py", "changed = 1\n")
    out = run(env, [mk()], b, VerdictCache(repo))
    assert len(b.calls) == n + 2 and out.cache_hits == 0


def test_cache_not_reused_across_vote_counts(env):
    repo, _scope, cfg = env
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk()], b)
    cfg.verifier.votes = 1
    out = run(env, [mk()], b, VerdictCache(repo))
    assert out.cache_hits == 0 and len(b.calls) == 3


def test_corrupt_cache_is_empty(env):
    repo, _, _ = env
    p = repo / ".preflight/cache/verdicts.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert VerdictCache(repo).get(mk()) is None


@pytest.mark.parametrize("source", [
    "gitleaks", "osv", "secret-files", "ruff", "ast-grep", "zizmor", "actionlint", "shellcheck",
])
def test_tool_findings_never_reach_a_model_verifier(env, source):
    b = FakeBackend()
    f = mk(title="a", source=source, sev=Severity.major, cat=Category.security)
    out = run(env, [f], b)
    assert b.calls == [] and f.verdict == Verdict.SKIPPED and out.failures == 0
    assert f.verifier_reason != UNVERIFIED_NIT


def test_nits_skip_the_verifier_as_unverified_nits(env):
    b = FakeBackend({"verifier": verdict()})
    trivial = mk(title="tr", sev=Severity.trivial)
    nit = mk(title="nl", sev=Severity.minor, lane=Lane.nitpick)
    real = mk(title="real")
    out = run(env, [trivial, nit, real], b)
    assert {c.prompt.count('"title": "real"') for c in b.calls} == {1} and len(b.calls) == 2
    for f in (trivial, nit):
        assert f.verdict == Verdict.SKIPPED and f.verifier_reason == UNVERIFIED_NIT
    assert out.failures == 0 and real.verdict == Verdict.CONFIRMED


def test_over_cap_unknown(env):
    repo, scope, cfg = env
    cfg.verifier.cap = 1
    b = FakeBackend({"verifier": verdict()})
    hi, lo = mk(title="hi", sev=Severity.critical), mk(title="lo", sev=Severity.minor)
    out = run(env, [lo, hi], b)
    assert hi.verdict == Verdict.CONFIRMED
    assert lo.verdict == Verdict.UNKNOWN and lo.verifier_reason == "not verified: over verifier cap"
    assert len(b.calls) == 2 and out.findings == [lo, hi]


def test_select_ordering():
    a = mk(title="a", sev=Severity.minor)
    b = mk(title="b", sev=Severity.major, lane=Lane.outside_diff)
    c = mk(title="c", sev=Severity.major)
    n = mk(title="n", sev=Severity.major, lane=Lane.nitpick)
    sel, pas, over = select_for_verification([a, b, c, n], 2)
    assert sel == [c, b] and over == [a] and pas == [n]


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


def test_no_cap_verifies_every_candidate_uncapped(env):
    _repo, _scope, cfg = env
    assert cfg.verifier.cap is None and cfg.budget_usd is None
    fs = [mk(title=f"t{i}", sid=f"s{i}") for i in range(20)]
    b = FakeBackend({"verifier": verdict()})
    run(env, fs, b)
    assert len(b.calls) == 40  # 2 agreeing votes each
    assert all(f.verdict == Verdict.CONFIRMED for f in fs)
    assert all(c.max_budget_usd is None for c in b.calls)
    assert all(c.max_turns == 10 and c.effort == "high" for c in b.calls)  # deep mode
    sel, _, over = select_for_verification(fs, None)
    assert len(sel) == 20 and over == []


def test_standard_mode_verifier_turns(env):
    _repo, _scope, cfg = env
    cfg.mode = "standard"
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk(sid="c")], b)
    assert b.calls[0].max_turns == 6 and b.calls[0].effort == "medium"


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
    assert len(b.calls) == 4
    for c in b.calls:
        assert ("PKT-A" in c.system_prompt) != ("PKT-B" in c.system_prompt)
    assert "PKT-A" in b.calls[0].system_prompt  # sorted by packet sha


def test_no_packet_uses_bare_system_prompt(env):
    repo, scope, cfg = env
    b = FakeBackend({"verifier": verdict()})
    run_verifier([mk()], None, cfg, b, scope, VerdictCache(repo), "SYS")
    assert b.calls[0].system_prompt == "SYS"


# --- multi-vote tally and adjudication ---------------------------------------------------

def V(v, adj=None):
    sev = Severity(adj) if adj else None
    ev = [] if v == Verdict.UNKNOWN else [object()]
    return Vote(verdict=v, evidence=ev, reason=v.value, checks=[], severity_adjusted=sev)


C, R, U = Verdict.CONFIRMED, Verdict.REFUTED, Verdict.UNKNOWN


@pytest.mark.parametrize("votes,expected", [
    ([C, C, C], C),
    ([C, C, U], C),
    ([R, R, U], R),
    ([R, R, R], R),
    ([U, U, U], U),
    ([C, C, R], C),  # majority decides
    ([C, U, U], U),
    ([C, R, U], None),
    ([R, U, U], U),
    ([C], C),
    ([U], U),
    ([C, U], None),
])
def test_tally(votes, expected):
    verdict, label = tally([V(v) for v in votes])
    assert verdict == expected and label.startswith("votes ")


def test_tally_label():
    assert tally([V(C), V(R), V(U)])[1] == "votes 1/3 CONFIRMED, 1 REFUTED, 1 UNKNOWN"


def test_score_vote_is_pure():
    f = mk()
    before = (f.verdict, f.verifier_reason, f.severity)
    v = score_vote(f, verdict(adj="minor"))
    assert v.verdict == Verdict.CONFIRMED and v.severity_adjusted == Severity.minor
    assert (f.verdict, f.verifier_reason, f.severity) == before
    assert score_vote(f, verdict(ev=False)).verdict == Verdict.UNKNOWN


def test_severity_lowered_once_by_least_lowering_vote(env):
    f = mk(sev=Severity.critical)
    run(env, [f], FakeBackend({"verifier": [verdict(adj="minor"), verdict(adj="major"),
                                            verdict(adj="trivial")]}))
    assert f.verdict == Verdict.CONFIRMED and f.severity == Severity.major
    g = mk(title="g", sev=Severity.critical)
    run(env, [g], FakeBackend({"verifier": [verdict(adj="minor"), verdict(), verdict()]}))
    assert g.severity == Severity.critical


def test_every_vote_cost_is_kept(env):
    out = run(env, [mk()], FakeBackend({"verifier": verdict()}))
    assert sum(r.cost_usd for r in out.results.values()) == pytest.approx(0.02)


def test_partial_vote_failure_is_not_a_verifier_failure(env):
    repo, _, _ = env
    f = mk()
    out = run(env, [f], FakeBackend({"verifier": [verdict(), BackendError("boom"), verdict()]}))
    assert f.verdict == Verdict.CONFIRMED and out.failures == 0
    assert f.verifier_reason.startswith("votes 2/2 CONFIRMED (sonnet) (1 of 3 votes failed): ")
    assert f.severity == Severity.major
    assert VerdictCache(repo).get(f, "|votes=3|model=sonnet|adj=opus") is None  # partial: not cached


SPLIT = [verdict(), verdict("REFUTED", reason="no"), verdict("UNKNOWN", ev=False)]


def test_disagreement_runs_a_third_vote_and_majority_decides(env):
    b = FakeBackend({"verifier": [verdict(), verdict("REFUTED", reason="no"), verdict()]})
    f = mk()
    out = run(env, [f], b)
    assert len(b.calls) == 3 and not [c for c in b.calls if c.role == "adjudicator"]
    assert f.verdict == Verdict.CONFIRMED
    assert f.verifier_reason == "votes 2/3 CONFIRMED, 1 REFUTED (sonnet): because"
    assert set(out.results) == {"verifier:t#v1", "verifier:t#v2", "verifier:t#v3"}


def test_non_critical_split_is_unverified_without_opus(env):
    b = FakeBackend({"verifier": list(SPLIT), "adjudicator": verdict()})
    f = mk(sev=Severity.major)
    out = run(env, [f], b)
    assert len(b.calls) == 3 and not [c for c in b.calls if c.role == "adjudicator"]
    assert f.verdict == Verdict.UNKNOWN and out.failures == 0 and out.adjudicated_by == {}
    assert f.verifier_reason.startswith("votes 1/3 CONFIRMED, 1 REFUTED, 1 UNKNOWN (sonnet): no majority")


def test_critical_two_one_split_goes_to_opus_once(env):
    b = FakeBackend({
        "verifier": [verdict(), verdict("REFUTED", reason="no"), verdict()],
        "adjudicator": verdict("REFUTED", reason="vote 2 holds"),
    })
    f = mk(sev=Severity.critical)
    out = run(env, [f], b)
    adj = [c for c in b.calls if c.role == "adjudicator"]
    assert len(adj) == 1 and adj[0].model == "opus" and adj[0].effort == "high"
    assert len([c for c in b.calls if c.role == "verifier"]) == 3
    assert '"verdict": "REFUTED"' in adj[0].prompt and "## Finding" in adj[0].prompt
    assert "PACKET" in adj[0].system_prompt
    assert f.verdict == Verdict.REFUTED and out.adjudicated_by == {"opus": 1}
    assert f.verifier_reason == (
        "votes 2/3 CONFIRMED, 1 REFUTED (sonnet); adjudicated by opus: REFUTED — vote 2 holds"
    )


def test_critical_agreement_needs_no_adjudicator(env):
    b = FakeBackend({"verifier": verdict(), "adjudicator": verdict()})
    f = mk(sev=Severity.critical)
    run(env, [f], b)
    assert len(b.calls) == 2 and f.verdict == Verdict.CONFIRMED


def sec(v="CONFIRMED", ev=True):
    return {**verdict(v, ev=ev), "confidence_10": 9}


def test_major_security_escalates_only_without_majority(env):
    split = [sec(), sec("REFUTED"), sec("UNKNOWN", ev=False)]
    b = FakeBackend({"verifier:security": split, "adjudicator:security": sec()})
    f = mk(sev=Severity.major, cat=Category.security)
    out = run(env, [f], b)
    assert [c.model for c in b.calls if c.role.startswith("adjudicator")] == ["opus"]
    assert out.adjudicated_by == {"opus": 1}
    two_one = [sec(), sec("REFUTED"), sec()]
    b = FakeBackend({"verifier:security": two_one, "adjudicator:security": sec()})
    g = mk(title="g", sev=Severity.major, cat=Category.security)
    run(env, [g], b)
    assert not [c for c in b.calls if c.role.startswith("adjudicator")]


def test_split_vote_goes_to_configured_adjudicator(env):
    _repo, _scope, cfg = env
    cfg.models.adjudicator = "fable"
    b = FakeBackend({"verifier": list(SPLIT), "adjudicator": verdict(reason="vote 1 holds")})
    f = mk(sev=Severity.critical)
    out = run(env, [f], b)
    adj = [c for c in b.calls if c.role == "adjudicator"]
    assert len(adj) == 1 and adj[0].model == "fable"
    assert f.verdict == Verdict.CONFIRMED
    assert f.verifier_reason == (
        "votes 1/3 CONFIRMED, 1 REFUTED, 1 UNKNOWN (sonnet); adjudicated by fable: "
        "CONFIRMED — vote 1 holds"
    )
    assert "adjudicator:t" in out.results and out.adjudicated_by == {"fable": 1}
    assert out.failures == 0


def test_adjudicator_unavailable_falls_back(env):
    _repo, _scope, cfg = env
    cfg.models.adjudicator = "fable"
    calls = []

    def adjudicator(req):
        calls.append(req.model)
        if req.model == "fable":
            return BackendError("model not found: fable")
        return verdict("REFUTED", reason="contradicted")

    b = FakeBackend({"verifier": list(SPLIT), "adjudicator": adjudicator})
    f = mk(sev=Severity.critical)
    out = run(env, [f], b)
    assert calls == ["fable", "opus"]
    assert f.verdict == Verdict.REFUTED and out.failures == 0
    assert "adjudicated by opus (fallback from fable): REFUTED" in f.verifier_reason
    assert out.results["adjudicator:t"].ok is False
    assert out.results["adjudicator:t#fallback"].ok is True
    assert out.adjudicated_by == {"opus": 1}
    repo, _, _ = env
    assert VerdictCache(repo).get(f, "|votes=3|model=sonnet|adj=fable") is None  # fallback: not cached


def test_adjudicated_verdict_not_reused_after_adjudicator_change(env):
    repo, _, cfg = env
    crit = Severity.critical
    cache = VerdictCache(repo)
    run(env, [mk(sev=crit)], FakeBackend({"verifier": list(SPLIT), "adjudicator": verdict()}), cache)
    assert VerdictCache(repo).get(mk(sev=crit), "|votes=3|model=sonnet|adj=opus") is not None
    b = FakeBackend({"verifier": list(SPLIT), "adjudicator": verdict()})
    out = run(env, [mk(sev=crit)], b, VerdictCache(repo))
    assert out.cache_hits == 1 and b.calls == []
    cfg.models.adjudicator = "fable"
    b = FakeBackend({"verifier": list(SPLIT), "adjudicator": verdict()})
    out = run(env, [mk(sev=crit)], b, VerdictCache(repo))
    assert out.cache_hits == 0
    assert [c.model for c in b.calls if c.role == "adjudicator"] == ["fable"]


def test_adjudication_failure_leaves_unverified_without_demotion(env):
    b = FakeBackend({
        "verifier": [verdict(), verdict("REFUTED"), verdict()],
        "adjudicator": BackendError("down"),
    })
    f = mk(sev=Severity.critical)
    out = run(env, [f], b)
    assert f.verdict == Verdict.UNKNOWN and f.severity == Severity.critical
    assert "adjudicator_failed=True" in f.verifier_reason and out.failures == 1
    assert out.adjudicated_by == {}


def test_adjudicator_unknown_goes_to_unverified(env):
    b = FakeBackend({
        "verifier": [verdict(), verdict("REFUTED"), verdict()],
        "adjudicator": verdict("UNKNOWN", ev=False),
    })
    f = mk(sev=Severity.critical)
    out = run(env, [f], b)
    assert f.verdict == Verdict.UNKNOWN and out.failures == 0
    assert [c.model for c in b.calls if c.role == "adjudicator"] == ["opus"]


def test_security_split_adjudicated_with_security_brief(env):
    b = FakeBackend({
        "verifier:security": [sec(), sec("REFUTED"), sec()],
        "adjudicator:security": BackendError("boom"),
    })
    f = mk(cat=Category.security, source="llm:security", sev=Severity.critical)
    run(env, [f], b)
    adj = [c for c in b.calls if c.role == "adjudicator:security"]
    assert [c.model for c in adj] == ["opus"]  # adjudicator == fallback: no second call
    assert "FALSE POSITIVE FILTERING" in adj[0].prompt  # security brief and rules carried over
    assert all(c.model == "sonnet" for c in b.calls if c.role == "verifier:security")
    assert f.verdict == Verdict.UNKNOWN


def test_quick_mode_single_sonnet_vote(env):
    _repo, _scope, cfg = env
    cfg.mode = "quick"
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk(sev=Severity.critical)], b)
    assert [c.model for c in b.calls] == ["sonnet"]


def test_secret_file_findings_skip_verifier():
    f = Finding(file=".env.local", start_line=1, end_line=1, title="Committed secret file",
                claim="c", severity=Severity.critical, category=Category.secrets,
                source="secret-files")
    sel, passthrough, _ = select_for_verification([f], None)
    assert sel == [] and passthrough == [f] and f.verdict == Verdict.SKIPPED


def test_lens_verifier_addendum_appended(env, monkeypatch):
    from preflight_review import prompts
    from preflight_review.stages.finders import LENSES, LensSpec

    monkeypatch.setitem(
        LENSES, "gated_x", LensSpec("gated_x", "lens_correctness", verifier_addendum="docs_review")
    )
    b = FakeBackend({"verifier": verdict()})
    run(env, [mk(source="llm:gated_x"), mk(title="u")], b)
    addendum = prompts.render("docs_review").strip()
    with_add = [c for c in b.calls if addendum in c.prompt]
    assert len(with_add) == 2  # both votes for the gated_x finding, none for the other
    assert all('"source": "llm:gated_x"' in c.prompt for c in with_add)
