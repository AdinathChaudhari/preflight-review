"""Report filename contract: <repo>-PR<##>-preflight-review-<##>.{md,json}."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def slugify_branch(name: str) -> str:
    slug = name.lower().replace("/", "-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def pr_label(repo_root: Path, branch: str | None, gh_runner=subprocess.run) -> str:
    """`PR07` when the branch has an open PR, else `PR-<branch-slug>`."""
    if branch is None:
        return "PR-detached"
    try:
        proc = gh_runner(
            ["gh", "pr", "view", "--json", "number,state"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise ValueError("gh failed")
        data = json.loads(proc.stdout)
        if data["state"] != "OPEN":
            raise ValueError("PR not open")
        return f"PR{int(data['number']):02d}"
    except Exception:  # gh missing, timeout, no PR, junk output: all mean "no PR"
        return f"PR-{slugify_branch(branch)}"


def report_basename(repo_name: str, pr_label: str, round: int) -> str:
    return f"{repo_name}-{pr_label}-preflight-review-{round:02d}"


def report_paths(repo_root: Path, report_dir: str, basename: str) -> dict[str, Path]:
    base = Path(repo_root) / report_dir
    return {
        "md": base / f"{basename}.md",
        "json": base / f"{basename}.json",
        "manifest": base / f"{basename}.manifest.json",
        "latest": base / "latest.md",
    }
