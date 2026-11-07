"""Persisted per-branch review state (.preflight/state.json)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from preflight_review.errors import StateError

STATE_PATH = Path(".preflight/state.json")


@dataclass
class FindingState:
    status: str  # open | addressed | dismissed
    response: str | None = None
    reason: str | None = None
    first_round: int = 1
    last_seen_round: int = 1
    addressed_sha: str | None = None
    title: str | None = None
    file: str | None = None
    start_line: int | None = None


@dataclass
class BranchState:
    last_reviewed_sha: str | None = None
    round: int = 0
    diff_hash: str | None = None
    last_report: str | None = None
    findings: dict[str, FindingState] = field(default_factory=dict)
    # A run paused by the usage limit: `review --resume` reuses its saved calls while the
    # diff is unchanged. Round and findings state are not advanced by a paused run.
    paused_report: str | None = None
    paused_diff_hash: str | None = None


@dataclass
class State:
    version: int = 1
    branches: dict[str, BranchState] = field(default_factory=dict)


def load_state(repo_root: Path) -> State:
    path = Path(repo_root) / STATE_PATH
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1:
            raise StateError(f"{path}: unsupported state version")
        branches = {}
        for name, b in data.get("branches", {}).items():
            findings = {k: FindingState(**v) for k, v in b.get("findings", {}).items()}
            branches[name] = BranchState(**{**b, "findings": findings})
        return State(version=1, branches=branches)
    except StateError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise StateError(f"{path}: corrupt state: {exc}") from exc


def save_state(repo_root: Path, state: State) -> None:
    path = Path(repo_root) / STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(state), fh, sort_keys=True, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
