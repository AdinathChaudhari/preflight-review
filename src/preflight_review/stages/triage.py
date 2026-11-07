"""Triage: decide which changed files need the full model review (deterministic first, Haiku second)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from preflight_review import prompts
from preflight_review.config import Config, call_cap
from preflight_review.context.risk import is_prose_doc
from preflight_review.model import ModelRequest, ModelResult
from preflight_review.model.schemas import load_schema
from preflight_review.scope import FileChange, Scope, docs_only_skip

logger = logging.getLogger("preflight_review.triage")

NEEDS_REVIEW = "NEEDS_REVIEW"
APPROVED = "APPROVED"
SKIPPED = "SKIPPED"
TRIAGE_MAX_TURNS = 2
TRIAGE_BUDGET_USD = 0.05
MAX_DIFF_CHARS = 20_000


@dataclass
class TriageResult:
    skip_llm: bool
    reason: str
    per_file: dict[str, str]  # path -> NEEDS_REVIEW | APPROVED | SKIPPED
    lane_results: dict[str, ModelResult] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)  # path -> reason


def _strip(line: str) -> str:
    return "".join(line.split())


def is_whitespace_only(fc: FileChange) -> bool:
    """Added and deleted lines are equal multisets once all whitespace is removed."""
    added: Counter[str] = Counter()
    deleted: Counter[str] = Counter()
    changed = 0
    for h in fc.hunks:
        for line in h.lines:
            if line.startswith("+"):
                changed += 1
                s = _strip(line[1:])
                if s:
                    added[s] += 1
            elif line.startswith("-"):
                changed += 1
                s = _strip(line[1:])
                if s:
                    deleted[s] += 1
    return changed > 0 and added == deleted


def file_diff_text(fc: FileChange) -> str:
    parts: list[str] = []
    for h in fc.hunks:
        parts.append(f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@")
        parts.extend(h.lines)
    text = "\n".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n… (diff truncated)"
    return text


def _haiku(fc: FileChange, scope: Scope, config: Config, backend) -> tuple[str, ModelResult | None]:
    try:
        req = ModelRequest(
            role="triage",
            model=config.models.triage,
            system_prompt="",
            prompt=prompts.render("triage", path=fc.path, diff=file_diff_text(fc)),
            schema=load_schema("triage"),
            cwd=scope.repo_root,
            tools=[],
            max_turns=TRIAGE_MAX_TURNS,
            max_budget_usd=call_cap(config, TRIAGE_BUDGET_USD),
        )
        res = backend.run(req)
    except Exception as exc:  # never skip a file on error
        logger.warning("triage failed for %s: %s", fc.path, exc)
        return NEEDS_REVIEW, None
    if res.ok and isinstance(res.structured, dict) and res.structured.get("decision") == APPROVED:
        return APPROVED, res
    return NEEDS_REVIEW, res


def triage(scope: Scope, config: Config, backend, review_docs: bool = False) -> TriageResult:
    """`review_docs`: a path-gated lens covers some changed doc (a deploy runbook), so docs
    are reviewed even under docs_review: off."""
    if not scope.files:
        return TriageResult(True, "empty diff", {})
    if docs_only_skip(scope, config.docs_review) and not review_docs:
        return TriageResult(True, "docs/lockfile-only diff", {fc.path: SKIPPED for fc in scope.files})

    per_file: dict[str, str] = {}
    notes: dict[str, str] = {}
    undecided: list[FileChange] = []
    for fc in scope.files:
        if fc.is_binary:
            per_file[fc.path] = SKIPPED
            notes[fc.path] = "binary"
        elif not fc.hunks and fc.status == "R":
            per_file[fc.path] = SKIPPED
            notes[fc.path] = "rename without content change"
        elif is_whitespace_only(fc):
            per_file[fc.path] = APPROVED
            notes[fc.path] = "whitespace-only"
        else:
            per_file[fc.path] = NEEDS_REVIEW
            undecided.append(fc)

    lane_results: dict[str, ModelResult] = {}
    if config.triage.haiku:
        for fc in undecided:
            if (config.docs_review != "off" or review_docs) and is_prose_doc(fc.path):
                continue  # docs are reviewed for consistency; Haiku never approves them
            decision, res = _haiku(fc, scope, config, backend)
            per_file[fc.path] = decision
            if res is not None:
                lane_results[f"triage:{fc.path}"] = res
                if decision == APPROVED:
                    notes[fc.path] = str((res.structured or {}).get("reason", "")) or "haiku"

    skip = all(v != NEEDS_REVIEW for v in per_file.values())
    reason = "all files trivial (approved or skipped)" if skip else ""
    return TriageResult(skip, reason, per_file, lane_results, notes)
