"""Seeded-error cases: load them from eval/seeded/cases and build throwaway git repos."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from preflight_review.errors import GitError, SchemaError
from preflight_review.evaluation.score import GoldenItem

TARGET = "app/target.py"

SEED_CONFIG = """\
version: 1
lenses: [correctness, contracts_tests, data_concurrency]
security_lane: true
walkthrough: false
backend: fake
tools:
  gitleaks: false
  ast_grep: false
  osv: false
"""


@dataclass
class SeededCase:
    name: str
    before: str
    after: str
    expected: list[GoldenItem] = field(default_factory=list)
    file: str = TARGET


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise SchemaError(f"seeded case file missing: {path}") from exc


def _read_meta_file(d: Path) -> str:
    """Return the case's target path from meta.json, or TARGET when absent."""
    meta = d / "meta.json"
    if not meta.is_file():
        return TARGET
    try:
        raw = json.loads(meta.read_text())
    except (OSError, ValueError) as exc:
        raise SchemaError(f"{d.name}/meta.json: invalid JSON: {exc}") from exc
    value = raw.get("file") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{d.name}/meta.json: 'file' must be a non-empty string")
    if "\\" in value or value.startswith("/") or PurePosixPath(value).is_absolute():
        raise SchemaError(f"{d.name}/meta.json: 'file' must be a relative posix path")
    if ".." in PurePosixPath(value).parts:
        raise SchemaError(f"{d.name}/meta.json: 'file' must not contain '..'")
    return value


def load_seeded_cases(directory: Path | str) -> list[SeededCase]:
    root = Path(directory)
    if not root.is_dir():
        raise SchemaError(f"seeded cases directory not found: {root}")
    cases: list[SeededCase] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        target = _read_meta_file(d)
        before = _read(d / "before.py")
        after = _read(d / "after.py")
        try:
            raw = json.loads(_read(d / "expected.json"))
        except ValueError as exc:
            raise SchemaError(f"{d.name}/expected.json: invalid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise SchemaError(f"{d.name}/expected.json: expected a list")
        expected: list[GoldenItem] = []
        for i, item in enumerate(raw):
            try:
                expected.append(
                    GoldenItem(
                        id=f"{d.name}:{i}",
                        file=target,
                        line_start=int(item["line_start"]),
                        line_end=int(item["line_end"]),
                        category=str(item["category"]),
                        severity=str(item["severity"]),
                        description=str(item["description"]),
                        kind="seeded",
                        is_real=True,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaError(f"{d.name}/expected.json[{i}]: bad item ({exc!r})") from exc
        cases.append(SeededCase(d.name, before, after, expected, file=target))
    return cases


def _git(repo: Path, *args: str) -> str:
    cmd = [
        "git", "-c", "user.name=preflight-eval", "-c", "user.email=eval@preflight.local",
        "-c", "commit.gpgsign=false", *args,
    ]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def materialise(case: SeededCase, tmp_path: Path | str) -> Path:
    """Create a repo with `main` (before) and branch `seed/<name>` (after); return its path."""
    repo = Path(tmp_path) / f"seed-{case.name}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in (
        (case.file, case.before),
        (".preflight/config.yaml", SEED_CONFIG),
    ):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{case.name}: before")
    _git(repo, "checkout", "-q", "-b", f"seed/{case.name}")
    (repo / case.file).write_text(case.after)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{case.name}: after")
    return repo
