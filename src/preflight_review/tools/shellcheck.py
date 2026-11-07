"""shellcheck (opt-in): lint changed shell scripts."""

from __future__ import annotations

import json
import time
from pathlib import Path

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

_SEV = {"error": Severity.major, "warning": Severity.minor}
SHELL_SUFFIXES = (".sh", ".bash")


def parse_shellcheck(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else {"comments": []}
    except json.JSONDecodeError as exc:
        raise ToolError(f"shellcheck produced malformed JSON: {exc}") from exc
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        raise ToolError("shellcheck json1 output must hold a comments list")
    out: list[Finding] = []
    for item in comments:
        try:
            rel = rel_path(item["file"], repo_root)
            start = int(item["line"])
            end = int(item.get("endLine") or start)
            code = f"SC{int(item['code'])}"
            message = item["message"]
            level = str(item.get("level", "")).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"shellcheck JSON entry malformed: {exc!r}") from exc
        out.append(
            Finding(
                file=rel,
                start_line=start,
                end_line=max(start, end),
                title=f"{code}: {message}"[:80],
                claim=message,
                severity=_SEV.get(level, Severity.trivial),
                category=Category.correctness,
                source="shellcheck",
                rule_id=code,
                evidence=[Evidence(rel, start, source_line(repo_root, rel, start))],
            )
        )
    return out


def run_shellcheck(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not config.tools.shellcheck:
        return [], ToolStatus("shellcheck", False, None, False, note="disabled in config")
    files = [f for f in scannable_files(scope) if f.path.endswith(SHELL_SUFFIXES)]
    binary = base.which("shellcheck")
    if binary is None:
        return [], missing_status("shellcheck", relevant=bool(files))
    version = tool_version(binary, scope.repo_root)
    if not files:
        return [], ToolStatus("shellcheck", True, version, False, note="no shell scripts changed")
    started = time.monotonic()
    cmd = [binary, "-f", "json1", "--", *[f.path for f in files]]  # `--`: paths are never flags
    code, out, err = run(cmd, scope.repo_root)
    if code not in (0, 1):
        return [], ToolStatus(
            "shellcheck", True, version, ran=False,
            note=f"failed (exit {code}): {err.strip()[:200]}", degraded=True,
        )
    findings = parse_shellcheck(out, scope.repo_root)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("shellcheck", True, version, True, len(findings), duration_ms=ms)
