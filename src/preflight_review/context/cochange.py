"""Git co-change mining: files that historically change together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from preflight_review.gitutil import run_git

_LOCKFILE_NAMES = {"package-lock.json", "uv.lock", "poetry.lock"}
MAX_PER_FILE = 5


@dataclass
class Companion:
    changed: str
    companion: str
    support: int
    total: int
    confidence: float


def _is_lockfile(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".lock") or name in _LOCKFILE_NAMES


def _transactions(repo_root: Path, n_commits: int, max_tx: int) -> list[set[str]]:
    out = run_git(
        ["log", "-n", str(n_commits), "--no-merges", "--name-only", "--pretty=format:@@%H"],
        repo_root,
        check=False,
    )
    txs: list[set[str]] = []
    cur: list[str] | None = None
    for line in out.split("\n"):
        if line.startswith("@@"):
            if cur is not None:
                txs.append(set(cur))
            cur = []
        elif line.strip() and cur is not None:
            cur.append(line.strip())
    if cur is not None:
        txs.append(set(cur))
    return [{p for p in t if not _is_lockfile(p)} for t in txs if len(t) <= max_tx]


def mine_cochange(
    repo_root: Path,
    changed_paths: list[str],
    n_commits: int = 500,
    min_support: int = 3,
    min_conf: float = 0.5,
    max_tx: int = 30,
) -> list[Companion]:
    txs = _transactions(Path(repo_root), n_commits, max_tx)
    changed_set = set(changed_paths)
    result: list[Companion] = []
    for changed in sorted(changed_set):
        with_changed = [t for t in txs if changed in t]
        total = len(with_changed)
        if total == 0:
            continue
        counts: dict[str, int] = {}
        for t in with_changed:
            for p in t:
                if p != changed and p not in changed_set:
                    counts[p] = counts.get(p, 0) + 1
        cands = [
            Companion(changed, p, n, total, n / total)
            for p, n in counts.items()
            if n >= min_support and n / total >= min_conf
        ]
        cands.sort(key=lambda c: (-c.confidence, -c.support, c.companion))
        result.extend(cands[:MAX_PER_FILE])
    result.sort(key=lambda c: (-c.confidence, -c.support, c.changed, c.companion))
    return result
