"""Cache of files whose exact change was already reviewed cleanly.

A file is skipped on a later round only when the same change (path, status, old blob, new
blob) was reviewed under the same prompts and settings (`fingerprint`). Line numbers and
the merge base do not matter, so a rebase keeps the entry; a merge of main that changes the
old blob does not.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from preflight_review import prompts
from preflight_review.stages import finders

MAX_ENTRIES = 5000
_MODEL_KEYS = (
    "finder", "finder_deep", "finder_rules", "security", "sweep", "verifier", "adjudicator",
)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def fingerprint(
    config, mode: str, lenses: list[str], security_lane: bool, guidelines_text: str
) -> str:
    """Hash of everything that shapes what a review of one file would say.

    Covers the prompts (system, each lens, security, sweep, verifiers), lens set, security
    lane, mode, effort and turn limits, the configured models, verifier votes, guidelines
    text and path_instructions. Learnings are deliberately NOT included: adding a learning
    must not invalidate every cached file (learnings only suppress findings at report time).
    """
    parts: list[str] = [prompts.prompt_sha("system")]
    for lens in lenses:
        spec = finders.LENSES.get(lens)
        if spec is None:
            continue
        parts.append(f"{lens}={prompts.prompt_sha(spec.prompt_file)}")
    for name in ("security", "sweep", "verifier", "verifier_security"):
        parts.append(f"{name}={prompts.prompt_sha(name)}")
    parts.append("lenses=" + ",".join(sorted(lenses)))
    parts.append(f"security_lane={bool(security_lane)}")
    parts.append(f"mode={mode}")
    parts.append(f"effort={getattr(config.effort, mode, None)}")
    parts.append(f"turns={finders.MAX_TURNS.get(mode)}")
    models = config.models
    parts.append(
        "models=" + ",".join(f"{k}:{getattr(models, k, None)}" for k in _MODEL_KEYS)
    )
    parts.append(f"votes={config.verifier.votes}")
    parts.append("guidelines=" + _sha(guidelines_text or ""))
    pi = [
        {"path": getattr(i, "path", None), "instructions": getattr(i, "instructions", None)}
        for i in (config.path_instructions or [])
    ]
    parts.append("path_instructions=" + json.dumps(pi, sort_keys=True, ensure_ascii=False))
    return _sha("\n".join(parts))


def file_key(path: str, status: str, old_blob: str | None, new_blob: str, fp: str) -> str:
    return _sha("|".join([path, status, old_blob or "-", new_blob, fp]))


class ReviewedCache:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self.path = self.repo_root / ".preflight" / "cache" / "reviewed.json"
        self._data: dict[str, dict] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text())
                self._data = (
                    {k: v for k, v in data.items() if isinstance(v, dict)}
                    if isinstance(data, dict) else {}
                )
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._load().get(key)

    def put(
        self, key: str, *, run: str, round: int, branch: str, mode: str,
        findings: list[str], at: str | None = None,
    ) -> None:
        entry = {
            "run": run, "round": round, "branch": branch, "mode": mode,
            "findings": list(findings),
            "at": at or datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self._load()[key] = entry

    def save(self) -> None:
        with self._lock:
            data = self._load()
            if len(data) > MAX_ENTRIES:
                keep = sorted(data, key=lambda k: str(data[k].get("at", "")), reverse=True)
                for k in keep[MAX_ENTRIES:]:
                    del data[k]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
