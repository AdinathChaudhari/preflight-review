"""Deterministic tool layer: ruff, gitleaks, ast-grep, osv-scanner -> Findings."""

from __future__ import annotations

from preflight_review.config import Config
from preflight_review.findings import Finding, Lane
from preflight_review.scope import Scope, is_in_diff
from preflight_review.tools.astgrep import parse_astgrep, run_astgrep
from preflight_review.tools.base import INSTALL_HINTS, ToolStatus, which
from preflight_review.tools.gitleaks import parse_gitleaks, run_gitleaks
from preflight_review.tools.osv import osv_should_run, parse_osv, run_osv
from preflight_review.tools.ruff import parse_ruff, run_ruff

__all__ = [
    "INSTALL_HINTS", "ToolStatus", "which", "run_tools", "osv_should_run",
    "parse_ruff", "parse_gitleaks", "parse_astgrep", "parse_osv",
    "run_ruff", "run_gitleaks", "run_astgrep", "run_osv",
]


def _assign_lane(scope: Scope, finding: Finding) -> None:
    if finding.source == "osv":  # file-level: stays inline
        finding.lane = Lane.inline
    elif is_in_diff(scope, finding.file, finding.start_line):
        finding.lane = Lane.inline
    else:
        finding.lane = Lane.pre_existing


def run_tools(scope: Scope, config: Config) -> tuple[list[Finding], list[ToolStatus]]:
    findings: list[Finding] = []
    statuses: list[ToolStatus] = []
    for runner in (run_ruff, run_gitleaks, run_astgrep, run_osv):
        found, status = runner(scope, config)
        for f in found:
            _assign_lane(scope, f)
        findings.extend(found)
        statuses.append(status)
    return findings, statuses
