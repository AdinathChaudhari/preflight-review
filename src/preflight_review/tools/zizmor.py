"""zizmor: security audit of changed GitHub Actions workflows and actions."""

from __future__ import annotations

import json
import time
from pathlib import Path, PurePosixPath

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Evidence, Finding, Severity
from preflight_review.scope import FileChange, Scope
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

# zizmor exit codes: 0 = no findings; 10-14 = findings, by highest severity; others = error.
_OK_CODES = {0, *range(10, 15)}
_SEV = {"high": Severity.major, "medium": Severity.minor}


def is_workflow_file(path: str) -> bool:
    """A GitHub Actions workflow (.github/workflows/*.yml) or action definition (action.yml)."""
    p = PurePosixPath(path)
    if p.suffix not in (".yml", ".yaml"):
        return False
    return p.parent.as_posix().endswith(".github/workflows") or p.stem == "action"


def workflow_files(scope: Scope) -> list[FileChange]:
    return [f for f in scannable_files(scope) if is_workflow_file(f.path)]


def _location_path(symbolic: dict) -> str | None:
    key = symbolic.get("key") or {}
    for value in key.values() if isinstance(key, dict) else ():
        if isinstance(value, dict) and value.get("given_path"):
            return str(value["given_path"])
    return None


def parse_zizmor(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"zizmor produced malformed JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ToolError("zizmor JSON must be a list")
    out: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            raise ToolError(f"zizmor JSON entry is not an object: {item!r}"[:200])
        if item.get("ignored"):
            continue
        try:
            ident = item["ident"]
            desc = item.get("desc") or ident
            severity = str((item.get("determinations") or {}).get("severity", "")).lower()
            locs = item["locations"]
            primary = next(
                (l for l in locs if (l.get("symbolic") or {}).get("kind") == "Primary"), locs[0]
            )
            symbolic = primary.get("symbolic") or {}
            given = _location_path(symbolic)
            point = primary["concrete"]["location"]
            start = int(point["start_point"]["row"]) + 1  # 0-based in zizmor
            end = int(point["end_point"]["row"]) + 1
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise ToolError(f"zizmor JSON entry malformed: {exc!r}") from exc
        if not given:
            raise ToolError(f"zizmor JSON entry has no file path: {ident}")
        rel = rel_path(given, repo_root)
        note = symbolic.get("annotation") or ""
        claim = f"{desc}: {note}" if note else desc
        if item.get("url"):
            claim += f" ({item['url']})"
        out.append(
            Finding(
                file=rel,
                start_line=start,
                end_line=max(start, end),
                title=f"{ident}: {desc}"[:80],
                claim=claim,
                severity=_SEV.get(severity, Severity.trivial),
                category=Category.security,
                source="zizmor",
                rule_id=ident,
                evidence=[Evidence(rel, start, source_line(repo_root, rel, start))],
            )
        )
    return out


def run_zizmor(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not config.tools.zizmor:
        return [], ToolStatus("zizmor", False, None, False, note="disabled in config")
    files = workflow_files(scope)
    binary = base.which("zizmor")
    if binary is None:
        return [], missing_status("zizmor", relevant=bool(files))
    version = tool_version(binary, scope.repo_root)
    if not files:
        return [], ToolStatus("zizmor", True, version, False, note="no workflow files changed")
    started = time.monotonic()
    cmd = [
        binary, "--format", "json", "--min-severity", "medium", "--offline",
        "--", *[f.path for f in files],  # `--`: paths are never flags
    ]
    code, out, err = run(cmd, scope.repo_root)
    if code not in _OK_CODES or (code != 0 and not out.strip()):
        return [], ToolStatus(
            "zizmor", True, version, ran=False, note=f"failed (exit {code}): {err.strip()[:200]}",
            degraded=True,
        )
    findings = parse_zizmor(out, scope.repo_root)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("zizmor", True, version, True, len(findings), duration_ms=ms)
