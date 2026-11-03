"""Pre-filter and clustering of raw findings (LLM lenses plus deterministic tools)."""

from __future__ import annotations

import re

from preflight_review import findings as findings_mod
from preflight_review.config import Config
from preflight_review.context.learnings import Learning
from preflight_review.findings import Category, Evidence, Finding, Lane, severity_rank
from preflight_review.scope import Scope, glob_to_regex, is_in_diff

DOC_SUFFIXES = (".md", ".rst", ".txt")

COARSE: dict[Category, str] = {
    Category.security: "security",
    Category.secrets: "security",
    Category.correctness: "correctness",
    Category.contracts: "correctness",
    Category.error_handling: "correctness",
    Category.data_integrity: "data",
    Category.concurrency: "data",
    Category.maintainability: "style",
    Category.rules: "style",
    Category.domain: "style",
    Category.dependency: "style",
}

_WIDEN = 2
_WORD_RE = re.compile(r"[a-z0-9_]+")
_STOP = {"flag", "report"}
_PREFIXES = ("do not flag", "do not report")


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 4 and w not in _STOP}


def _suppressing_learning(f: Finding, learnings: list[Learning]) -> Learning | None:
    """Heuristic learnings suppression (M4 may replace it with a model call).

    A learning applies only if its lowercase text starts with "do not flag" or
    "do not report" and its path_glob matches the finding's file. It suppresses
    the finding when the learning text and the finding's title+claim share at
    least 2 content words (lowercase [a-z0-9_]+ tokens of length >= 4, excluding
    "flag"/"report" and the "do not" prefix).
    """
    fwords = _content_words(f"{f.title} {f.claim}")
    for l in learnings:
        low = l.text.lower().strip()
        prefix = next((p for p in _PREFIXES if low.startswith(p)), None)
        if prefix is None or not glob_to_regex(l.path_glob).match(f.file):
            continue
        if len(_content_words(low[len(prefix):]) & fwords) >= 2:
            return l
    return None


def _is_doc(path: str) -> bool:
    return path.lower().endswith(DOC_SUFFIXES)


def _cites_changed_code(f: Finding, scope: Scope) -> bool:
    """True when any evidence entry points at a non-doc file changed in this diff.

    A finding on a doc that quotes changed code is a doc-code consistency claim
    (e.g. a ledger describing a guard the branch removed), not a prose nitpick.
    File-level on purpose: when code is added then removed on the branch, the
    cited line is unchanged context, but its file is still in the diff.
    """
    changed = {fc.path for fc in scope.files}
    return any(e.file in changed and not _is_doc(e.file) for e in f.evidence)


def _parse_caused_by(value: str) -> tuple[str, int] | None:
    path, sep, line = value.rpartition(":")
    if not sep or not path or not line.strip().isdigit():
        return None
    return path, int(line)


def prefilter(
    findings: list[Finding],
    scope: Scope,
    config: Config,
    learnings: list[Learning],
) -> tuple[list[Finding], list[tuple[Finding, str]], list[tuple[Finding, str]]]:
    """Return (kept, suppressed, dropped). The input list is not reordered."""
    instr = [glob_to_regex(pi.path) for pi in config.path_instructions]
    kept: list[Finding] = []
    suppressed: list[tuple[Finding, str]] = []
    dropped: list[tuple[Finding, str]] = []
    for f in findings:
        if (
            _is_doc(f.file)
            and not any(r.match(f.file) for r in instr)
            and not _cites_changed_code(f, scope)
        ):
            dropped.append((f, "docs file"))
            continue
        learning = _suppressing_learning(f, learnings)
        if learning is not None:
            f.suppressed_by = learning.id if learning.id.startswith("L") else f"L{learning.id}"
            suppressed.append((f, learning.id))
            continue
        if f.lane == Lane.outside_diff:
            parsed = _parse_caused_by(f.caused_by) if f.caused_by else None
            if parsed is None or not is_in_diff(scope, parsed[0], parsed[1]):
                f.lane = Lane.pre_existing
        kept.append(f)
    return kept, suppressed, dropped


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _overlap(a: Finding, b: Finding) -> bool:
    return a.start_line - _WIDEN <= b.end_line + _WIDEN and b.start_line - _WIDEN <= a.end_line + _WIDEN


def cluster(findings: list[Finding]) -> list[Finding]:
    """Merge same-file, same-coarse-group findings whose widened ranges overlap."""
    n = len(findings)
    parent = list(range(n))
    for i in range(n):
        for j in range(i + 1, n):
            a, b = findings[i], findings[j]
            if (
                a.file == b.file
                and COARSE[a.category] == COARSE[b.category]
                and _overlap(a, b)
            ):
                parent[_find(parent, j)] = _find(parent, i)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(_find(parent, i), []).append(i)

    out: list[Finding] = []
    for members_idx in sorted(groups.values(), key=lambda g: g[0]):
        members = [findings[i] for i in members_idx]
        if len(members) == 1:
            out.append(members[0])
            continue
        primary = next((m for m in members if m.source.startswith("llm")), None)
        if primary is None:
            primary = max(members, key=lambda m: (severity_rank(m.severity), -members.index(m)))
        for m in members:
            if m is not primary and not m.source.startswith("llm"):
                primary.evidence.append(
                    Evidence(m.file, m.start_line, f"{m.source} {m.rule_id}: {m.title}")
                )
        primary.severity = max((m.severity for m in members), key=severity_rank)
        primary.cluster_members = [m.stable_id or findings_mod.stable_id(m) for m in members]
        primary.start_line = min(m.start_line for m in members)
        primary.end_line = max(m.end_line for m in members)
        out.append(primary)
    return out
