"""Markdown report: every section always present, empty bodies read `none`."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from preflight_review.context.risk import TIER_RANK, risk_tier
from preflight_review.findings import Finding, Severity, Verdict, severity_rank, to_dict
from preflight_review.prompts import all_prompt_shas
from preflight_review.render.agent_prompt import aggregate_prompt, agent_prompt
from preflight_review.render.cost import cost_line
from preflight_review.stages.verify import UNVERIFIED_NIT, is_unverified_nit
from preflight_review.tools import degraded_label

if TYPE_CHECKING:
    from preflight_review.render import RunResult


def _enum(x) -> str:
    return x.value if hasattr(x, "value") else str(x)


def _pretty(x) -> str:
    return _enum(x).replace("_", " ").capitalize()


def _heading(n: int, f: Finding) -> str:
    loc = f"{f.file}:{f.start_line}" if f.start_line == f.end_line else f"{f.file}:{f.start_line}-{f.end_line}"
    return f"### F{n} · {_pretty(f.severity)} · {_pretty(f.category)} · {_pretty(f.effort).lower()} · {loc}"


def _block(n: int, f: Finding, run: RunResult) -> str:
    lines = [_heading(n, f), "", f"Title: {f.title}", f"Claim: {f.claim}", "", "Evidence:"]
    for e in f.evidence:
        lines.append(f"- `{e.file}:{e.line}` `{e.quote}`")
    verdict = _enum(f.verdict) if f.verdict is not None else "UNVERIFIED"
    if is_unverified_nit(f):
        lines.append(f"- Verifier: {UNVERIFIED_NIT} — trivial or nitpick finding, not sent to the verifier")
    elif f.verdict == Verdict.SKIPPED:
        lines.append("- Verifier: SKIPPED — deterministic tool finding (tool-verified, no model vote)")
    else:
        lines.append(
            f"- Verifier: {verdict} — {f.verifier_reason or 'no reason given'}; "
            f"checked: {'; '.join(f.verifier_checks) if f.verifier_checks else 'none'}"
        )
    for e in f.verifier_evidence:
        lines.append(f"- `{e.file}:{e.line}` `{e.quote}`")
    if f.trace:
        lines += ["", "Trace:"]
        for i, hop in enumerate(f.trace, 1):
            lines.append(f"  {i}. {hop.get('file')}:{hop.get('line')} — {hop.get('note', '')}")
    lines += ["", f"Why it matters: {f.why_it_matters}", f"Suggested fix: {f.suggested_fix}"]
    if f.caused_by:
        lines.append(f"caused_by: {f.caused_by}")
    if f.bug_class_instances:
        lines.append("Bug-class instances:")
        for inst in f.bug_class_instances:
            if isinstance(inst, dict):
                lines.append("- " + ", ".join(f"{k}: {v}" for k, v in inst.items()))
            else:
                lines.append(f"- {inst}")
    lines += [
        f"Stable id: {f.stable_id or 'none'}",
        "",
        "**Prompt for AI agent**",
        "",
        "```text",
        agent_prompt(f, run.test_cmd),
        "```",
    ]
    return "\n".join(lines)


def _blocks(items: list[Finding], start: int, run: RunResult) -> str:
    if not items:
        return "none"
    return "\n\n".join(_block(start + i, f, run) for i, f in enumerate(items))


def merge_risk(run: RunResult) -> tuple[str, str]:
    """(level, sentence). Incomplete runs never get a low/medium/high verdict."""
    if run.outcome != "complete" and not run.reused:
        cov = coverage_of(run)
        k = len(cov.not_reviewed)
        if k:
            src = sum(1 for p, _ in cov.not_reviewed if _tier(run, p) == "source")
            return "unknown", (
                f"Merge risk: unknown \u2014 review incomplete ({k} of {cov.total} files "
                f"not reviewed, incl. {src} source files)"
            )
        return "unknown", f"Merge risk: unknown \u2014 review incomplete ({'; '.join(run.incomplete_reasons)})"
    rep, unv = run.policy.report, run.policy.unverified
    serious = [f for f in rep if severity_rank(f.severity) >= severity_rank(Severity.major)]
    level = "high" if serious else ("medium" if rep or unv else "low")
    nits = n_unverified_nits(rep)
    return level, (
        f"Merge risk: {level} \u2014 {len(rep) - nits} confirmed ({len(serious)} critical/major), "
        + (f"{nits} unverified nits, " if nits else "")
        + f"{len(unv)} unverified."
    )


def n_unverified_nits(findings: list[Finding]) -> int:
    """Reported nits that skipped the verifier: not counted as confirmed."""
    return sum(1 for f in findings if is_unverified_nit(f))


def _merge_risk(run: RunResult) -> str:
    return merge_risk(run)[1]


def _walkthrough(w: dict | None, incomplete: bool = False) -> str:
    if not w:
        return "none"
    out: list[str] = []
    if w.get("summary"):
        prefix = "(Partial: describes only the reviewed part of the change.) " if incomplete else ""
        out += [prefix + str(w["summary"]), ""]
    themes = w.get("changes_by_theme") or []
    if themes:
        out += ["### Changes", ""]
        for t in themes:
            files = ", ".join(t.get("files") or [])
            out.append(f"- **{t.get('theme', '')}** ({files}): {t.get('description', '')}")
        out.append("")
    if w.get("effort_1_to_5") is not None:
        out += [f"Effort: {w['effort_1_to_5']}/5", ""]
    if w.get("merge_risk") and incomplete:
        out.append(
            "Model merge risk: suppressed \u2014 the review is incomplete, so the model's rating "
            f"({w['merge_risk']}) is not shown as a verdict"
        )
    elif w.get("merge_risk"):
        line = f"Model merge risk: {w['merge_risk']}"
        if w.get("merge_risk_sentence"):
            line += f" — {w['merge_risk_sentence']}"
        out.append(line)
    return "\n".join(out).rstrip() or "none"


def _tool_row(t) -> str:
    if t.degraded:
        status = f"DEGRADED: {t.note}" if t.note else "DEGRADED"
    elif not t.installed:
        status = f"not installed ({t.note})" if t.note else "not installed"
    elif not t.ran:
        status = f"skipped ({t.note})" if t.note else "skipped"
    else:
        status = "ran"
    return f"| {t.name} | {t.version or '-'} | {status} | {t.finding_count} |"


MAX_NOT_REVIEWED = 300


@dataclass
class Coverage:
    total: int
    reviewed: list[str] = field(default_factory=list)
    not_reviewed: list[tuple[str, str]] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def coverage(scope, reviewed_paths, not_reviewed, triaged_out, triage_notes) -> Coverage:
    """Single source of truth: len(reviewed) + len(not_reviewed) == total by construction."""
    universe = list(dict.fromkeys(fc.path for fc in scope.files))
    triaged = set(triaged_out)
    if reviewed_paths is None:
        lost = {path for path, _ in not_reviewed}
        done = {p for p in universe if p not in triaged and p not in lost}
    else:
        done = set(reviewed_paths)
    reasons: dict[str, str] = {}
    for path, reason in not_reviewed:
        reasons.setdefault(path, reason)
    reviewed: list[str] = []
    missing: list[tuple[str, str]] = []
    for p in universe:
        if p in done:
            reviewed.append(p)
        elif p in reasons:
            missing.append((p, reasons[p]))
        elif p in triaged:
            note = triage_notes.get(p)
            missing.append((p, "triaged as trivial" + (f": {note}" if note else "")))
        else:
            missing.append((p, "not reviewed (no reason recorded)"))
    return Coverage(len(universe), reviewed, missing, list(scope.filtered_out))


def coverage_of(run: RunResult) -> Coverage:
    return coverage(run.scope, run.reviewed_paths, run.not_reviewed, run.triaged_out, run.triage_notes)


def _tier(run: RunResult, path: str) -> str:
    return run.risk_tiers.get(path) or risk_tier(path)


def not_reviewed_entries(run: RunResult) -> list[tuple[str, str]]:
    """(path, reason) pairs, source files first (stable within a tier)."""
    entries = coverage_of(run).not_reviewed
    return sorted(entries, key=lambda e: TIER_RANK[_tier(run, e[0])])


def reviewed_count(run: RunResult) -> int:
    return len(coverage_of(run).reviewed)


def dropped_records(run: RunResult) -> list[dict]:
    recs: list[dict] = [dict(r) for r in run.finder_dropped]
    for f, reason in run.prefilter_dropped:
        recs.append({"stage": "prefilter", "reason": reason, "candidate": to_dict(f)})
    for f, reason in run.policy.dropped:
        stage = "verifier" if str(reason).lower().startswith("refuted") else "policy"
        recs.append({"stage": stage, "reason": reason, "candidate": to_dict(f)})
    for r in recs:
        cand = r.get("candidate") or {}
        r.setdefault("stable_id", cand.get("stable_id"))
        r.setdefault("title", cand.get("title"))
    return recs


MAX_EXCLUDED = 50
MAX_DROPPED = 50


def _dropped_section(run: RunResult, recs: list[dict]) -> str:
    """One line per dropped candidate, so a human can spot a wrong drop."""
    if not recs:
        return "none"
    lines = []
    for r in recs[:MAX_DROPPED]:
        cand = r.get("candidate") or {}
        where = f" ({cand['file']}:{cand.get('start_line')})" if cand.get("file") else ""
        lines.append(f"- {r.get('stage')}: {r.get('title') or '(untitled)'}{where} \u2014 {r.get('reason')}")
    if len(recs) > MAX_DROPPED:
        lines.append(f"- ... and {len(recs) - MAX_DROPPED} more (see {run.basename}.dropped.jsonl)")
    return "\n".join(lines)


def _not_reviewed_section(run: RunResult, entries: list[tuple[str, str]]) -> str:
    if not entries:
        return "none"
    counts = {"source": 0, "test": 0, "config": 0, "docs": 0}
    for p, _ in entries:
        counts[_tier(run, p)] += 1
    labels = {"source": "source", "test": "test", "config": "config", "docs": "docs/fixtures"}
    lines = [", ".join(f"{counts[t]} {labels[t]}" for t in labels if counts[t])]
    lines += [f"- {path} \u2014 {reason} [risk: {_tier(run, path)}]" for path, reason in entries[:MAX_NOT_REVIEWED]]
    if len(entries) > MAX_NOT_REVIEWED:
        lines.append(f"- ... and {len(entries) - MAX_NOT_REVIEWED} more (full list in the JSON report)")
    return "\n".join(lines)


def _excluded_line(excluded: list[str]) -> str:
    if not excluded:
        return "Excluded by path filters: none"
    shown = ", ".join(excluded[:MAX_EXCLUDED])
    more = f", ... and {len(excluded) - MAX_EXCLUDED} more" if len(excluded) > MAX_EXCLUDED else ""
    return f"Excluded by path filters ({len(excluded)}): {shown}{more}"


def _previous_line(sid: str, st: str, info: dict) -> str:
    if sid in info:
        title, file, line = info[sid]
        return f"- {sid}: {st} — {title} ({file}:{line})"
    return f"- {sid}: {st} — (details not recorded)"


HEADER_CRUCIAL_CAP = 3  # crucial paths named in the report's Mode line


def mode_line(run: RunResult) -> str:
    """`Mode: deep (auto — crucial paths: a, b)`; an explicit mode reads `(set explicitly)`."""
    mode = run.config.mode
    if run.mode_requested == "menu":
        return f"Mode: {mode} (chosen at the pre-run depth menu)"
    if run.mode_requested != "auto":
        return f"Mode: {mode}" + (" (set explicitly)" if run.mode_requested else "")
    if not run.crucial_paths:
        return f"Mode: {mode} (auto — no crucial paths changed)"
    shown = ", ".join(run.crucial_paths[:HEADER_CRUCIAL_CAP])
    more = len(run.crucial_paths) - HEADER_CRUCIAL_CAP
    if more > 0:
        shown += f", +{more} more"
    return f"Mode: {mode} (auto — crucial paths: {shown})"


def _not_started(run: RunResult, out: list[str]) -> str:
    est = run.estimate
    out += [
        mode_line(run),
        "Result: not started — " + "; ".join(run.incomplete_reasons),
        "",
        "## Not started",
        "",
        (
            "No model was called and nothing was reviewed. A review is never cut short once "
            "it starts, so a run estimated above `confirm_above_usd` asks first, and without "
            "a terminal to ask on it does not start."
        ),
        "",
    ]
    if est is not None:
        out += [f"Estimate: {est.describe()}.", ""]
    if run.mode_estimates:
        out += ["Estimate per depth:", ""]
        out += [f"- {m}: {e.describe()}" for m, e in run.mode_estimates.items()]
        out.append("")
    out += ["To proceed, run one of:", "", "```sh", *run.rerun_commands, "```", ""]
    out += [
        (
            "Each command runs the review at that depth without asking. On a terminal the "
            "same choice is offered as a menu. `confirm_above_usd: null` in "
            "`.preflight/config.yaml` turns the menu off once it is on the base branch."
        ),
    ]
    if run.notes:
        out += ["", "## Notes", "", *[f"- {n}" for n in run.notes]]
    return "\n".join(out) + "\n"


def paused_result_line(run: RunResult) -> str:
    resets = f" (resets {run.limit_resets})" if run.limit_resets else ""
    return (
        f"Result: PAUSED \u2014 Claude usage limit reached{resets}. "
        f"{run.calls_done} of {run.calls_total} calls done. "
        f"Resume with: {run.resume_command or 'preflight-review review --resume'}"
    )


def render_report(run: RunResult) -> str:
    s, p = run.scope, run.policy
    out: list[str] = []
    out.append(
        f"# Preflight review — {s.repo_name} {run.pr_label} round {run.round:02d} — "
        f"{s.branch or 'detached'} @ {s.head_sha[:7]}"
    )
    out.append("")
    if run.degraded:
        out += [
            "> **DEGRADED:** " + "; ".join(run.degraded)
            + ". Findings these tools would report are missing from this review.",
            "",
        ]
    start = (s.diff_from or s.base_sha)[:7]
    added = sum(fc.added_lines for fc in s.files)
    deleted = sum(fc.deleted_lines for fc in s.files)
    cov = coverage_of(run)
    n = cov.total
    entries = not_reviewed_entries(run)
    k = len(entries)
    nr = f"{k} (see Not reviewed)" if k else "none"
    out.append(
        f"Scope: {start}..head, worktree included {'yes' if s.include_worktree else 'no'}, "
        f"{n} files, +{added}/−{deleted} lines, reviewed {len(cov.reviewed)}/{n}, "
        f"not reviewed: {nr}"
    )
    if run.outcome == "not_started" and not run.reused:
        return _not_started(run, out)
    out.append(mode_line(run))
    if run.reused:
        out.append(f"Result: reused (diff unchanged since {run.previous_report})")
    else:
        result = f"Result: {run.outcome}"
        if run.outcome == "paused":
            result = paused_result_line(run)
        elif run.degraded:
            result += " (DEGRADED: " + ", ".join(degraded_label(d) for d in run.degraded) + ")"
        if run.incomplete_reasons and run.outcome != "paused":
            result += " — " + "; ".join(run.incomplete_reasons)
        out.append(result)
    out += ["", f"## Not reviewed ({k})", "", _not_reviewed_section(run, entries), "", _excluded_line(cov.excluded)]
    incomplete = merge_risk(run)[0] == "unknown"
    out += ["", "## Walkthrough", "", _walkthrough(run.walkthrough, incomplete), "", _merge_risk(run), ""]

    sup_ids = " ".join(lid for _, lid in run.suppressed)
    nits = n_unverified_nits(p.report)
    out.append(
        f"## Findings ({len(p.report) - nits} confirmed, "
        + (f"{nits} unverified nits, " if nits else "")
        + f"{len(p.unverified)} unverified, "
        f"{len(p.pre_existing)} pre-existing hidden, {len(run.suppressed)} suppressed by learnings"
        f"{' ' + sup_ids if sup_ids else ''}, {len(p.previously_reported)} previously reported)"
    )
    out += ["", f"Dropped: {len(dropped_records(run))} (see {run.basename}.dropped.jsonl)", ""]
    out += [_blocks(p.report, 1, run), ""]

    out += [f"## Unverified ({len(p.unverified)})", ""]
    if p.unverified:
        # Collapsed: recall-first finders produce more UNKNOWN findings than confirmed ones.
        out += [
            "<details>",
            (
                f"<summary>{len(p.unverified)} finding(s) the verifier could not confirm or "
                "refute (expand)</summary>"
            ),
            "",
            _blocks(p.unverified, len(p.report) + 1, run),
            "",
            "</details>",
            "",
        ]
    else:
        out += ["none", ""]

    recs = dropped_records(run)
    out += [f"## Dropped ({len(recs)})", "", _dropped_section(run, recs), ""]

    out += ["## Previously reported (still tracked)", ""]
    out.append(
        "\n".join(_previous_line(sid, st, p.previous_info) for sid, st in p.previously_reported) or "none"
    )
    out.append("")

    out += [f"## Pre-existing (hidden, {len(p.pre_existing)})", ""]
    if run.show_pre_existing and p.pre_existing:
        base = len(p.report) + len(p.unverified) + 1
        out.append(_blocks(p.pre_existing, base, run))
    else:
        out.append(f"{len(p.pre_existing)} pre-existing findings hidden")
    out.append("")

    out += ["## Suppressed by learnings", ""]
    out.append("\n".join(f"- {f.stable_id} ({f.title}) -> {lid}" for f, lid in run.suppressed) or "none")
    out.append("")

    out += ["## Tool output", ""]
    if run.tool_statuses:
        out += ["| tool | version | status | findings |", "|---|---|---|---|"]
        out += [_tool_row(t) for t in run.tool_statuses]
    else:
        out.append("none")
    out.append("")

    shas = all_prompt_shas()
    prompts_sha = hashlib.sha1(repr(sorted(shas.items())).encode()).hexdigest()[:12]
    out += ["## Run", ""]
    out.append("- Models: " + (", ".join(f"{k}={v}" for k, v in run.models_used.items()) or "none"))
    out.append(f"- Model calls: {len(run.lane_results)}")
    out.append("- " + cost_line(run.cost_usd, run.lane_results))
    out.append(f"- Duration: {run.duration_s:.1f}s")
    out.append(f"- Claude Code: {run.claude_code_version or 'unknown'}")
    out.append(f"- Prompts sha: {prompts_sha}")
    first_sha = run.packet_shas[0] if run.packet_shas else (run.packet.sha if run.packet else "none")
    sha_line = f"- Packet sha: {first_sha}"
    if len(run.packet_shas) > 1:
        sha_line += f" ({len(run.packet_shas)} packets)"
    out.append(sha_line)
    if run.batches > 0:
        out.append(f"- Batches: {run.batches}")
    for note in run.notes:
        out.append(f"- Note: {note}")
    if p.round_message:
        out.append(f"- {p.round_message}")
    out.append("")

    out += [
        "## Respond",
        "",
        "Label each finding so learnings and eval improve: "
        '`preflight-review respond F1 fixed|wrong|intentional|didnt_matter ["reason"]`',
        "",
        "## Aggregate prompt",
        "",
        "```text",
        aggregate_prompt(p.report + p.unverified, run.test_cmd),
        "```",
        "",
    ]
    return "\n".join(out)
