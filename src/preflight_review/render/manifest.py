"""Reproducibility manifest: versions and fingerprints of everything that shaped a run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from preflight_review import __version__
from preflight_review.model.schemas import SCHEMA_NAMES, schema_sha
from preflight_review.prompts import all_prompt_shas

if TYPE_CHECKING:
    from preflight_review.render import RunResult


def build_manifest(run: RunResult) -> dict:
    return {
        "preflight_version": __version__,
        "claude_code_version": run.claude_code_version,
        "backend": run.backend_name,
        "models": dict(run.models_used),
        "prompt_shas": all_prompt_shas(),
        "schema_shas": {name: schema_sha(name) for name in SCHEMA_NAMES},
        "files_read_outside_packet": {k: list(v) for k, v in run.files_read_outside_packet.items()},
        "packet_sha": run.packet.sha if run.packet else None,
        "packet_shas": list(run.packet_shas),
        "config_source": run.config_source,
        "generated_at": run.generated_at.isoformat(),
    }
