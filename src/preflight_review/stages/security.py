"""Security lane (models.security): one traced-finding call, plus the security verifier helpers."""

from __future__ import annotations

import json
import logging

from preflight_review import prompts
from preflight_review.config import Config, stage_effort
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.findings import Finding, Verdict, to_dict
from preflight_review.model import ModelRequest, ModelResult
from preflight_review.model.schemas import load_schema
from preflight_review.scope import Scope
from preflight_review.stages.finders import FinderOutcome, _convert, packet_system_prompt

logger = logging.getLogger("preflight_review.security")

SECURITY_MAX_TURNS = 8
SECURITY_MAX_TURNS_DEEP = 18
MIN_CONFIDENCE_10 = 8


def _trace_ok(trace: object) -> bool:
    if not isinstance(trace, list) or not trace:
        return False
    for hop in trace:
        if not isinstance(hop, dict):
            return False
        file, line = hop.get("file"), hop.get("line")
        if not isinstance(file, str) or not file.strip():
            return False
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return False
    return True


def run_security_lane(
    packet: Packet,
    config: Config,
    backend,
    scope: Scope,
    system_prompt: str,
    index: RepoIndex,
    lane_suffix: str = "",
) -> FinderOutcome:
    out = FinderOutcome()
    req = ModelRequest(
        role="security",
        model=config.models.security,
        system_prompt=packet_system_prompt(system_prompt, packet),
        prompt=prompts.render("security"),
        schema=load_schema("security"),
        cwd=scope.repo_root,
        max_turns=SECURITY_MAX_TURNS_DEEP if config.mode == "deep" else SECURITY_MAX_TURNS,
        max_budget_usd=None if config.budget_usd is None else config.budget_usd / 3,
        effort=stage_effort(config, config.models.security),
    )
    try:
        res = backend.run(req)
    except Exception as exc:
        res = ModelResult(
            ok=False, structured=None, subtype="exception", error=str(exc),
            model=config.models.security,
        )
    out.lane_results[f"security{lane_suffix}"] = res
    if not res.ok or not isinstance(res.structured, dict):
        out.incomplete.append("security")
        return out
    s = res.structured
    out.files_read_outside_packet["security"] = list(s.get("files_read_outside_packet", []))
    for raw in s.get("findings", []):
        if not _trace_ok(raw.get("trace")):
            logger.warning("dropped security finding: no_trace")
            out.dropped.append((raw, "no_trace"))
            continue
        d = dict(raw)
        exploit = d.pop("exploit_scenario", None)
        if exploit and not d.get("failure_scenario"):
            d["failure_scenario"] = exploit
        d["category"] = "security"
        f, reason = _convert(d, "security", scope, index)
        if f is None:
            logger.warning("dropped security finding: %s", reason)
            out.dropped.append((raw, reason or "invalid"))
        else:
            out.findings.append(f)
    return out


def security_verifier_prompt(finding: Finding) -> str:
    return prompts.render(
        "verifier_security",
        finding_json=json.dumps(to_dict(finding), indent=2),
    )


def parse_conf10(value) -> int | None:
    """confidence_10 as an int ("9" and 9.0 count as 9), or None for a bool or junk. Shared
    by the verdict rule and the stored vote, so the aggregate re-scores the same way."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def security_verdict_rule(result: dict) -> Verdict:
    verdict = result.get("verdict")
    if verdict == "REFUTED":
        return Verdict.REFUTED
    if verdict == "CONFIRMED":
        conf = parse_conf10(result.get("confidence_10", 0)) or 0
        if conf >= MIN_CONFIDENCE_10:
            return Verdict.CONFIRMED
    return Verdict.UNKNOWN
