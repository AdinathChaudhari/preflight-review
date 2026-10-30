"""ast-grep: structural rules from .preflight/ast-rules/*.yml."""

from __future__ import annotations

import json
import tempfile
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
    tool_version,
)

_SEV = {"error": Severity.major, "warning": Severity.minor}


def parse_astgrep(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"ast-grep produced malformed JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ToolError("ast-grep JSON must be a list")
    out: list[Finding] = []
    for item in data:
        try:
            rel = rel_path(item["file"], repo_root)
            start = int(item["range"]["start"]["line"]) + 1  # 0-based in ast-grep
            end = int(item["range"]["end"]["line"]) + 1
            rule = item["ruleId"]
            message = item.get("message") or rule
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"ast-grep JSON entry malformed: {exc!r}") from exc
        quote = str(item.get("lines") or item.get("text") or "").strip().splitlines()
        out.append(
            Finding(
                file=rel,
                start_line=start,
                end_line=max(start, end),
                title=f"{rule}: {message}"[:80],
                claim=message,
                severity=_SEV.get(str(item.get("severity", "")).lower(), Severity.trivial),
                category=Category.domain,
                source="ast-grep",
                rule_id=rule,
                evidence=[Evidence(rel, start, quote[0] if quote else "")],
            )
        )
    return out


def run_astgrep(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not config.tools.ast_grep:
        return [], ToolStatus("ast-grep", False, None, False, note="disabled in config")
    rules_dir = scope.repo_root / ".preflight" / "ast-rules"
    has_rules = rules_dir.is_dir() and any(rules_dir.glob("*.yml"))
    binary = base.which("ast-grep") or base.which("sg")
    if binary is None:
        if not has_rules:
            return [], ToolStatus("ast-grep", False, None, False, note="no ast-grep rules defined")
        return [], missing_status("ast-grep")
    version = tool_version(binary, scope.repo_root)
    if not has_rules:
        return [], ToolStatus("ast-grep", True, version, False, note="no ast-grep rules defined")
    files = scannable_files(scope, ".py")
    if not files:
        return [], ToolStatus("ast-grep", True, version, False, note="no python files changed")
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        sgconfig = Path(tmp) / "sgconfig.yml"
        sgconfig.write_text(f"ruleDirs:\n  - {json.dumps(str(rules_dir.resolve()))}\n")
        cmd = [binary, "scan", "--json=compact", "-c", str(sgconfig), *[f.path for f in files]]
        code, out, err = run(cmd, scope.repo_root)
    if code not in (0, 1) and not out.strip():
        return [], ToolStatus(
            "ast-grep", True, version, ran=False, note=f"failed (exit {code}): {err.strip()[:200]}"
        )
    findings = parse_astgrep(out, scope.repo_root)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("ast-grep", True, version, True, len(findings), duration_ms=ms)
