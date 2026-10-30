"""Machine-readable outputs: findings JSON and CodeRabbit-style NDJSON events."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING

from preflight_review.findings import Finding, to_dict
from preflight_review.render.agent_prompt import agent_prompt
from preflight_review.render.manifest import build_manifest
from preflight_review.render.report import coverage_of, dropped_records, merge_risk, _tier, not_reviewed_entries

if TYPE_CHECKING:
    from preflight_review.render import RunResult


def _finding(f: Finding, run: RunResult) -> dict:
    d = to_dict(f)
    d["agent_prompt"] = agent_prompt(f, run.test_cmd)
    return d


def _val(x) -> str:
    return x.value if hasattr(x, "value") else str(x)


def render_json(run: RunResult) -> dict:
    s, p = run.scope, run.policy
    cov = coverage_of(run)
    level, detail = merge_risk(run)
    walk = run.walkthrough
    if level == "unknown" and walk:
        walk = dict(walk)
        walk["model_merge_risk_suppressed"] = {
            k: walk.get(k) for k in ("merge_risk", "merge_risk_sentence") if k in walk
        }
        walk["merge_risk"] = "unknown"
        walk["merge_risk_sentence"] = detail
    return {
        "schema_version": 1,
        "repo": s.repo_name,
        "branch": s.branch,
        "pr_label": run.pr_label,
        "round": run.round,
        "base_sha": s.base_sha,
        "head_sha": s.head_sha,
        "diff_hash": s.diff_hash,
        "generated_at": run.generated_at.isoformat(),
        "outcome": run.outcome,
        "merge_risk": level,
        "merge_risk_detail": detail,
        "incomplete_reasons": list(run.incomplete_reasons),
        "scope": {
            "files": [
                {"path": fc.path, "status": fc.status, "added": fc.added_lines, "deleted": fc.deleted_lines}
                for fc in s.files
            ],
            "unreviewed": list(cov.excluded),  # deprecated alias of excluded_by_path_filter
            "excluded_by_path_filter": list(cov.excluded),
            "reviewed": len(cov.reviewed),
            "total": cov.total,
            "batches": run.batches,
            "not_reviewed": [{"path": path, "reason": reason, "risk": _tier(run, path)} for path, reason in not_reviewed_entries(run)],
        },
        "findings": [_finding(f, run) for f in p.report],
        "unverified": [_finding(f, run) for f in p.unverified],
        "previously_reported": [
            {
                "stable_id": sid,
                "status": st,
                "title": p.previous_info.get(sid, (None, None, None))[0],
                "file": p.previous_info.get(sid, (None, None, None))[1],
                "line": p.previous_info.get(sid, (None, None, None))[2],
            }
            for sid, st in p.previously_reported
        ],
        "pre_existing_count": len(p.pre_existing),
        "pre_existing": [_finding(f, run) for f in p.pre_existing] if run.show_pre_existing else [],
        "suppressed": [{"stable_id": f.stable_id, "learning_id": lid} for f, lid in run.suppressed],
        "dropped": dropped_records(run),
        "tools": [dataclasses.asdict(t) for t in run.tool_statuses],
        "walkthrough": walk,
        "cost": {
            "usd": run.cost_usd,
            "duration_s": run.duration_s,
            "calls": [
                {
                    "role": role,
                    "model": r.model,
                    "ok": r.ok,
                    "cost_usd": r.cost_usd,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cache_read_tokens": r.cache_read_tokens,
                    "cache_write_tokens": r.cache_write_tokens,
                    "num_turns": r.num_turns,
                    "duration_ms": r.duration_ms,
                }
                for role, r in run.lane_results.items()
            ],
        },
        "manifest": build_manifest(run),
    }


def write_ndjson(run: RunResult, path: Path) -> None:
    s = run.scope
    events: list[dict] = [
        {
            "type": "review_context",
            "repo": s.repo_name,
            "branch": s.branch,
            "pr_label": run.pr_label,
            "round": run.round,
            "head_sha": s.head_sha,
        }
    ]
    for f in run.policy.report:
        events.append(
            {
                "type": "finding",
                "severity": _val(f.severity),
                "fileName": f.file,
                "startLine": f.start_line,
                "endLine": f.end_line,
                "title": f.title,
                "comment": f.claim,
                "codegenInstructions": agent_prompt(f, run.test_cmd),
            }
        )
    events.append({"type": "complete", "findings": len(run.policy.report), "outcome": run.outcome})
    Path(path).write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
