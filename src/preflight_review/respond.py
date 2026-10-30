"""`respond`: record the author's label for a finding (state + audit log)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from preflight_review import gitutil
from preflight_review.config import Config
from preflight_review.context.learnings import (
    Learning,
    append_learning,
    next_id,
    read_learnings,
)
from preflight_review.errors import PreflightError
from preflight_review.naming import report_paths
from preflight_review.state import FindingState, load_state, save_state

LABELS = {
    "fixed": "addressed",
    "wrong": "dismissed",
    "intentional": "dismissed",
    "didnt_matter": "dismissed",
}
RESPONSES_PATH = ".preflight/responses.jsonl"
_RUN_ID_RE = re.compile(r"F\d+")
_STABLE_RE = re.compile(r"[0-9a-f]{12}")


@dataclass
class RespondResult:
    stable_id: str
    status: str
    learning_id: str | None
    message: str


@dataclass
class ResolvedFinding:
    stable_id: str
    run_id: str | None
    file: str | None
    title: str | None
    report: str | None


def _report_dir(root: Path) -> str:
    cfg = root / ".preflight" / "config.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PreflightError(f"{cfg}: cannot parse config: {exc}") from exc
        if isinstance(data, dict) and isinstance(data.get("report_dir"), str):
            return data["report_dir"]
    return Config().report_dir


def _load_report(root: Path, last_report: str | None) -> dict | None:
    if not last_report:
        return None
    path = report_paths(root, _report_dir(root), last_report)["json"]
    if not path.is_file():
        raise PreflightError(f"report file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PreflightError(f"{path}: corrupt report json: {exc}") from exc


def resolve_finding(root: Path, ref: str, branch: str | None) -> ResolvedFinding:
    """Resolve `F<n>` (newest report) or a 12-hex stable id for this branch."""
    root = Path(root)
    is_run = bool(_RUN_ID_RE.fullmatch(ref))
    if not is_run and not _STABLE_RE.fullmatch(ref):
        raise PreflightError(
            f"unknown finding reference {ref!r}: use F<n> (e.g. F1) or a 12-hex stable id"
        )
    key = branch or "HEAD"
    state = load_state(root)
    bs = state.branches.get(key)
    if bs is None:
        raise PreflightError(f"no review state for branch {key!r}: run `preflight-review review` first")
    report = _load_report(root, bs.last_report)
    entries: list[dict] = []
    if report:
        entries = list(report.get("findings", [])) + list(report.get("unverified", []))
    for e in entries:
        if (is_run and e.get("run_id") == ref) or (not is_run and e.get("stable_id") == ref):
            return ResolvedFinding(
                e["stable_id"], e.get("run_id"), e.get("file"), e.get("title"), bs.last_report
            )
    if not is_run and ref in bs.findings:
        return ResolvedFinding(ref, None, None, None, bs.last_report)
    where = f"report {bs.last_report}" if bs.last_report else "state"
    raise PreflightError(f"finding {ref!r} not found in {where} for branch {key!r}")


def _glob_for(file: str | None) -> str:
    if not file or "/" not in file:
        return "**"
    return file.rsplit("/", 1)[0] + "/**"


def respond(
    repo_root: Path,
    finding_ref: str,
    label: str,
    reason: str | None = None,
    learn: bool = False,
    branch: str | None = None,
) -> RespondResult:
    if label not in LABELS:
        raise PreflightError(f"invalid label {label!r}: choose one of {', '.join(LABELS)}")
    root = gitutil.toplevel(Path(repo_root))
    if branch is None:
        branch = gitutil.current_branch(root)
    found = resolve_finding(root, finding_ref, branch)
    state = load_state(root)
    bs = state.branches[branch or "HEAD"]
    fs = bs.findings.get(found.stable_id)
    if fs is None:
        fs = FindingState(status="open", first_round=bs.round, last_seen_round=bs.round)
        bs.findings[found.stable_id] = fs
    fs.status = LABELS[label]
    fs.response = label
    fs.reason = reason
    if label == "fixed":
        fs.addressed_sha = gitutil.rev_parse("HEAD", root)
    save_state(root, state)

    message = f"{found.run_id or found.stable_id} ({found.stable_id}): {fs.status} ({label})"
    learning_id = None
    if learn:
        if not found.title:
            raise PreflightError(
                f"cannot learn from {found.stable_id}: it is not in the latest report"
            )
        learning_id = next_id(read_learnings(root, None))
        append_learning(root, Learning(
            id=learning_id,
            date=date.today().isoformat(),
            path_glob=_glob_for(found.file),
            text=f"Do not flag: {found.title}. Reason: {reason}",
            source_finding=f"{found.run_id} ({found.stable_id})",
            uses=0,
        ))
        message += (
            f"; learning {learning_id} added (takes effect for reviews once "
            f".preflight/learnings.md is committed to the base branch)"
        )

    log = root / RESPONSES_PATH
    log.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "branch": branch,
        "stable_id": found.stable_id,
        "run_id": found.run_id,
        "label": label,
        "reason": reason,
        "report": found.report,
    }
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    return RespondResult(found.stable_id, fs.status, learning_id, message)
