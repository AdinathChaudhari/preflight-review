"""Scoring formulas, deterministic matching and the two-model judge."""

from __future__ import annotations

import pytest

from preflight_review.evaluation.judge import deterministic_matches, judge_pairs
from preflight_review.evaluation.score import (
    GoldenItem,
    Match,
    coarse,
    deterministic_match,
    finding_id,
    score,
)
from preflight_review.findings import Category, Finding, Lane, Severity
from preflight_review.model import FakeBackend


def mk_finding(sid, file="a.py", start=10, end=10, category=Category.security, lane=Lane.inline):
    return Finding(
        file=file, start_line=start, end_line=end, title="t", claim="c",
        severity=Severity.major, category=category, lane=lane, stable_id=sid,
    )


def mk_golden(gid, file="a.py", start=10, end=10, category="security", real=True):
    return GoldenItem(gid, file, start, end, category, "major", "desc", "seeded", real)


def test_formulas():
    goldens = [mk_golden("g1"), mk_golden("g2", start=50, end=50), mk_golden("g3", start=90, end=90)]
    findings = [mk_finding("f1"), mk_finding("f2", start=50), mk_finding("f3", start=200)]
    matches = [Match("g1", "f1", "deterministic"), Match("g2", "f2", "deterministic")]
    s = score(goldens, findings, matches)
    assert s.precision == pytest.approx(2 / 3)
    assert s.ep_recall == pytest.approx(2 / 3)
    assert s.snr == pytest.approx(2.0)
    assert s.f1 == pytest.approx(2 / 3)
    assert s.false_positive_ids == ["f3"]
    assert s.per_category_recall == {"security": pytest.approx(2 / 3)}


def test_snr_no_noise_guard():
    s = score([mk_golden("g1")], [mk_finding("f1")], [Match("g1", "f1", "deterministic")])
    assert s.precision == 1.0
    assert s.snr is None and s.snr_note == "no noise (all findings important)"


def test_important_extra_counts_toward_snr():
    findings = [mk_finding("f1"), mk_finding("f2"), mk_finding("f3")]
    s = score([mk_golden("g1")], findings, [Match("g1", "f1", "deterministic")], {"f2"})
    assert s.snr == pytest.approx(2.0)
    assert s.precision == pytest.approx(1 / 3)


def test_zero_edges():
    s = score([mk_golden("g1")], [], [])
    assert s.precision is None and s.f1 is None and s.ep_recall == 0.0
    s = score([], [mk_finding("f1")], [])
    assert s.ep_recall is None and s.precision == 0.0 and s.f1 is None
    s = score([mk_golden("g1")], [mk_finding("f1", file="b.py")], [])
    assert s.f1 == 0.0


def test_non_inline_lanes_excluded():
    findings = [
        mk_finding("f1"),
        mk_finding("n1", lane=Lane.nitpick),
        mk_finding("o1", lane=Lane.outside_diff),
        mk_finding("p1", lane=Lane.pre_existing),
    ]
    s = score([mk_golden("g1")], findings, [Match("g1", "f1", "deterministic")])
    assert s.n_findings == 1 and s.precision == 1.0


def test_non_real_golden_not_ep_pass():
    s = score([mk_golden("g1", real=False)], [mk_finding("f1")], [Match("g1", "f1", "deterministic")])
    assert s.precision == 0.0 and s.ep_recall is None


@pytest.mark.parametrize(
    "start,end,expected",
    [(13, 13, True), (14, 14, False), (7, 7, True), (6, 6, False)],
)
def test_deterministic_tolerance(start, end, expected):
    g = mk_golden("g", start=10, end=10)
    assert deterministic_match(g, mk_finding("f", start=start, end=end)) is expected


def test_deterministic_other_file_and_category():
    g = mk_golden("g")
    assert not deterministic_match(g, mk_finding("f", file="b.py"))
    assert not deterministic_match(g, mk_finding("f", category=Category.correctness))
    # same coarse class: secrets maps to security
    assert deterministic_match(g, mk_finding("f", category=Category.secrets))


def test_coarse_enum_and_string():
    assert coarse(Category.concurrency) == "data"
    assert coarse("error_handling") == "correctness"
    assert coarse("Race Condition") == "race condition"


def test_finding_id_fallbacks():
    f = mk_finding("sid")
    assert finding_id(f) == "sid"
    f.stable_id = None
    assert finding_id(f) == "a.py:10"


def _judge_backend(sonnet, opus):
    return FakeBackend({"judge:sonnet": sonnet, "judge:opus": opus})


def test_judge_both_true(tmp_path):
    be = _judge_backend({"same": True, "reason": "r"}, {"same": True, "reason": "r"})
    matches, agree = judge_pairs([mk_golden("g")], [mk_finding("f")], be, ["sonnet", "opus"], tmp_path)
    assert [(m.golden_id, m.finding_id, m.judge) for m in matches] == [("g", "f", "sonnet,opus")]
    assert agree == 1.0
    assert be.calls[0].tools == [] and be.calls[0].max_turns == 1


def test_judge_disagreement(tmp_path):
    be = _judge_backend({"same": True, "reason": "r"}, {"same": False, "reason": "r"})
    matches, agree = judge_pairs([mk_golden("g")], [mk_finding("f")], be, ["sonnet", "opus"], tmp_path)
    assert matches == [] and agree < 1.0


def test_judge_failure_is_no_match(tmp_path):
    be = FakeBackend({"judge:sonnet": {"same": True, "reason": "r"}})  # opus has no response
    matches, agree = judge_pairs([mk_golden("g")], [mk_finding("f")], be, ["sonnet", "opus"], tmp_path)
    assert matches == [] and agree < 1.0


def test_judge_skips_other_files(tmp_path):
    be = _judge_backend({"same": True, "reason": "r"}, {"same": True, "reason": "r"})
    matches, agree = judge_pairs([mk_golden("g")], [mk_finding("f", file="b.py")], be, ["sonnet", "opus"], tmp_path)
    assert matches == [] and be.calls == []


def test_deterministic_matches_list():
    ms = deterministic_matches([mk_golden("g")], [mk_finding("f"), mk_finding("x", start=99)])
    assert [(m.golden_id, m.finding_id, m.judge) for m in ms] == [("g", "f", "deterministic")]
