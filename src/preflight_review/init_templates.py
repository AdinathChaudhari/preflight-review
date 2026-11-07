"""`preflight-review init`: templates and the idempotent repo scaffolder."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from preflight_review import config as config_mod
from preflight_review import gitutil
from preflight_review.errors import ConfigError

CONFIG_YAML = config_mod.DEFAULT_CONFIG_YAML

RULES_MD = """\
# Repo rules for preflight-review

## What failure looks like in this repo
<!-- Example: A request handler that returns another tenant's data is a critical bug. -->

## Always check
<!-- Example: New API routes must have an auth check and a test. -->
- When a commit on this branch removes or renames a function, class, field, flag or
  guard, search the branch's changed docs (`*.md`, `*.rst`, `*.txt`, ledgers, indexes,
  changelogs) for the old name. A doc that still describes the removed behaviour is a
  finding on the doc, with the changed code file as evidence.

## Workflow
- After any commit that answers review findings, run
  `preflight-review review --incremental` before pushing. A fix commit can leave prose
  written earlier in the branch describing the old design.

## Nit policy
<!-- Example: Ignore formatting nits; ruff handles style. -->
"""

LEARNINGS_MD = """\
# Learnings

<!-- Entries are added by `preflight-review learn`; edit or delete them freely. -->
"""

PREFLIGHT_GITIGNORE = "reviews/\nstate.json\ncache/\nintent.md\nruns/\n"



@dataclass
class InitResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    coderabbit_imported: bool = False
    guideline_files: list[str] = field(default_factory=list)
    message: str = ""


def _coderabbit_config_text(root: Path) -> str | None:
    cr_path = root / ".coderabbit.yaml"
    if not cr_path.is_file():
        return None
    try:
        cr = yaml.safe_load(cr_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f".coderabbit.yaml: invalid YAML: {exc}") from exc
    reviews = cr.get("reviews") if isinstance(cr, dict) else None
    if not isinstance(reviews, dict):
        return None
    data = yaml.safe_load(CONFIG_YAML)
    # reviews.profile is not imported: a CodeRabbit `chill` would override the exhaustive
    # `assertive` default. Only path filters and path instructions carry over.
    imported = False
    filters = reviews.get("path_filters")
    if isinstance(filters, list) and filters:
        data["path_filters"] = [str(f) for f in filters]
        imported = True
    instr = reviews.get("path_instructions")
    if isinstance(instr, list):
        items = [
            {"path": str(i["path"]), "instructions": str(i["instructions"])}
            for i in instr
            if isinstance(i, dict) and "path" in i and "instructions" in i
        ]
        if items:
            data["path_instructions"] = items
            imported = True
    if not imported:
        return None
    config_mod.config_from_dict(data)  # raises ConfigError if invalid
    return (
        "# preflight-review config (imported from .coderabbit.yaml)\n"
        + yaml.safe_dump(data, sort_keys=False)
    )


def _guideline_files(root: Path) -> list[str]:
    found = []
    for p in gitutil.ls_files(root):
        if p in ("CLAUDE.md", "AGENTS.md") or p.endswith(("/CLAUDE.md", "/AGENTS.md")):
            found.append(p)
        elif p.startswith(".claude/rules/") and p.endswith(".md"):
            found.append(p)
    return sorted(found)


def init_repo(
    repo_root: Path, force: bool = False, with_ast_rules: bool = False
) -> InitResult:
    root = gitutil.toplevel(Path(repo_root))
    pf = root / ".preflight"
    result = InitResult()

    cr_text = _coderabbit_config_text(root)
    config_text = cr_text if cr_text is not None else CONFIG_YAML
    files = [
        (pf / "config.yaml", config_text),
        (pf / "RULES.md", RULES_MD),
        (pf / "learnings.md", LEARNINGS_MD),
        (pf / ".gitignore", PREFLIGHT_GITIGNORE),
    ]
    pf.mkdir(parents=True, exist_ok=True)
    for path, text in files:
        if path.exists() and not force:
            result.skipped.append(path)
            continue
        path.write_text(text, encoding="utf-8")
        result.created.append(path)
        if path.name == "config.yaml" and cr_text is not None:
            result.coderabbit_imported = True

    rules_dir = pf / "ast-rules"
    if rules_dir.is_dir():
        result.skipped.append(rules_dir)
    else:
        rules_dir.mkdir(parents=True)
        result.created.append(rules_dir)

    rules_copied = 0
    if with_ast_rules:
        src_dir = resources.files("preflight_review").joinpath("ast_rules", "rules")
        for entry in sorted(src_dir.iterdir(), key=lambda e: e.name):
            if not entry.name.endswith(".yml"):
                continue
            dest = rules_dir / entry.name
            if dest.exists() and not force:
                result.skipped.append(dest)
                continue
            dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
            result.created.append(dest)
            rules_copied += 1

    result.guideline_files = _guideline_files(root)

    def rel(p: Path) -> str:
        return str(p.relative_to(root))

    lines = ["preflight-review init"]
    lines += [f"  created: {rel(p)}" for p in result.created]
    lines += [f"  skipped (exists): {rel(p)}" for p in result.skipped]
    if with_ast_rules:
        lines.append(f"  installed {rules_copied} bundled ast-grep rule(s) in .preflight/ast-rules/")
    if result.coderabbit_imported:
        lines.append("  imported settings from .coderabbit.yaml")
    if result.guideline_files:
        lines.append("  guidelines found: " + ", ".join(result.guideline_files))
    else:
        lines.append("  guidelines found: none (consider adding CLAUDE.md)")
    result.message = "\n".join(lines)
    return result
