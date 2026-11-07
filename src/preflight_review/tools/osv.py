"""osv-scanner: new vulnerable dependencies introduced by changed lockfiles."""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from preflight_review.config import Config
from preflight_review.errors import GitError, ToolError
from preflight_review.findings import Category, Evidence, Finding, Severity, Verdict
from preflight_review.gitutil import show_file
from preflight_review.scope import Scope
from preflight_review.tools import base
from preflight_review.tools.base import (
    ToolStatus,
    missing_status,
    run,
    tool_version,
)

# Only lockfiles and requirements files are scanned (osv-scanner cannot extract pyproject.toml).
MANIFEST_RE = re.compile(
    r"(^|/)(requirements[^/]*\.txt|poetry\.lock|uv\.lock|Pipfile\.lock|package-lock\.json)$"
)


def _manifests(scope: Scope) -> list[str]:
    """Changed manifest paths, including ones the path filters moved to filtered_out."""
    candidates = [f.path for f in scope.files if f.status != "D"] + list(scope.filtered_out)
    seen: set[str] = set()
    out: list[str] = []
    for path in candidates:
        if path in seen or not MANIFEST_RE.search(path):
            continue
        seen.add(path)
        if (scope.repo_root / path).is_file():
            out.append(path)
    return out


def osv_should_run(scope: Scope, config: Config) -> bool:
    if config.tools.osv not in ("auto", True):
        return False
    return bool(_manifests(scope))


def _vulns(json_text: str) -> dict[tuple, dict]:
    """Map (package key, vuln id) -> {name, version, id}."""
    try:
        data = json.loads(json_text) if json_text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ToolError(f"osv-scanner produced malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError("osv-scanner JSON must be an object")
    out: dict[tuple, dict] = {}
    try:
        for res in data.get("results") or []:
            for pkg in res.get("packages") or []:
                p = pkg["package"]
                name, version = p["name"], p.get("version", "")
                key = p.get("purl") or f"{p.get('ecosystem', '')}/{name}@{version}"
                for v in pkg.get("vulnerabilities") or []:
                    out[(key, v["id"])] = {"name": name, "version": version, "id": v["id"]}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ToolError(f"osv-scanner JSON malformed: {exc!r}") from exc
    return out


def parse_osv(
    json_text: str, repo_root: Path, lockfile: str = "", base_text: str | None = None
) -> list[Finding]:
    """Findings for vulns in json_text that are absent from base_text (if given)."""
    current = _vulns(json_text)
    base = _vulns(base_text) if base_text else {}
    try:
        lock_lines = (repo_root / lockfile).read_text(errors="replace").splitlines() if lockfile else []
    except OSError:
        lock_lines = []
    out: list[Finding] = []
    for key, v in current.items():
        if key in base:
            continue
        name, version, vid = v["name"], v["version"], v["id"]
        line = next((i for i, ln in enumerate(lock_lines, 1) if name in ln), 1)
        out.append(
            Finding(
                file=lockfile,
                start_line=line,
                end_line=line,
                title=f"{vid}: {name}@{version}"[:80],
                claim=f"{name} {version} is affected by {vid}",
                severity=Severity.major,
                category=Category.dependency,
                source="osv",
                rule_id=vid,
                evidence=[Evidence(lockfile, line, f"{name} {version}")],
                verdict=Verdict.SKIPPED,
            )
        )
    return out


def _scan(binary: str, path: Path, cwd: Path) -> tuple[str | None, str]:
    """Returns (stdout or None on tool failure, failure note)."""
    code, out, err = run([binary, "scan", "source", "--format", "json", "-L", str(path)], cwd)
    if code not in (0, 1) and not out.strip():
        return None, f"failed (exit {code}): {err.strip()[:200]}"
    return out, ""


def run_osv(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    if not osv_should_run(scope, config):
        note = "disabled in config" if config.tools.osv not in ("auto", True) else "no dependency manifests changed"
        return [], ToolStatus("osv-scanner", False, None, False, note=note)
    binary = base.which("osv-scanner")
    if binary is None:
        return [], missing_status("osv-scanner")
    version = tool_version(binary, scope.repo_root)
    started = time.monotonic()
    findings: list[Finding] = []
    notes: list[str] = []
    scanned = 0
    for path in _manifests(scope):
        current, note = _scan(binary, scope.repo_root / path, scope.repo_root)
        if current is None:
            notes.append(f"{path}: {note}")
            continue
        scanned += 1
        base_out = None
        try:
            base_src = show_file(scope.base_sha, path, scope.repo_root)
        except GitError:
            base_src = None
        if base_src is not None:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / Path(path).name
                p.write_text(base_src)
                base_out, _ = _scan(binary, p, scope.repo_root)
        findings.extend(parse_osv(current, scope.repo_root, path, base_out))
    ms = int((time.monotonic() - started) * 1000)
    if notes and not scanned:
        return findings, ToolStatus(
            "osv-scanner", True, version, False, note="; ".join(notes)[:400], duration_ms=ms,
            degraded=True,
        )
    return findings, ToolStatus(
        "osv-scanner", True, version, True, len(findings), note="; ".join(notes)[:400],
        duration_ms=ms, degraded=bool(notes),
    )
