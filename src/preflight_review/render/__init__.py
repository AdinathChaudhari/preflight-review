"""Report rendering: markdown, JSON, NDJSON and manifest outputs for one run."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.errors import RenderError
from preflight_review.findings import Finding
from preflight_review.model.backend import ModelResult
from preflight_review.scope import Scope
from preflight_review.stages.policy import PolicyResult
from preflight_review.tools.base import ToolStatus


@dataclass
class RunResult:
    scope: Scope
    config: Config
    packet: Packet | None
    policy: PolicyResult
    tool_statuses: list[ToolStatus]
    walkthrough: dict | None
    lane_results: dict[str, ModelResult]  # 'finder:correctness', 'verifier:<stable_id>'
    cost_usd: float
    duration_s: float
    round: int
    pr_label: str
    basename: str
    outcome: str  # 'complete' | 'incomplete'
    incomplete_reasons: list[str]
    models_used: dict[str, str]  # stage -> model
    previous_report: str | None
    suppressed: list[tuple[Finding, str]] = field(default_factory=list)  # (finding, learning_id)
    prefilter_dropped: list[tuple[Finding, str]] = field(default_factory=list)
    show_pre_existing: bool = False
    reused: bool = False
    reused_confirmed: int = 0
    reused_unverified: int = 0
    notes: list[str] = field(default_factory=list)
    backend_name: str = ""
    config_source: str = "worktree"
    claude_code_version: str | None = None
    test_cmd: str | None = None
    triaged_out: list[str] = field(default_factory=list)  # APPROVED (trivial) paths
    triage_notes: dict[str, str] = field(default_factory=dict)  # path -> reason
    files_read_outside_packet: dict[str, list[str]] = field(default_factory=dict)  # lane -> paths
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_paths: list[str] | None = None  # None = legacy: n - triaged
    not_reviewed: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    batches: int = 0
    packet_shas: list[str] = field(default_factory=list)
    finder_dropped: list[dict] = field(default_factory=list)  # {stage, lane, batch, reason, candidate}
    risk_tiers: dict[str, str] = field(default_factory=dict)  # path -> tier


from preflight_review.render.json_out import render_json, write_ndjson  # noqa: E402
from preflight_review.render.manifest import build_manifest  # noqa: E402
from preflight_review.render.report import dropped_records, render_report  # noqa: E402


def write_outputs(run: RunResult, paths: dict[str, Path]) -> dict[str, Path]:
    """Write md, json, manifest, ndjson and the relative `latest.md` symlink."""
    try:
        md = Path(paths["md"])
        md.parent.mkdir(parents=True, exist_ok=True)
        json_path = Path(paths["json"])
        manifest_path = Path(paths["manifest"])
        ndjson_path = md.with_suffix(".ndjson")
        latest = Path(paths["latest"])

        md.write_text(render_report(run), encoding="utf-8")
        json_path.write_text(json.dumps(render_json(run), indent=2, sort_keys=False) + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(build_manifest(run), indent=2) + "\n", encoding="utf-8")
        write_ndjson(run, ndjson_path)
        dropped_path = md.parent / (md.stem + ".dropped.jsonl")
        dropped_path.write_text(
            "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in dropped_records(run)),
            encoding="utf-8",
        )

        latest.parent.mkdir(parents=True, exist_ok=True)
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        try:
            latest.symlink_to(md.name)
        except OSError:
            shutil.copyfile(md, latest)
    except (OSError, KeyError) as exc:
        raise RenderError(f"could not write review outputs: {exc}") from exc
    return {"md": md, "json": json_path, "manifest": manifest_path, "ndjson": ndjson_path, "dropped": dropped_path, "latest": latest}


__all__ = ["RunResult", "write_outputs", "render_report", "render_json", "write_ndjson", "build_manifest"]
