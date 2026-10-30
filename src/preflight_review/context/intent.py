"""Review intent: branch name, commit subjects, intent.md, referenced issues."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from preflight_review.config import Config
from preflight_review.gitutil import run_git
from preflight_review.scope import Scope

MAX_SUBJECTS = 30
MAX_ISSUES = 3
MAX_ISSUE_CHARS = 1500
INTENT_FILE = ".preflight/intent.md"
_REF_RE = re.compile(r"#\d+|\b[A-Z]{2,}-\d+\b")


@dataclass
class Intent:
    branch: str | None
    commit_subjects: list[str]
    intent_md: str | None
    issue_refs: list[str]
    issues: list[dict] = field(default_factory=list)


def _fetch_issue(gh_runner: Callable[..., Any], number: str, cwd) -> dict:
    ref = f"#{number}"
    try:
        out = gh_runner(
            ["gh", "issue", "view", number, "--json", "title,body"], cwd
        )
        data = json.loads(out) if isinstance(out, (str, bytes)) else out
        if not isinstance(data, dict):
            raise ValueError("unexpected gh output")
        body = data.get("body") or ""
        if len(body) > MAX_ISSUE_CHARS:
            body = body[:MAX_ISSUE_CHARS] + "…"
        return {"ref": ref, "title": data.get("title", ""), "body": body}
    except Exception as exc:  # failures are recorded, never raised
        return {"ref": ref, "error": str(exc)}


def gather_intent(scope: Scope, config: Config, gh_runner: Callable[..., Any] | None = None) -> Intent:
    root = scope.repo_root
    out = run_git(["log", "--format=%s", f"{scope.base_sha}..HEAD"], root, check=False)
    subjects = [s for s in out.splitlines() if s.strip()][:MAX_SUBJECTS]

    md_path = root / INTENT_FILE
    intent_md = md_path.read_text(encoding="utf-8", errors="replace") if md_path.is_file() else None

    refs: list[str] = []
    for text in [scope.branch or "", *subjects]:
        for m in _REF_RE.finditer(text):
            if m.group(0) not in refs:
                refs.append(m.group(0))

    issues: list[dict] = []
    if config.intent.issues and gh_runner is not None:
        for ref in refs:
            if ref.startswith("#"):
                if len(issues) >= MAX_ISSUES:
                    break
                issues.append(_fetch_issue(gh_runner, ref[1:], root))
    return Intent(scope.branch, subjects, intent_md, refs, issues)
