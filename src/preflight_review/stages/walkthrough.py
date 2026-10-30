"""Walkthrough: one Sonnet call that describes the change. It never adds findings."""

from __future__ import annotations

import logging

from preflight_review import prompts
from preflight_review.config import Config
from preflight_review.gitutil import run_git
from preflight_review.model import ModelRequest, ModelResult
from preflight_review.model.schemas import load_schema
from preflight_review.scope import Scope
from preflight_review.stages.policy import PolicyResult

logger = logging.getLogger("preflight_review.walkthrough")

WALKTHROUGH_MAX_TURNS = 3
WALKTHROUGH_BUDGET_USD = 0.25
MAX_SUBJECTS = 30


def diff_summary(scope: Scope) -> str:
    lines = [f"{fc.path} (+{fc.added_lines}/−{fc.deleted_lines})" for fc in scope.files]
    out = run_git(
        ["log", "--format=%s", f"{scope.base_sha}..{scope.head_sha}"], scope.repo_root, check=False
    )
    subjects = [s for s in out.splitlines() if s.strip()][:MAX_SUBJECTS]
    if subjects:
        lines += ["", "Commit subjects:"] + [f"- {s}" for s in subjects]
    return "\n".join(lines) or "(none)"


def findings_summary(policy: PolicyResult) -> str:
    items = list(policy.report) + list(policy.unverified)
    if not items:
        return "(none)"
    return "\n".join(
        f"F{i} {getattr(f.severity, 'value', f.severity)} {f.title}"
        for i, f in enumerate(items, 1)
    )


def walkthrough(
    scope: Scope, policy_result: PolicyResult, config: Config, backend
) -> tuple[dict | None, ModelResult | None]:
    try:
        req = ModelRequest(
            role="walkthrough",
            model=config.models.walkthrough,
            system_prompt="",
            prompt=prompts.render(
                "walkthrough",
                diff_summary=diff_summary(scope),
                findings_summary=findings_summary(policy_result),
            ),
            schema=load_schema("walkthrough"),
            cwd=scope.repo_root,
            tools=[],
            max_turns=WALKTHROUGH_MAX_TURNS,
            max_budget_usd=WALKTHROUGH_BUDGET_USD,
        )
        res = backend.run(req)
    except Exception as exc:
        logger.warning("walkthrough failed: %s", exc)
        return None, None
    if res.ok and isinstance(res.structured, dict):
        return res.structured, res
    return None, res
