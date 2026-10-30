"""`learn`: append a learning to .preflight/learnings.md."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from preflight_review import gitutil, prompts
from preflight_review.config import CONFIG_PATH
from preflight_review.context.learnings import (
    Learning,
    append_learning,
    next_id,
    read_learnings,
)
from preflight_review.errors import PreflightError
from preflight_review.model import ModelRequest
from preflight_review.model.schemas import load_schema
from preflight_review.respond import resolve_finding


def learn(
    repo_root: Path,
    text: str,
    path_glob: str = "**",
    from_ref: str | None = None,
) -> Learning:
    if not text or not text.strip():
        raise PreflightError("learning text is empty")
    root = gitutil.toplevel(Path(repo_root))
    source = None
    if from_ref:
        found = resolve_finding(root, from_ref, gitutil.current_branch(root))
        source = f"{found.run_id or from_ref} ({found.stable_id})"
    learning = Learning(
        id=next_id(read_learnings(root, None)),
        date=date.today().isoformat(),
        path_glob=path_glob or "**",
        text=text.strip(),
        source_finding=source,
        uses=0,
    )
    append_learning(root, learning)
    return learning


def list_learnings(repo_root: Path) -> list[Learning]:
    return read_learnings(gitutil.toplevel(Path(repo_root)), None)


PROPOSE_MAX_TURNS = 2
PROPOSE_BUDGET_USD = 0.25


def _learnings_block(learnings: list[Learning]) -> str:
    groups: dict[str, list[Learning]] = {}
    for l in learnings:
        groups.setdefault(l.path_glob, []).append(l)
    blocks = []
    for glob, items in groups.items():
        lines = [f"glob: {glob}"]
        for l in items:
            lines.append(f"{l.id}: {' '.join(l.text.split())}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def propose_path_instructions(repo_root: Path, backend, model: str = "sonnet") -> list[dict]:
    root = gitutil.toplevel(Path(repo_root))
    learnings = read_learnings(root, None)
    if not learnings:
        return []
    req = ModelRequest(
        role="propose",
        model=model,
        system_prompt="",
        prompt=prompts.render("propose", learnings=_learnings_block(learnings)),
        schema=load_schema("propose"),
        cwd=root,
        tools=[],
        max_turns=PROPOSE_MAX_TURNS,
        max_budget_usd=PROPOSE_BUDGET_USD,
    )
    res = backend.run(req)
    if not res.ok or res.structured is None:
        raise PreflightError(f"propose failed: {res.subtype}: {res.error or 'no output'}")
    known = {l.id for l in learnings}
    out: list[dict] = []
    for p in res.structured.get("proposals", []):
        ids = [str(i) for i in p.get("from_learnings", [])]
        instructions = str(p.get("instructions", "")).strip()
        path = str(p.get("path", "")).strip()
        if not instructions or not path or not ids or any(i not in known for i in ids):
            continue
        out.append({"path": path, "instructions": instructions, "from_learnings": ids})
    return out


def format_proposals_yaml(proposals: list[dict]) -> str:
    lines = ["path_instructions:"]
    for p in proposals:
        lines.append(f"  # from learnings: {', '.join(p['from_learnings'])}")
        dumped = yaml.safe_dump(
            [{"path": p["path"], "instructions": p["instructions"]}],
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        )
        lines.extend("  " + ln for ln in dumped.rstrip("\n").splitlines())
    return "\n".join(lines)


def apply_proposals(repo_root: Path, proposals: list[dict]) -> Path:
    root = gitutil.toplevel(Path(repo_root))
    path = root / CONFIG_PATH
    if not path.is_file():
        raise PreflightError(f"{CONFIG_PATH} not found; run `preflight-review init` first")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PreflightError(f"{CONFIG_PATH} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PreflightError(f"{CONFIG_PATH} must be a YAML mapping")
    existing = data.get("path_instructions")
    if not isinstance(existing, list):
        existing = []
    for p in proposals:
        entry = {"path": p["path"], "instructions": p["instructions"]}
        if entry not in existing:
            existing.append(entry)
    data["path_instructions"] = existing
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path
