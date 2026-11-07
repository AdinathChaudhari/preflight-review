"""Pre-filter and clustering of raw findings (LLM lenses plus deterministic tools)."""

from __future__ import annotations

import re

from preflight_review import findings as findings_mod
from preflight_review.config import Config
from preflight_review.context.learnings import Learning
from preflight_review.context.risk import is_prose_doc
from preflight_review.findings import (
    Category,
    Evidence,
    Finding,
    Lane,
    Severity,
    severity_rank,
)
from preflight_review.scope import Scope, glob_to_regex, is_in_diff
from preflight_review.stages.finders import lens_matches, source_lens

# docs_review: auto -- when docs are at least this share of changed lines, low-signal doc
# findings are dropped (see _low_signal_doc).
DOCS_HEAVY_SHARE = 0.5
_DOC_KEEP_CATEGORIES = {Category.correctness, Category.contracts, Category.data_integrity}
_INTERNAL_CATEGORIES = {Category.correctness, Category.contracts}

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
# Never merged into another finding: a merge would make an LLM finding the primary, and a
# REFUTED verdict on it would then drop a finding that must always be reported.
UNMERGED_SOURCES = frozenset({"secret-files"})
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
    """A prose doc: a doc suffix and risk tier "docs".

    Prompt files and agent-instruction files (SKILL.md, CLAUDE.md, AGENTS.md, .claude/**)
    have risk tier "source": they change behaviour, so findings on them are kept like code.
    """
    return is_prose_doc(path)


def _cites_changed_file(f: Finding, scope: Scope, docs_review: str = "auto") -> bool:
    """True when the doc finding is anchored in more than its own single quote.

    Kept: evidence in any *other* file changed in this diff, code or doc (a doc-code
    drift claim, or a doc-vs-doc contradiction such as two status files disagreeing),
    or at least 2 quotes from the doc itself in category correctness or contracts (an
    internal contradiction). File-level on purpose: when code is added then removed on
    the branch, the cited line is unchanged context, but its file is still in the diff.

    Under docs_review auto|on, a finding anchored on a changed line of the doc may also
    cite an unchanged file that exists in the repository: new doc text that contradicts
    unchanged code or config, or names a command or path that is not there, is what the
    doc-consistency review asks for. Drift on an unchanged doc line stays dropped.
    """
    changed = {fc.path for fc in scope.files}
    if any(e.file in changed and e.file != f.file for e in f.evidence):
        return True
    if (
        docs_review != "off"
        and is_in_diff(scope, f.file, f.start_line)
        and any(e.file != f.file and (scope.repo_root / e.file).is_file() for e in f.evidence)
    ):
        return True
    same_doc = sum(1 for e in f.evidence if e.file == f.file)
    return same_doc >= 2 and f.category in _INTERNAL_CATEGORIES


def _gated_lens_doc(f: Finding) -> bool:
    """A finding from a path-gated lens on a file inside its gate (a deploy runbook for
    deploy_ci): reviewing those docs is the lens's job, so the docs drop does not apply."""
    spec = source_lens(f.source)
    return spec is not None and bool(spec.paths) and lens_matches(spec, f.file)


def docs_share(scope: Scope) -> float:
    """Share of changed lines (added + deleted) that are in prose docs."""
    total = sum(fc.added_lines + fc.deleted_lines for fc in scope.files)
    if not total:
        return 0.0
    docs = sum(fc.added_lines + fc.deleted_lines for fc in scope.files if _is_doc(fc.path))
    return docs / total


def _low_signal_doc(f: Finding) -> bool:
    """Doc findings kept in a docs-heavy diff: correctness, contracts or data_integrity at
    minor or above, or any category at major or above. Never style nits."""
    if f.lane == Lane.nitpick:
        return True
    rank = severity_rank(f.severity)
    if rank >= severity_rank(Severity.major):
        return False
    return not (f.category in _DOC_KEEP_CATEGORIES and rank >= severity_rank(Severity.minor))


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
    docs_heavy = config.docs_review == "auto" and docs_share(scope) >= DOCS_HEAVY_SHARE
    for f in findings:
        # The docs drop targets model findings: a deterministic tool hit (a committed
        # `.env.txt`, a gitleaks match in README.md, an osv advisory on requirements.txt)
        # is about the file itself, not doc wording.
        if (
            f.source.startswith("llm")
            and _is_doc(f.file)
            and not any(r.match(f.file) for r in instr)
            and not _gated_lens_doc(f)
        ):
            if not _cites_changed_file(f, scope, config.docs_review):
                dropped.append((f, "docs file"))
                continue
            if docs_heavy and _low_signal_doc(f):
                dropped.append((f, "docs file: low-signal finding in a docs-heavy diff"))
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
                and a.source not in UNMERGED_SOURCES
                and b.source not in UNMERGED_SOURCES
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
