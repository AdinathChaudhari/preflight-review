"""Reproducibility manifest: versions and fingerprints of everything that shaped a run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from preflight_review import __version__
from preflight_review.depth import MANIFEST_CRUCIAL_CAP
from preflight_review.model.schemas import SCHEMA_NAMES, schema_sha
from preflight_review.prompts import all_prompt_shas
from preflight_review.render.cost import calls_by_model, calls_by_stage

if TYPE_CHECKING:
    from preflight_review.render import RunResult


def build_manifest(run: RunResult) -> dict:
    return {
        "preflight_version": __version__,
        "claude_code_version": run.claude_code_version,
        "backend": run.backend_name,
        "mode_requested": run.mode_requested or run.config.mode,
        "mode_resolved": run.config.mode,
        "crucial_paths": list(run.crucial_paths[:MANIFEST_CRUCIAL_CAP]),
        "estimate": (
            {
                "usd": round(run.estimate.usd, 2), "minutes": run.estimate.minutes,
                "files": run.estimate.files, "batches": run.estimate.batches,
                "calls": run.estimate.calls,
            }
            if run.estimate else None
        ),
        "models": dict(run.models_used),
        "calls_by_stage": {
            stage: {"calls": n, "usd": round(usd, 4)}
            for stage, n, usd in calls_by_stage(run.lane_results)
        },
        "calls_by_model": {
            model: {"calls": n, "usd": round(usd, 4)}
            for model, n, usd in calls_by_model(run.lane_results)
        },
        "prompt_shas": all_prompt_shas(),
        "schema_shas": {name: schema_sha(name) for name in SCHEMA_NAMES},
        "files_read_outside_packet": {k: list(v) for k, v in run.files_read_outside_packet.items()},
        "packet_sha": run.packet.sha if run.packet else None,
        "packet_shas": list(run.packet_shas),
        "config_source": run.config_source,
        "generated_at": run.generated_at.isoformat(),
    }
