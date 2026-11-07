"""Deep-mode gap sweep: one fresh finder per run that reports only issues not yet found.

It gets every candidate found so far and the list of changed files. A single-batch run also
gets that batch's packet; a multi-batch run reads the changed files itself."""

from __future__ import annotations

import logging

from preflight_review import prompts
from preflight_review.config import Config, split_call_cap, stage_effort
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.findings import Finding
from preflight_review.model import ModelRequest, ModelResult
from preflight_review.model.schemas import load_schema
from preflight_review.scope import Scope
from preflight_review.stages.finders import (
    FINDER_MIN_BUDGET_USD,
    MAX_TURNS,
    FinderOutcome,
    _convert,
    packet_system_prompt,
)

logger = logging.getLogger("preflight_review.sweep")


def sweep_enabled(config: Config) -> bool:
    return config.sweep and config.mode == "deep"


def known_findings_text(known: list[Finding]) -> str:
    """One line per candidate already reported: title, file and line range."""
    lines = []
    seen: set[tuple] = set()
    for f in sorted(known, key=lambda f: (f.file, f.start_line, f.title)):
        k = (f.file, f.start_line, f.end_line, f.title)
        if k in seen:
            continue
        seen.add(k)
        span = f"{f.start_line}" if f.end_line == f.start_line else f"{f.start_line}-{f.end_line}"
        lines.append(f"- {f.file}:{span} [{f.category.value}] {f.title}")
    return "\n".join(lines) if lines else "(none)"


def changed_files_text(paths: list[str]) -> str:
    return "\n".join(f"- {p}" for p in paths) if paths else "(none)"


def run_sweep(
    packet: Packet | None,
    known_findings: list[Finding],
    config: Config,
    backend,
    scope: Scope,
    system_prompt: str,
    index: RepoIndex,
    lane_suffix: str = "",
    n_calls: int = 1,
    changed_files: list[str] | None = None,
) -> FinderOutcome:
    """Run one sweep call over the change (`packet` when given). New candidates carry source
    'llm:sweep'."""
    paths = changed_files if changed_files is not None else [fc.path for fc in scope.files]
    out = FinderOutcome()
    req = ModelRequest(
        role="sweep",
        model=config.models.sweep,
        system_prompt=packet_system_prompt(system_prompt, packet),
        prompt=prompts.render(
            "sweep",
            known_findings=known_findings_text(known_findings),
            changed_files=changed_files_text(paths),
            diff_base=(scope.diff_from or scope.base_sha)[:12],
        ),
        schema=load_schema("finder"),
        cwd=scope.repo_root,
        max_turns=MAX_TURNS.get(config.mode, 6),
        max_budget_usd=split_call_cap(config, n_calls, FINDER_MIN_BUDGET_USD),
        effort=stage_effort(config, config.models.sweep),
    )
    try:
        res = backend.run(req)
    except Exception as exc:  # noqa: BLE001 - any backend failure is a failed lane
        res = ModelResult(
            ok=False, structured=None, subtype="exception", error=str(exc),
            model=config.models.sweep,
        )
    out.lane_results[f"sweep{lane_suffix}"] = res
    if not res.ok or not isinstance(res.structured, dict):
        out.incomplete.append("sweep")
        return out
    s = res.structured
    out.files_read_outside_packet["sweep"] = list(s.get("files_read_outside_packet", []))
    for raw in s.get("findings", []):
        f, reason = _convert(raw, "sweep", scope, index)
        if f is None:
            logger.warning("dropped sweep finding: %s", reason)
            out.dropped.append((raw, reason or "invalid"))
        else:
            out.findings.append(f)
    return out
