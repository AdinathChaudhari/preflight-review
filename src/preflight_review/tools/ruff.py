"""ruff: lint changed Python files into Findings.

Rules in `tools.ruff.test_ignore` (assert, hardcoded test passwords, temp paths, non-crypto
random) are not reported on `tools.ruff.test_paths`: in tests they are noise, not bugs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Evidence, Finding, Severity
from preflight_review.scope import Scope, glob_to_regex
from preflight_review.tools import base
from preflight_review.tools.base import (
    ToolStatus,
    missing_status,
    rel_path,
    run,
    scannable_files,
    source_line,
    tool_version,
)


def _severity(code: str) -> Severity:
    # Lint pattern matches, not proven vulnerabilities: they are reported without a model
    # vote, so they must not raise merge risk on their own. The security lane finds real ones.
    if code.startswith("S"):
        return Severity.minor
    if code.startswith("B") or code in ("F821", "F811") or code.startswith("E9"):
        return Severity.minor
    return Severity.trivial


def _category(code: str) -> Category:
    if code.startswith("S"):
        return Category.security
    if code.startswith(("B", "F", "E9")):
        return Category.correctness
    return Category.maintainability


def parse_ruff(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"ruff produced malformed JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ToolError("ruff JSON must be a list")
    out: list[Finding] = []
    for item in data:
        try:
            code = item["code"] or ""
            message = item["message"]
            rel = rel_path(item["filename"], repo_root)
            start = int(item["location"]["row"])
            end = int((item.get("end_location") or item["location"])["row"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"ruff JSON entry malformed: {exc!r}") from exc
        fix = item.get("fix") or {}
        out.append(
            Finding(
                file=rel,
                start_line=start,
                end_line=max(start, end),
                title=f"{code}: {message}"[:80],
                claim=message,
                severity=_severity(code),
                category=_category(code),
                source="ruff",
                rule_id=code,
                evidence=[Evidence(rel, start, source_line(repo_root, rel, start))],
                suggested_fix=fix.get("message") or "",
            )
        )
    return out


def drop_test_noise(findings: list[Finding], config: Config) -> list[Finding]:
    """Findings minus `test_ignore` rules on files matching `test_paths`."""
    ignore = set(config.tools.ruff.test_ignore)
    globs = [glob_to_regex(g) for g in config.tools.ruff.test_paths if g]
    return [
        f for f in findings
        if not (f.rule_id in ignore and any(g.match(f.file) for g in globs))
    ]


def run_ruff(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    files = scannable_files(scope, ".py")
    binary = base.which("ruff")
    if binary is None:
        return [], missing_status("ruff", relevant=bool(files))
    version = tool_version(binary, scope.repo_root)
    if not files:
        return [], ToolStatus("ruff", True, version, False, note="no python files changed")
    started = time.monotonic()
    cmd = [
        binary, "check", "--output-format", "json", "--no-cache", "--exit-zero",
        "--select", ",".join(config.tools.ruff.select),
        *[f.path for f in files],
    ]
    code, out, err = run(cmd, scope.repo_root)
    if code not in (0, 1) and not out.strip():
        return [], ToolStatus(
            "ruff", True, version, ran=False, note=f"failed (exit {code}): {err.strip()[:200]}",
            degraded=True,
        )
    findings = drop_test_noise(parse_ruff(out, scope.repo_root), config)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("ruff", True, version, True, len(findings), duration_ms=ms)
