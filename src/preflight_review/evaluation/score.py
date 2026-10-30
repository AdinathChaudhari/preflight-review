"""Scoring with formulas modelled on CodeRabbit's public EP (error pattern) benchmark.

Only inline-lane findings are counted (nitpick / outside_diff / pre_existing are
excluded from every denominator, matching CodeRabbit).

Conventions:
- precision = EP_PASS findings / counted findings; None when there are no findings.
- ep_recall = real goldens with >= 1 match / real goldens; None when there are none.
- SNR = Important / (total - Important); None (with snr_note) when no noise.
- f1 = 2PR/(P+R) when both > 0, 0.0 when either is 0, None when either is None.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight_review.findings import Category, Finding, Lane
from preflight_review.stages.cluster import COARSE

TOLERANCE = 3


@dataclass
class GoldenItem:
    id: str
    file: str
    line_start: int | None
    line_end: int | None
    category: str
    severity: str
    description: str
    kind: str  # 'seeded' | 'real'
    is_real: bool = True


@dataclass
class Match:
    golden_id: str
    finding_id: str
    judge: str  # 'deterministic' or comma-joined models


@dataclass
class Scores:
    precision: float | None
    ep_recall: float | None
    snr: float | None
    f1: float | None
    n_findings: int
    n_goldens: int
    per_category_recall: dict = field(default_factory=dict)
    matched_ids: list = field(default_factory=list)
    false_positive_ids: list = field(default_factory=list)
    snr_note: str | None = None


def coarse(category: Category | str | None) -> str:
    """Map a category (enum or string) to its coarse class."""
    if isinstance(category, Category):
        return COARSE[category]
    text = str(category or "").strip().lower()
    try:
        return COARSE[Category(text)]
    except ValueError:
        return text


def finding_id(f: Finding) -> str:
    return f.stable_id or f.run_id or f"{f.file}:{f.start_line}"


def deterministic_match(golden: GoldenItem, finding: Finding) -> bool:
    if golden.file != finding.file:
        return False
    if golden.line_start is None or golden.line_end is None:
        return False
    if finding.start_line > golden.line_end + TOLERANCE:
        return False
    if finding.end_line < golden.line_start - TOLERANCE:
        return False
    return coarse(golden.category) == coarse(finding.category)


def _counted(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.lane == Lane.inline]


def score(
    goldens: list[GoldenItem],
    findings: list[Finding],
    matches: list[Match],
    important_extra: set[str] | frozenset[str] = frozenset(),
) -> Scores:
    counted = _counted(findings)
    ids = [finding_id(f) for f in counted]
    id_set = set(ids)
    real = {g.id: g for g in goldens if g.is_real}
    real_matches = [m for m in matches if m.golden_id in real and m.finding_id in id_set]

    ep_pass = {m.finding_id for m in real_matches}
    total = len(ids)
    important = ep_pass | (set(important_extra) & id_set)

    precision = (len(ep_pass) / total) if total else None
    snr: float | None
    snr_note: str | None = None
    if total == 0:
        snr, snr_note = None, "no findings"
    elif total == len(important):
        snr, snr_note = None, "no noise (all findings important)"
    else:
        snr = len(important) / (total - len(important))

    matched_golden = {m.golden_id for m in real_matches}
    ep_recall = (len(matched_golden) / len(real)) if real else None

    if precision is None or ep_recall is None:
        f1 = None
    elif precision > 0 and ep_recall > 0:
        f1 = 2 * precision * ep_recall / (precision + ep_recall)
    else:
        f1 = 0.0

    by_cat: dict[str, list[int]] = {}
    for g in real.values():
        slot = by_cat.setdefault(coarse(g.category), [0, 0])
        slot[1] += 1
        if g.id in matched_golden:
            slot[0] += 1
    per_cat = {k: v[0] / v[1] for k, v in sorted(by_cat.items())}

    return Scores(
        precision=precision,
        ep_recall=ep_recall,
        snr=snr,
        f1=f1,
        n_findings=total,
        n_goldens=len(real),
        per_category_recall=per_cat,
        matched_ids=sorted(ep_pass),
        false_positive_ids=[i for i in dict.fromkeys(ids) if i not in ep_pass],
        snr_note=snr_note,
    )
