from __future__ import annotations

from preflight_review.config import Config, NitpicksConfig
from preflight_review.findings import Category, Finding, Lane, Severity, Verdict
from preflight_review.stages.policy import ROUND_MESSAGE, apply_policy
from preflight_review.state import BranchState, FindingState


def mk(title="t", sev=Severity.major, verdict=Verdict.CONFIRMED, file="a.py", s=1,
       lane=Lane.inline, **kw):
    return Finding(file=file, start_line=s, end_line=s, title=title, claim="c", severity=sev,
                   category=Category.correctness, lane=lane, verdict=verdict, **kw)


def pol(fs, round=1, **cfg):
    return apply_policy(fs, Config(**cfg), BranchState(), round)


def test_buckets():
    ref = mk("r", verdict=Verdict.REFUTED, verifier_reason="nope")
    unk = mk("u", sev=Severity.critical, verdict=Verdict.UNKNOWN)
    none = mk("n", verdict=None)
    con = mk("c")
    skp = mk("s", verdict=Verdict.SKIPPED)
    r = pol([ref, unk, none, con, skp], profile="assertive")
    assert r.dropped == [(ref, "refuted: nope")]
    assert {f.title for f in r.unverified} == {"u", "n"}
    assert all(f.severity == Severity.minor for f in r.unverified)
    assert {f.title for f in r.report} == {"c", "s"}


def test_pre_existing_hidden():
    p = mk(lane=Lane.pre_existing)
    r = pol([p])
    assert r.pre_existing == [p] and r.report == []


def test_previously_reported():
    f = mk("old", stable_id="abc")
    st = BranchState(findings={"abc": FindingState("dismissed", last_seen_round=1)})
    r = apply_policy([f], Config(), st, 2)
    assert r.report == [] and r.previously_reported == [("abc", "dismissed")]
    assert r.previous_info == {"abc": ("old", "a.py", 1)}
    st.findings["abc"].last_seen_round = 2
    r = apply_policy([f], Config(), st, 2)
    assert r.report == [f] and r.previously_reported == []


def test_profiles():
    fs = lambda: [mk("maj"), mk("min", sev=Severity.minor), mk("triv", sev=Severity.trivial),
                  mk("inf", sev=Severity.info)]
    q = pol(fs(), profile="quiet")
    assert [f.title for f in q.report] == ["maj"]
    assert len(q.dropped) == 3 and all(d[1] == "below profile quiet" for d in q.dropped)
    c = pol(fs(), profile="chill")
    assert [f.title for f in c.report] == ["maj", "min", "triv"]
    assert [d[0].title for d in c.dropped] == ["inf"]
    a = pol(fs(), profile="assertive")
    assert len(a.report) == 4


def test_quiet_drops_unverified():
    r = pol([mk(verdict=Verdict.UNKNOWN)], profile="quiet")
    assert r.unverified == [] and len(r.dropped) == 1


def test_nit_caps():
    nits = [mk(f"n{i}", sev=Severity.trivial, file="a.py", s=i) for i in range(3)]
    r = pol(nits, profile="chill")
    assert len(r.report) == 2 and len(r.nit_capped) == 1
    many = [mk(f"m{i}", sev=Severity.trivial, file=f"f{i}.py") for i in range(7)]
    r = apply_policy(many, Config(nitpicks=NitpicksConfig(per_file=2, per_run=5)), BranchState(), 1)
    assert len(r.report) == 5 and len(r.nit_capped) == 2


def test_round2_drops_nitpicks():
    n = mk("n", sev=Severity.minor, lane=Lane.nitpick)
    r = pol([n, mk("m")], round=2, profile="assertive")
    assert [f.title for f in r.report] == ["m"]
    assert r.dropped == [(n, "nitpicks suppressed after round 1")]


def test_round_message():
    assert pol([], round=4, max_rounds=3).round_message == ROUND_MESSAGE.format(n=3)
    assert pol([], round=3, max_rounds=3).round_message is None


def test_sort_and_run_ids():
    a = mk("a", sev=Severity.minor, s=1)
    b = mk("b", sev=Severity.major, file="z.py", s=9)
    c = mk("c", sev=Severity.major, file="b.py", s=5)
    u = mk("u", verdict=Verdict.UNKNOWN)
    r = pol([a, b, c, u], profile="assertive")
    assert [f.title for f in r.report] == ["c", "b", "a"]
    assert [f.run_id for f in r.report] == ["F1", "F2", "F3"]
    assert r.unverified[0].run_id == "F4"
