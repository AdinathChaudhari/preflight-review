"""Triage: decide which changed files need the full model review (deterministic first, Haiku second)."""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
GROUP_MAX_FILES = 20
GROUP_MAX_CHARS = 60_000


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


def _fenced(text: str) -> str:
    """Fence longer than any backtick run inside the diff, so a diff cannot close it early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}diff\n{text}\n{fence}"


def _groups(files: list[tuple[FileChange, str]]) -> list[list[tuple[FileChange, str]]]:
    """Plan-order groups of at most GROUP_MAX_FILES files and GROUP_MAX_CHARS of diff text;
    a file bigger than the char cap ends up alone."""
    groups: list[list[tuple[FileChange, str]]] = []
    cur: list[tuple[FileChange, str]] = []
    chars = 0
    for item in files:
        n = len(item[1])
        if cur and (len(cur) >= GROUP_MAX_FILES or chars + n > GROUP_MAX_CHARS):
            groups.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += n
    if cur:
        groups.append(cur)
    return groups


def _haiku_group(
    group: list[tuple[FileChange, str]], scope: Scope, config: Config, backend
) -> tuple[dict[str, tuple[str, str]], ModelResult | None]:
    """One Haiku call for a group. Returns {path: (decision, reason)} and the raw result.
    Anything but a clean, exact APPROVED for a listed path resolves to NEEDS_REVIEW."""
    paths = [fc.path for fc, _ in group]
    out = {p: (NEEDS_REVIEW, "") for p in paths}
    try:
        blocks = "\n\n".join(f"### {fc.path}\n{_fenced(text)}" for fc, text in group)
        req = ModelRequest(
            role="triage",
            model=config.models.triage,
            system_prompt="",
            prompt=prompts.render("triage_batch", files=blocks),
            schema=load_schema("triage_batch"),
            cwd=scope.repo_root,
            tools=[],
            max_turns=TRIAGE_MAX_TURNS,
            max_budget_usd=call_cap(config, TRIAGE_BUDGET_USD * len(group)),
        )
        res = backend.run(req)
    except Exception as exc:  # never skip a file on error
        logger.warning("triage group failed (%d files): %s", len(group), exc)
        return out, None
    if not (res.ok and isinstance(res.structured, dict)):
        return out, res
    decisions = res.structured.get("decisions")
    if not isinstance(decisions, list):
        return out, res
    seen: Counter[str] = Counter()
    for d in decisions:
        if isinstance(d, dict) and isinstance(d.get("path"), str):
            seen[d["path"]] += 1
    listed = set(paths)
    for d in decisions:
        if not isinstance(d, dict):
            continue
        p = d.get("path")
        if isinstance(p, str) and p in listed and seen[p] == 1 and d.get("decision") == APPROVED:
            out[p] = (APPROVED, str(d.get("reason", "")) or "haiku")
    return out, res


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
        gate_docs = config.docs_review != "off" or review_docs
        # Docs are reviewed for consistency; Haiku never approves them.
        sendable = [(fc, file_diff_text(fc)) for fc in undecided if not (gate_docs and is_prose_doc(fc.path))]
        groups = _groups(sendable)
        if groups:
            with ThreadPoolExecutor(max_workers=max(1, min(len(groups), config.concurrency))) as pool:
                futures = [pool.submit(_haiku_group, g, scope, config, backend) for g in groups]
                outcomes = [f.result() for f in futures]  # plan order, not completion order
            for k, (group, (decided, res)) in enumerate(zip(groups, outcomes), start=1):
                if res is not None:
                    lane_results[f"triage:g{k}"] = res
                for fc, _ in group:
                    decision, reason = decided[fc.path]
                    per_file[fc.path] = decision
                    if decision == APPROVED:
                        notes[fc.path] = reason

    skip = all(v != NEEDS_REVIEW for v in per_file.values())
    reason = "all files trivial (approved or skipped)" if skip else ""
    return TriageResult(skip, reason, per_file, lane_results, notes)
