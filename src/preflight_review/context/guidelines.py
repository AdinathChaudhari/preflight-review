"""Guideline discovery: CLAUDE.md, AGENTS.md, rules dirs, RULES.md, path instructions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from preflight_review import gitutil
from preflight_review.config import Config
from preflight_review.context.tokens import estimate_tokens
from preflight_review.scope import glob_to_regex

MAX_GUIDELINE_TOKENS = 6_000
_TRUNCATED = "\n[truncated]"


@dataclass
class Guideline:
    source_path: str
    scope_dir: str  # repo-relative dir ("" = repo root) or a glob for path_instruction
    text: str
    kind: str  # claude_md | agents_md | rules_dir | preflight_rules | path_instruction | cursorrules


def _cap(text: str) -> str:
    if estimate_tokens(text) <= MAX_GUIDELINE_TOKENS:
        return text
    cut = text.encode("utf-8")[: MAX_GUIDELINE_TOKENS * 4]
    return cut.decode("utf-8", errors="ignore") + _TRUNCATED


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _dirs_for(changed_paths: list[str]) -> list[str]:
    """Repo root plus every ancestor dir of each changed path, shallowest first."""
    dirs: set[str] = {""}
    for p in changed_paths:
        parts = PurePosixPath(p).parts[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return sorted(dirs, key=lambda d: (d.count("/") + 1 if d else 0, d))


def _in_repo_files(repo_root: Path, d: str) -> list[tuple[str, str]]:
    """(relative path, kind) of guideline files that exist in repo dir `d`."""
    base = repo_root / d if d else repo_root
    out: list[tuple[str, str]] = []

    def rel(name: str) -> str:
        return f"{d}/{name}" if d else name

    if (base / "CLAUDE.md").is_file():
        out.append((rel("CLAUDE.md"), "claude_md"))
    if (base / "AGENTS.md").is_file():
        out.append((rel("AGENTS.md"), "agents_md"))
    rules = base / ".claude" / "rules"
    if rules.is_dir():
        for f in sorted(rules.glob("*.md")):
            if f.is_file():
                out.append((rel(f".claude/rules/{f.name}"), "rules_dir"))
    if (base / ".cursorrules").is_file():
        out.append((rel(".cursorrules"), "cursorrules"))
    return out


def _ancestor_dirs(repo_root: Path, home: Path) -> list[Path]:
    """Parents of repo_root up to and including home, outermost first."""
    try:
        root = repo_root.resolve()
        home_r = home.resolve()
        root.relative_to(home_r)
    except (ValueError, OSError):
        return []
    out: list[Path] = []
    cur = root.parent
    while True:
        try:
            cur.relative_to(home_r)
        except ValueError:
            break
        out.append(cur)
        if cur == home_r:
            break
        cur = cur.parent
    out.reverse()
    return out


def discover_guidelines(
    repo_root: Path,
    changed_paths: list[str],
    config: Config,
    base_ref: str | None,
    home: Path | None = None,
) -> list[Guideline]:
    repo_root = Path(repo_root)
    home = Path.home() if home is None else Path(home)
    found: list[Guideline] = []

    if config.guidelines.discover:
        if config.guidelines.ancestors:
            for d in _ancestor_dirs(repo_root, home):
                f = d / "CLAUDE.md"
                text = _read(f) if f.is_file() else None
                if text is not None:
                    found.append(Guideline(str(f), "", text, "claude_md"))
        for d in _dirs_for(changed_paths):
            for rel, kind in _in_repo_files(repo_root, d):
                text = _read(repo_root / rel)
                if text is not None:
                    found.append(Guideline(rel, d, text, kind))

    if base_ref is not None:
        text = gitutil.show_file(base_ref, config.rules_file, repo_root)
        if text is not None:
            found.append(Guideline(config.rules_file, "", text, "preflight_rules"))

    for pi in config.path_instructions:
        rx = glob_to_regex(pi.path)
        if any(rx.match(p) for p in changed_paths):
            found.append(
                Guideline("config:path_instructions", pi.path, pi.instructions, "path_instruction")
            )

    out: list[Guideline] = []
    seen: set[str] = set()
    for g in found:
        # path instructions share one source_path; dedupe the rest by source_path
        if g.kind != "path_instruction":
            if g.source_path in seen:
                continue
            seen.add(g.source_path)
        g.text = _cap(g.text)
        out.append(g)
    return out
