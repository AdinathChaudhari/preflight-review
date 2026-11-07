"""Deterministic tool layer: ruff, gitleaks, ast-grep, osv-scanner, secret-files, zizmor,
actionlint, shellcheck -> Findings."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Finding, Lane
from preflight_review.scope import Scope, is_in_diff
from preflight_review.tools.actionlint import parse_actionlint, run_actionlint
from preflight_review.tools.astgrep import parse_astgrep, run_astgrep
from preflight_review.tools.base import INSTALL_HINTS, ToolStatus, which
from preflight_review.tools.gitleaks import parse_gitleaks, run_gitleaks
from preflight_review.tools.osv import osv_should_run, parse_osv, run_osv
from preflight_review.tools.ruff import parse_ruff, run_ruff
from preflight_review.tools.secret_files import run_secret_files
from preflight_review.tools.shellcheck import parse_shellcheck, run_shellcheck
from preflight_review.tools.zizmor import parse_zizmor, run_zizmor

__all__ = [
    "INSTALL_HINTS", "ToolStatus", "which", "run_tools", "osv_should_run",
    "parse_ruff", "parse_gitleaks", "parse_astgrep", "parse_osv",
    "parse_zizmor", "parse_actionlint", "parse_shellcheck",
    "run_ruff", "run_gitleaks", "run_astgrep", "run_osv", "run_secret_files",
    "run_zizmor", "run_actionlint", "run_shellcheck", "degraded_tools",
]

# Sources whose findings are file-level and always inline (never hidden as pre-existing).
_ALWAYS_INLINE = {"osv", "secret-files"}


def _assign_lane(scope: Scope, finding: Finding) -> None:
    if finding.source in _ALWAYS_INLINE:  # file-level: stays inline
        finding.lane = Lane.inline
    elif is_in_diff(scope, finding.file, finding.start_line):
        finding.lane = Lane.inline
    else:
        finding.lane = Lane.pre_existing


def run_tools(
    scope: Scope, config: Config, extra_rule_dirs: Sequence[Path] = ()
) -> tuple[list[Finding], list[ToolStatus]]:
    """Run every tool. `extra_rule_dirs`: ast-grep rule directories switched on by a repo
    detector (extensions), added to the repo's own .preflight/ast-rules/."""
    findings: list[Finding] = []
    statuses: list[ToolStatus] = []

    def astgrep(s: Scope, c: Config):
        return run_astgrep(s, c, extra_rule_dirs)

    runners = (
        ("ruff", run_ruff), ("gitleaks", run_gitleaks), ("ast-grep", astgrep),
        ("osv-scanner", run_osv), ("secret-files", run_secret_files),
        ("zizmor", run_zizmor), ("actionlint", run_actionlint), ("shellcheck", run_shellcheck),
    )
    for name, runner in runners:
        try:
            found, status = runner(scope, config)
        except (ToolError, OSError, subprocess.SubprocessError) as exc:
            # Unparseable output, a crash or a timeout: report it, never fail the run.
            found, status = [], ToolStatus(
                name, True, None, False, note=f"failed: {exc}"[:300], degraded=True
            )
        for f in found:
            _assign_lane(scope, f)
        findings.extend(found)
        statuses.append(status)
    return findings, statuses


def degraded_tools(statuses: list[ToolStatus]) -> list[str]:
    """`<tool>: <why>` for each enabled, relevant tool that gave no usable result, or
    `<tool>: partial: <why>` for one that ran but lost part of its work (an osv-scanner
    manifest that failed while others were scanned)."""
    return [
        f"{t.name}: partial: {t.note}" if t.ran else f"{t.name}: {t.note or 'did not run'}"
        for t in statuses if t.degraded
    ]


def degraded_partial(entry: str) -> bool:
    """True when a `degraded_tools` entry is for a tool that ran partially."""
    return entry.partition(": ")[2].startswith("partial: ")


def degraded_label(entry: str) -> str:
    """The tool name of a `degraded_tools` entry, with "(partial)" when it ran partially."""
    return entry.split(":", 1)[0] + (" (partial)" if degraded_partial(entry) else "")
