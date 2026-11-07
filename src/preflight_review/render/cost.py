"""Model calls and USD per pipeline stage and per model, from a run's lane results."""

from __future__ import annotations

import re

# Result keys look like `finder:correctness@b2`, `security@b1`, `verifier:<id>#v2`,
# `adjudicator:<id>#fallback`, `triage:<path>`, `sweep`, `walkthrough`.
STAGE_NAMES = {"verifier": "verify"}
STAGE_ORDER = ("triage", "rerank", "finder", "security", "sweep", "verify", "adjudicator",
               "walkthrough")
_STAGE_RE = re.compile(r"^[^:@#]+")


def stage_of(key: str) -> str:
    m = _STAGE_RE.match(key)
    name = m.group(0) if m else key
    return STAGE_NAMES.get(name, name)


def calls_by_stage(lane_results: dict) -> list[tuple[str, int, float]]:
    """(stage, calls, USD) in pipeline order; stages without calls are left out."""
    counts: dict[str, list] = {}
    for key, res in lane_results.items():
        entry = counts.setdefault(stage_of(key), [0, 0.0])
        entry[0] += 1
        entry[1] += getattr(res, "cost_usd", 0.0) or 0.0
    rank = {s: i for i, s in enumerate(STAGE_ORDER)}
    ordered = sorted(counts, key=lambda s: (rank.get(s, len(rank)), s))
    return [(s, counts[s][0], counts[s][1]) for s in ordered]


FAMILIES = ("haiku", "sonnet", "opus", "fable")


def model_family(model: str | None) -> str:
    low = (model or "").lower()
    return next((f for f in FAMILIES if f in low), model or "unknown")


def calls_by_model(lane_results: dict) -> list[tuple[str, int, float]]:
    """(model family, calls, USD), cheapest family first, so Opus use is visible."""
    counts: dict[str, list] = {}
    for res in lane_results.values():
        entry = counts.setdefault(model_family(getattr(res, "model", None)), [0, 0.0])
        entry[0] += 1
        entry[1] += getattr(res, "cost_usd", 0.0) or 0.0
    rank = {f: i for i, f in enumerate(FAMILIES)}
    ordered = sorted(counts, key=lambda f: (rank.get(f, len(rank)), f))
    return [(f, counts[f][0], counts[f][1]) for f in ordered]


def _parts(rows: list[tuple[str, int, float]]) -> str:
    return ", ".join(f"{name} {n} call{'s' if n != 1 else ''} ${usd:.2f}" for name, n, usd in rows)


def cost_line(cost_usd: float, lane_results: dict) -> str:
    """`Cost: USD 1.2345 — by stage: finder 5 calls $0.80, verify 3 calls $0.20; by model:
    sonnet 7 calls $0.90, opus 1 call $0.10`."""
    line = f"Cost: USD {cost_usd:.4f}"
    if lane_results:
        line += (
            f" — by stage: {_parts(calls_by_stage(lane_results))}; "
            f"by model: {_parts(calls_by_model(lane_results))}"
        )
    return line
