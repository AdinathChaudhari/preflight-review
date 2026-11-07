"""Two-model LLM judge for matching findings to golden items."""

from __future__ import annotations

from pathlib import Path

from preflight_review import prompts
from preflight_review.evaluation.score import (
    GoldenItem,
    Match,
    deterministic_match,
    finding_id,
)
from preflight_review.findings import Finding
from preflight_review.model import ModelRequest
from preflight_review.model.schemas import load_schema


def _golden_text(g: GoldenItem) -> str:
    return f"{g.file}:{g.line_start}-{g.line_end} [{g.category}/{g.severity}] {g.description}"


def _finding_text(f: Finding) -> str:
    return (
        f"{f.file}:{f.start_line}-{f.end_line} [{f.category.value}/{f.severity.value}] "
        f"{f.title}. {f.claim}"
    )


def _ask(backend, model: str, g: GoldenItem, f: Finding, cwd: Path) -> bool | None:
    req = ModelRequest(
        role=f"judge:{model}",
        model=model,
        system_prompt="",
        prompt=prompts.render("judge", golden=_golden_text(g), finding=_finding_text(f)),
        schema=load_schema("judge"),
        cwd=Path(cwd),
        tools=[],
        max_turns=1,
    )
    try:
        res = backend.run(req)
    except Exception:
        return None
    if not res.ok or not isinstance(res.structured, dict):
        return None
    same = res.structured.get("same")
    return same if isinstance(same, bool) else None


def judge_pairs(
    goldens: list[GoldenItem],
    findings: list[Finding],
    backend,
    models: list[str],
    cwd: Path,
) -> tuple[list[Match], float]:
    """Return (matches, agreement_rate). A pair matches only if every model says same."""
    matches: list[Match] = []
    n_pairs = 0
    n_agree = 0
    for g in goldens:
        for f in findings:
            if f.file != g.file:
                continue
            n_pairs += 1
            answers = [_ask(backend, m, g, f, cwd) for m in models]
            if len(set(answers)) == 1 and None not in answers:
                n_agree += 1
            if answers and all(a is True for a in answers):
                matches.append(Match(g.id, finding_id(f), ",".join(models)))
    return matches, (n_agree / n_pairs if n_pairs else 1.0)


def deterministic_matches(goldens: list[GoldenItem], findings: list[Finding]) -> list[Match]:
    return [
        Match(g.id, finding_id(f), "deterministic")
        for g in goldens
        for f in findings
        if deterministic_match(g, f)
    ]
