"""actionlint (opt-in): syntax and expression checks for changed GitHub Actions workflows."""

from __future__ import annotations

import json
import time
from pathlib import Path, PurePosixPath

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Evidence, Finding, Severity
from preflight_review.scope import Scope
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


def _is_workflow(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix in (".yml", ".yaml") and p.parent.as_posix().endswith(".github/workflows")


def parse_actionlint(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"actionlint produced malformed JSON: {exc}") from exc
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ToolError("actionlint JSON must be a list")
    out: list[Finding] = []
    for item in data:
        try:
            rel = rel_path(item["filepath"], repo_root)
            line = int(item["line"])
            message = item["message"]
            kind = item.get("kind") or "actionlint"
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"actionlint JSON entry malformed: {exc!r}") from exc
        out.append(
            Finding(
                file=rel,
                start_line=max(1, line),
                end_line=max(1, line),
                title=f"{kind}: {message}"[:80],
                claim=message,
                severity=Severity.minor,
                category=Category.correctness,
                source="actionlint",
                rule_id=kind,
                evidence=[Evidence(rel, max(1, line), source_line(repo_root, rel, line))],
            )
        )
    return out


def run_actionlint(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not config.tools.actionlint:
        return [], ToolStatus("actionlint", False, None, False, note="disabled in config")
    files = [f for f in scannable_files(scope) if _is_workflow(f.path)]
    binary = base.which("actionlint")
    if binary is None:
        return [], missing_status("actionlint", relevant=bool(files))
    version = tool_version(binary, scope.repo_root, ("-version",))
    if not files:
        return [], ToolStatus("actionlint", True, version, False, note="no workflow files changed")
    started = time.monotonic()
    # `--`: a changed path starting with "-" must not be parsed as a flag.
    cmd = [binary, "-format", "{{json .}}", "-no-color", "--", *[f.path for f in files]]
    code, out, err = run(cmd, scope.repo_root)
    if code not in (0, 1):
        return [], ToolStatus(
            "actionlint", True, version, ran=False,
            note=f"failed (exit {code}): {err.strip()[:200]}", degraded=True,
        )
    findings = parse_actionlint(out, scope.repo_root)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("actionlint", True, version, True, len(findings), duration_ms=ms)
