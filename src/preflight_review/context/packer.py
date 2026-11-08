"""Deterministic, token-budgeted context packet.

The packet text is byte-identical for identical inputs: fixed section order,
sorted dict iteration, no timestamps, no set iteration.

Budget rules:
  * A large change is first split into batches by `plan_batches` (files ordered
    by risk tier then risk score (see `context.risk`), not by directory; co-change
    test companions sit next to their file; each batch's rendered diff <= the batch budget;
    a file over budget is split by hunks, a single huge hunk by line windows).
    One packet is built per batch; `build_packet(batch_note=...)` prefixes a
    `# Batch` section so the model knows other files live in other packets.
    Paths that cannot be packed are reported in `BatchPlan.oversized`.
  * `# Diff` is still capped at `diff_cap` estimated tokens as a safety net;
    overflow files (first-fit: a smaller later file may still fit) are listed
    under `# Not included (diff budget)` and in `Packet.omitted`.
    `Packet.files` lists the paths whose diff block is in the packet.
  * `# Context` is filled greedily (callees, callers, tests, co-change) until
    context_tokens would exceed diff_tokens * clamp(ratio, bounds); items that
    do not fit are named in `Packet.dropped`.
  * PacketBudgetError is raised only if the mandatory sections (Intent,
    Guidelines, Learnings, optional Prior review comments, Tool signals) alone exceed diff_cap * bounds[1].
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from preflight_review.config import Config
from preflight_review.context.callers import CallSite
from preflight_review.context.cochange import Companion
from preflight_review.context.guidelines import Guideline
from preflight_review.context.hunks import render_diff, render_file_hunks
from preflight_review.context.intent import Intent
from preflight_review.context.learnings import Learning
from preflight_review.context.retrieval import format_history_section
from preflight_review.context.risk import TIER_RANK, order_by_risk, risk_tier
from preflight_review.context.symbols import RepoIndex, Symbol
from preflight_review.context.tokens import estimate_tokens
from preflight_review.errors import PacketBudgetError
from preflight_review.findings import Finding, Lane
from preflight_review.scope import FileChange, Hunk, Scope

MAX_BODY_LINES = 40
MAX_TEST_LINES = 5
NONE = "(none)"
DEFAULT_BATCH_DIFF_TOKENS = 12_000
# Diff tokens per packet by depth: quick and standard reads can carry more per call.
PACKET_DIFF_TOKENS = {"quick": 32_000, "standard": 24_000, "deep": 16_000}
MIN_WINDOW_LINES = 20


@dataclass
class PacketSection:
    name: str
    text: str
    tokens: int
    priority: int


@dataclass
class Packet:
    text: str
    sections: list[PacketSection]
    diff_tokens: int
    context_tokens: int
    dropped: list[str]
    sha: str
    files: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)


def _section(heading: str, body: str) -> str:
    return f"# {heading}\n{body.rstrip(chr(10)) or NONE}"


def _intent_body(intent: Intent) -> str:
    lines: list[str] = []
    if intent.branch:
        lines.append(f"Branch: {intent.branch}")
    if intent.commit_subjects:
        lines.append("Commits (newest first):")
        lines += [f"- {s}" for s in intent.commit_subjects]
    if intent.intent_md:
        lines.append("Intent notes:")
        lines.append(intent.intent_md.rstrip("\n"))
    for issue in intent.issues:
        if "error" in issue:
            lines.append(f"Issue {issue.get('ref')}: (unavailable)")
        else:
            lines.append(f"Issue {issue.get('ref')}: {issue.get('title', '')}")
            if issue.get("body"):
                lines.append(str(issue["body"]).rstrip("\n"))
    return "\n".join(lines)


def _guidelines_body(guidelines: list[Guideline]) -> str:
    blocks = [
        f"source: {g.source_path}\napplies to: {g.scope_dir or '(repo)'}\n{g.text.rstrip(chr(10))}"
        for g in guidelines
    ]
    return "\n\n".join(blocks)


def _learnings_body(learnings: list[Learning]) -> str:
    return "\n".join(f"[{l.id}] ({l.path_glob}) {l.text}" for l in learnings)


def _signals_body(findings: list[Finding], candidates: list[str] | None = None) -> str:
    lines = [
        f"{f.rule_id or f.source} {f.file}:{f.start_line} {f.title}"
        for f in findings
        if f.lane == Lane.inline
    ]
    return "\n".join(lines + list(candidates or []))


def _diff_section(
    scope: Scope, index: RepoIndex, diff_cap: int
) -> tuple[str, int, list[str], list[str]]:
    def _dedupe(paths: list[str]) -> list[str]:
        return list(dict.fromkeys(paths))

    full = render_diff(scope, index)
    if estimate_tokens(full) <= diff_cap or not scope.files:
        body = full
        included = _dedupe([fc.path for fc in scope.files if render_file_hunks(fc, index)])
        return body.rstrip("\n"), estimate_tokens(body) if body else 0, included, []
    ordered = sorted(scope.files, key=lambda f: (-f.added_lines, f.path))
    kept: list[str] = []
    included: list[str] = []
    used = 0
    omitted: list[str] = []
    for fc in ordered:
        block = render_diff(dataclasses.replace(scope, files=[fc]), index).rstrip("\n")
        if not block:
            continue
        cost = estimate_tokens(block)
        if used + cost > diff_cap:
            omitted.append(fc.path)
            continue
        kept.append(block)
        included.append(fc.path)
        used += cost
    included = _dedupe(included)
    omitted = [p for p in _dedupe(omitted) if p not in set(included)]
    body = "\n\n".join(kept)
    tokens = estimate_tokens(body) if body else 0
    if omitted:
        body += ("\n\n" if body else "") + "# Not included (diff budget)\n" + "\n".join(
            f"- {p}" for p in omitted
        )
    return body, tokens, included, omitted


@dataclass
class Batch:
    index: int  # 1-based, plan order
    files: list[FileChange]  # pass as Scope.files; a split file is a FileChange copy with a hunk subset
    paths: list[str]  # distinct paths in this batch, in order
    split: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (part_no, n_parts)
    diff_tokens: int = 0  # estimate_tokens(render_diff(batch scope))
    mode: str = ""  # depth of this packet when planned with file_modes, else ""


@dataclass
class BatchPlan:
    batches: list[Batch] = field(default_factory=list)
    # (path, reason): binary / no textual diff -- informational, not coverage loss
    not_reviewable: list[tuple[str, str]] = field(default_factory=list)
    # (path, reason): reviewable content that could not be packed -- coverage loss
    oversized: list[tuple[str, str]] = field(default_factory=list)
    # path -> risk tier, for every path in scope (including unpackable ones)
    risk_tiers: dict[str, str] = field(default_factory=dict)


def _block(scope: Scope, index: RepoIndex, fc: FileChange) -> str:
    return render_diff(dataclasses.replace(scope, files=[fc]), index).rstrip("\n")


def _joined_tokens(blocks: list[str]) -> int:
    return estimate_tokens("\n\n".join(blocks) + "\n") if blocks else 0


def _split_hunk_windows(
    scope: Scope, index: RepoIndex, fc: FileChange, h: Hunk, budget: int
) -> list[FileChange] | None:
    """Split one oversized hunk into new-file line windows; None if impossible."""
    if h.new_len <= 0:
        return None
    last = h.new_start + h.new_len - 1
    size = h.new_len
    while True:
        wins: list[FileChange] = []
        a = h.new_start
        first = True
        while a <= last:
            b = min(a + size - 1, last)
            clipped = [(max(lo, a), min(hi, b)) for lo, hi in h.added_ranges if hi >= a and lo <= b]
            sub = Hunk(
                old_start=h.old_start,
                old_len=0,
                new_start=a,
                new_len=b - a + 1,
                lines=[ln for ln in h.lines if ln.startswith("-")] if first else [],
                added_ranges=clipped,
            )
            if first:
                sub.old_len = h.old_len
            wins.append(dataclasses.replace(fc, hunks=[sub]))
            first = False
            a = b + 1
        if all(estimate_tokens(_block(scope, index, w)) <= budget for w in wins):
            return wins
        if size < MIN_WINDOW_LINES:
            return None
        size = max(size // 2, 1)


def _split_file(
    scope: Scope, index: RepoIndex, fc: FileChange, budget: int
) -> list[FileChange] | None:
    parts: list[FileChange] = []
    cur: list[Hunk] = []
    for h in fc.hunks:
        trial = cur + [h]
        if estimate_tokens(_block(scope, index, dataclasses.replace(fc, hunks=trial))) <= budget:
            cur = trial
            continue
        if cur:
            parts.append(dataclasses.replace(fc, hunks=cur))
            cur = []
        if estimate_tokens(_block(scope, index, dataclasses.replace(fc, hunks=[h]))) <= budget:
            cur = [h]
            continue
        wins = _split_hunk_windows(scope, index, fc, h, budget)
        if wins is None:
            return None
        parts.extend(wins)
    if cur:
        parts.append(dataclasses.replace(fc, hunks=cur))
    return parts


def context_bounds(diff_budget: int) -> tuple[float, float]:
    """(lo, hi) context-to-diff ratio bounds for `build_packet`: a large packet gets a
    tighter ceiling so its context section stays around 36k tokens."""
    return (0.5, 1.5) if diff_budget >= 24_000 else (0.5, 3.0)


def plan_batches(
    scope: Scope,
    index: RepoIndex,
    diff_budget: int = DEFAULT_BATCH_DIFF_TOKENS,
    companions: list[Companion] | None = None,
    fan_in: dict[str, int] | None = None,
    *,
    file_modes: dict[str, str] | None = None,
    budget_by_mode: dict[str, int] | None = None,
) -> BatchPlan:
    plan = BatchPlan()
    plan.risk_tiers = {fc.path: risk_tier(fc.path) for fc in scope.files}
    by_path: dict[str, FileChange] = {}
    for fc in scope.files:
        if fc.is_binary:
            plan.not_reviewable.append((fc.path, "binary file"))
        elif not render_file_hunks(fc, index):
            plan.not_reviewable.append((fc.path, "no textual diff"))
        else:
            by_path[fc.path] = fc
    order = order_by_risk(
        [(p, by_path[p].added_lines + by_path[p].deleted_lines) for p in by_path], fan_in
    )
    for c in sorted(companions or [], key=lambda c: (c.changed, c.companion)):
        if (
            c.changed in by_path and c.companion in by_path and c.changed != c.companion
            and TIER_RANK[risk_tier(c.companion)] <= TIER_RANK["test"]
        ):
            order.remove(c.companion)
            order.insert(order.index(c.changed) + 1, c.companion)

    modes: dict[str, str] = {}
    if file_modes is not None:
        modes = {p: file_modes.get(p, "standard") for p in order}
        for c in companions or []:  # a companion follows its changed file's mode
            if c.changed in modes and c.companion in modes and c.changed != c.companion \
                    and TIER_RANK[risk_tier(c.companion)] <= TIER_RANK["test"]:
                modes[c.companion] = modes[c.changed]
        # within each risk tier, deep files first; a companion stays next to its file
        tier_of = {p: TIER_RANK[risk_tier(p)] for p in order}
        pos = {p: i for i, p in enumerate(order)}
        followers = {
            c.companion: c.changed for c in companions or []
            if c.changed in by_path and c.companion in by_path and c.changed != c.companion
            and TIER_RANK[risk_tier(c.companion)] <= TIER_RANK["test"]
        }

        def _anchor(p: str) -> str:
            seen = {p}
            while p in followers and followers[p] not in seen:
                p = followers[p]
                seen.add(p)
            return p

        order = sorted(
            order,
            key=lambda p: (
                tier_of[_anchor(p)],
                0 if modes[p] == "deep" else 1,
                pos[_anchor(p)], pos[p],
            ),
        )

    def _budget(path: str) -> int:
        if file_modes is None or not budget_by_mode:
            return diff_budget
        return budget_by_mode.get(modes[path], diff_budget)

    # entries: (FileChange to render, (part_no, n_parts) | None)
    entries: list[tuple[FileChange, tuple[int, int] | None]] = []
    for path in order:
        fc = by_path[path]
        pb = _budget(path)
        if estimate_tokens(_block(scope, index, fc)) <= pb:
            entries.append((fc, None))
            continue
        parts = _split_file(scope, index, fc, pb)
        if parts is None:
            plan.oversized.append((path, "oversized: hunk too large for one packet"))
            continue
        n = len(parts)
        entries.extend((p, (k, n)) for k, p in enumerate(parts, 1))

    def _close(cur: list[tuple[FileChange, tuple[int, int] | None]]) -> None:
        files = [e[0] for e in cur]
        paths = list(dict.fromkeys(f.path for f in files))
        split = {e[0].path: e[1] for e in cur if e[1] is not None}
        plan.batches.append(
            Batch(
                index=len(plan.batches) + 1,
                files=files,
                paths=paths,
                split=split,
                diff_tokens=estimate_tokens(render_diff(dataclasses.replace(scope, files=files), index)),
                mode=modes.get(paths[0], "") if file_modes is not None else "",
            )
        )

    cur: list[tuple[FileChange, tuple[int, int] | None]] = []
    cur_blocks: list[str] = []
    for fc, part in entries:
        blk = _block(scope, index, fc)
        mode_changed = (
            file_modes is not None and cur and modes[fc.path] != modes[cur[-1][0].path]
        )
        if cur and (mode_changed or _joined_tokens(cur_blocks + [blk]) > _budget(fc.path)):
            _close(cur)
            cur, cur_blocks = [], []
        cur.append((fc, part))
        cur_blocks.append(blk)
    if cur:
        _close(cur)
    return plan


def batch_note(batch: Batch, n_batches: int) -> str:
    lines = [
        f"Batch {batch.index} of {n_batches}. This change is too large for one packet, so it was "
        f"split into {n_batches} packets that are reviewed separately. Review only what this "
        "packet shows; do not report files from other batches as missing. Files in this packet:"
    ]
    for p in batch.paths:
        line = f"- {p}"
        if p in batch.split:
            k, n = batch.split[p]
            line += f" (part {k} of {n}: other hunks are in other batches)"
        lines.append(line)
    return "\n".join(lines)


def _read_lines(repo_root: Path, path: str) -> list[str] | None:
    try:
        return (repo_root / path).read_text(errors="replace").split("\n")
    except OSError:
        return None


def _callee_item(repo_root: Path, s: Symbol) -> str:
    lines = _read_lines(repo_root, s.file)
    head = f"{s.file}:{s.start_line}"
    if lines is None:
        return f"{head}\n{s.signature}"
    body = lines[s.start_line - 1 : s.end_line][:MAX_BODY_LINES]
    return f"{head}\n" + "\n".join(body)


def _test_item(repo_root: Path, path: str, names: list[str]) -> str:
    lines = _read_lines(repo_root, path) or []
    rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b") if names else None
    hits = [(i, ln) for i, ln in enumerate(lines, 1) if rx and rx.search(ln)][:MAX_TEST_LINES]
    first = hits[0][0] if hits else 1
    return f"{path}:{first}\n" + "\n".join(f"{i} {ln}" for i, ln in hits)


def build_packet(
    scope: Scope,
    index: RepoIndex,
    config: Config,
    tool_findings: list[Finding],
    guidelines: list[Guideline],
    learnings: list[Learning],
    intent: Intent,
    companions: list[Companion],
    callers: dict[str, list[CallSite]],
    callees: dict[str, list[Symbol]],
    tests: list[str],
    ratio: float = 1.0,
    bounds: tuple[float, float] = (0.5, 3.0),
    diff_cap: int = 12_000,
    history: list | None = None,
    batch_note: str | None = None,
    candidates: list[str] | None = None,
) -> Packet:
    mandatory = []
    if batch_note is not None:
        mandatory.append(("Batch", batch_note))
    mandatory += [
        ("Intent", _intent_body(intent)),
        ("Guidelines", _guidelines_body(guidelines)),
        ("Learnings", _learnings_body(learnings)),
    ]
    if history:
        mandatory.append(("Prior review comments in these files", format_history_section(history)))
    mandatory.append(("Tool signals (do not restate)", _signals_body(tool_findings, candidates)))
    sections: list[PacketSection] = []
    texts: list[str] = []
    for prio, (name, body) in enumerate(mandatory):
        t = _section(name, body)
        texts.append(t)
        sections.append(PacketSection(name, t, estimate_tokens(t), prio))
    mand_tokens = estimate_tokens("\n\n".join(texts))
    if mand_tokens > diff_cap * bounds[1]:
        raise PacketBudgetError(
            f"mandatory sections need ~{mand_tokens} tokens, over limit "
            f"{int(diff_cap * bounds[1])} (diff_cap {diff_cap} x {bounds[1]})"
        )

    diff_body, diff_tokens, diff_files, diff_omitted = _diff_section(scope, index, diff_cap)
    t = _section("Diff", diff_body)
    texts.append(t)
    sections.append(PacketSection("Diff", t, estimate_tokens(t), len(mandatory)))

    # context candidates, in priority order: (group, drop-name, text)
    root = Path(scope.repo_root)
    cands: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key in sorted(callees):
        for s in callees[key]:
            if (s.file, s.qualname) in seen:
                continue
            seen.add((s.file, s.qualname))
            cands.append((0, f"callee:{s.file}:{s.start_line}", _callee_item(root, s)))
    seen_sites: set[tuple[str, int]] = set()
    names: list[str] = []
    for key in sorted(callers):
        if key.split(".")[-1] not in names:
            names.append(key.split(".")[-1])
        for c in callers[key]:
            if (c.file, c.line) in seen_sites:
                continue
            seen_sites.add((c.file, c.line))
            text = f"{c.file}:{c.line} in {c.enclosing or '(module)'}"
            if c.snippet:
                text += "\n" + c.snippet
            cands.append((1, f"caller:{c.file}:{c.line}", text))
    for key in sorted(callees):
        n = key.split(".")[-1]
        if n not in names:
            names.append(n)
    for path in tests:
        cands.append((2, f"test:{path}", _test_item(root, path, names)))
    for c in companions:
        cands.append(
            (3, f"cochange:{c.companion}",
             f"{c.companion} — changed together in {c.support} of {c.total} commits")
        )

    lo, hi = bounds
    budget = int(diff_tokens * min(max(ratio, lo), hi))
    used = 0
    dropped: list[str] = []
    groups: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for grp, name, text in cands:
        cost = estimate_tokens(text)
        if used + cost <= budget:
            used += cost
            groups[grp].append(text)
        else:
            dropped.append(name)
    titles = {0: "Callee definitions", 1: "Callers", 2: "Tests", 3: "Co-changed files"}
    ctx_body = "\n\n".join(
        f"## {titles[g]}\n" + ("\n\n".join(groups[g]) if groups[g] else NONE) for g in range(4)
    )
    t = _section("Context", ctx_body)
    texts.append(t)
    sections.append(PacketSection("Context", t, estimate_tokens(t), len(mandatory) + 1))

    text = "\n\n".join(texts) + "\n"
    return Packet(
        text=text,
        sections=sections,
        diff_tokens=diff_tokens,
        context_tokens=used,
        dropped=dropped,
        sha=hashlib.sha1(text.encode()).hexdigest(),
        files=diff_files,
        omitted=diff_omitted,
    )
