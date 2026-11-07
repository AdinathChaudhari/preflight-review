"""gitleaks: secret scan of committed history (+ changed worktree files).

Secret material is never read: scans use --redact and the Secret/Match/Line
fields are ignored by the parser.

A `.gitleaks.toml` at the repository root (gitleaks' own config format: extra rules and
`[[allowlists]]`) is passed to every scan with `--config`. Without it, a `gitleaks dir`
scan of the temporary worktree copy would not see the repo's allowlists.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Evidence, Finding, Severity, Verdict
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


def _fingerprint(item: dict, rel: str) -> str:
    return item.get("Fingerprint") or f"{rel}:{item.get('RuleID')}:{item.get('StartLine')}"


def parse_gitleaks(json_text: str, repo_root: Path) -> list[Finding]:
    try:
        data = json.loads(json_text) if json_text.strip() else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"gitleaks produced malformed JSON: {exc}") from exc
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ToolError("gitleaks JSON must be a list")
    out: list[Finding] = []
    seen: set[str] = set()
    for item in data:
        try:
            rule = item["RuleID"]
            rel = rel_path(item["File"], repo_root)
            start = int(item["StartLine"])
            end = int(item.get("EndLine") or start)
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"gitleaks JSON entry malformed: {exc!r}") from exc
        key = _fingerprint(item, rel)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Finding(
                file=rel,
                start_line=start,
                end_line=max(start, end),
                title=f"Secret detected: {rule}"[:80],
                claim=f"gitleaks rule {rule} matched at {rel}:{start}",
                severity=Severity.critical,
                category=Category.secrets,
                source="gitleaks",
                rule_id=rule,
                evidence=[Evidence(rel, start, f"REDACTED ({rule})")],
                verdict=Verdict.SKIPPED,
            )
        )
    return out


CONFIG_FILE = ".gitleaks.toml"


def config_args(repo_root: Path) -> list[str]:
    """`--config <repo>/.gitleaks.toml` when the repository has one, else nothing."""
    path = Path(repo_root) / CONFIG_FILE
    return ["--config", str(path)] if path.is_file() else []


def _scan(binary: str, args: list[str], repo_root: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        cmd = [
            binary, *args, *config_args(repo_root), "-f", "json", "-r", str(report),
            "--redact", "--no-banner", "--exit-code", "0",
        ]
        code, _, err = run(cmd, repo_root)
        if code not in (0, 1):
            raise ToolError(f"gitleaks failed (exit {code}): {err.strip()[:200]}")
        return report.read_text() if report.exists() else ""


def run_gitleaks(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not config.tools.gitleaks:
        return [], ToolStatus("gitleaks", False, None, False, note="disabled in config")
    binary = base.which("gitleaks")
    if binary is None:
        return [], missing_status("gitleaks")
    version = tool_version(binary, scope.repo_root, ("version",))
    started = time.monotonic()
    root = str(scope.repo_root)
    findings: list[Finding] = []
    seen: set[tuple] = set()

    def _add(text: str, base_root: Path) -> None:
        for f in parse_gitleaks(text, base_root):
            key = (f.file, f.rule_id, f.start_line)
            if key not in seen:
                seen.add(key)
                findings.append(f)

    _add(_scan(binary, ["git", f"--log-opts={scope.diff_from}..HEAD", root], scope.repo_root),
         scope.repo_root)
    if scope.include_worktree:
        files = scannable_files(scope)
        if files:
            # One `gitleaks dir` run over a temp copy; reported paths are
            # mapped back to repo-relative by stripping the temp prefix.
            with tempfile.TemporaryDirectory() as snap:
                snap_root = Path(snap)
                for f in files:
                    dest = snap_root / f.path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(scope.repo_root / f.path, dest)
                _add(_scan(binary, ["dir", str(snap_root)], scope.repo_root), snap_root)
    ms = int((time.monotonic() - started) * 1000)
    return findings, ToolStatus("gitleaks", True, version, True, len(findings), duration_ms=ms)
