"""Pipeline orchestration: scope -> tools -> context -> finders -> verify -> policy -> report."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from preflight_review import findings as findings_mod
from preflight_review import prompts
from preflight_review.config import Config, load_config, merge_overrides
from preflight_review.context.callers import (
    callees as find_callees,
    callers as find_callers,
    changed_symbols,
    tests_touching,
)
from preflight_review.context.cochange import mine_cochange
from preflight_review.context.guidelines import discover_guidelines
from preflight_review.context.intent import gather_intent
from preflight_review.context.learnings import (
    filter_by_path,
    read_learnings,
    retrieve_learnings,
)
from preflight_review.context.retrieval import query_from_scope, retrieve_history
from concurrent.futures import ThreadPoolExecutor

from preflight_review.context.packer import (
    DEFAULT_BATCH_DIFF_TOKENS,
    Batch,
    batch_note,
    build_packet,
    plan_batches,
)
from preflight_review.context.risk import risk_tier
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import ConfigError, GitError
from preflight_review.findings import Lane
from preflight_review.gitutil import current_branch, toplevel
from preflight_review.model import FakeBackend, ModelResult, claude_version
from preflight_review.naming import pr_label, report_basename, report_paths
from preflight_review.render import RunResult, write_outputs
from preflight_review.render.report import coverage
from preflight_review.scope import only_docs_or_lockfiles, resolve_scope
from preflight_review.stages.cluster import cluster, prefilter
from preflight_review.stages.finders import run_finders, validate_lenses
from preflight_review.stages.policy import PolicyResult, apply_policy
from preflight_review.stages.security import run_security_lane
from preflight_review.stages.triage import NEEDS_REVIEW, triage
from preflight_review.stages.walkthrough import walkthrough as run_walkthrough_stage
from preflight_review.stages.verify import VerdictCache, run_verifier
from preflight_review.state import BranchState, FindingState, load_state, save_state
from preflight_review.tools import run_tools
from preflight_review.tools.cochange_finding import companion_findings

INTERNAL_FILTERS = ["!.preflight/**"]
DEFAULT_BASE = "origin/main"
BATCH_DIFF_TOKENS = DEFAULT_BATCH_DIFF_TOKENS  # per-packet diff budget (tests monkeypatch)


@dataclass
class ReviewOptions:
    repo_root: Path
    base: str | None = None
    full: bool = False
    incremental: bool = False
    mode: str | None = None
    backend_name: str | None = None
    include_worktree: bool = True
    show_pre_existing: bool = False
    budget_usd: float | None = None
    walkthrough: bool | None = None
    json_stdout: bool = False


def select_backend(opts: ReviewOptions, config: Config):
    name = opts.backend_name or config.backend
    if name == "cli":
        from preflight_review.model.cli_backend import CliBackend

        return CliBackend()
    if name == "fake":
        path = os.environ.get("PREFLIGHT_FAKE_RESPONSES")
        if path:
            return FakeBackend.from_file(path)
        return FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    raise ConfigError(f"unknown backend: {name!r} (expected 'cli' or 'fake')")


def _intent_gh(gh_runner: Callable | None) -> Callable:
    runner = gh_runner or subprocess.run

    def call(cmd, cwd):
        proc = runner(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "gh failed").strip())
        return proc.stdout

    return call


def packet_for_finding(f, entries):
    """Pick the (Packet, Batch) entry whose hunks cover the finding's start line.

    Falls back to the first entry; None when the path was never delivered.
    """
    if not entries:
        return None
    line = f.start_line
    for packet, batch in entries:
        for fc in batch.files:
            if fc.path != f.file:
                continue
            for h in fc.hunks:
                if h.new_start <= line <= h.new_start + max(h.new_len, 1) - 1:
                    return packet
    return entries[0][0]


def _lane_name(role: str) -> str:
    name = role[len("finder:"):] if role.startswith("finder:") else role
    return name.split("@b", 1)[0]


def _write_raw(root: Path, basename: str, batch_index: int, results: dict, notes: list[str]) -> None:
    for role, res in results.items():
        try:
            d = root / ".preflight" / "runs" / basename / "raw"
            d.mkdir(parents=True, exist_ok=True)
            payload = {
                "role": role, "model": res.model, "ok": res.ok, "subtype": res.subtype,
                "error": res.error, "cost_usd": res.cost_usd,
                "structured": res.structured, "raw": res.raw,
            }
            (d / f"{_lane_name(role)}@b{batch_index}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str)
            )
        except OSError as e:
            msg = f"raw output not saved: {e}"
            if msg not in notes:
                notes.append(msg)
            return


def _rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def run_review(
    opts: ReviewOptions,
    backend=None,
    now: Callable[[], datetime] = datetime.now,
    gh_runner=None,
) -> tuple[RunResult, dict[str, Path]]:
    started = time.monotonic()
    root = toplevel(opts.repo_root)
    branch = current_branch(root)

    base_ref = opts.base or DEFAULT_BASE
    config = load_config(root, base_ref)
    if opts.base is None and config.base != base_ref:
        base_ref = config.base
        config = load_config(root, base_ref)
    config = merge_overrides(
        config, mode=opts.mode, budget_usd=opts.budget_usd, walkthrough=opts.walkthrough
    )
    config_source = f"base:{base_ref}"

    state = load_state(root)
    bs = state.branches.setdefault(branch or "HEAD", BranchState())
    filters = list(config.path_filters) + INTERNAL_FILTERS
    scope = resolve_scope(
        root,
        base_ref,
        opts.include_worktree,
        since_sha=(bs.last_reviewed_sha if opts.incremental and not opts.full else None),
        path_filters=filters,
    )
    resolved_base = scope.base_ref

    label = pr_label(root, branch, gh_runner or subprocess.run)

    # Reuse: same diff as the last review and its report is still on disk.
    if not opts.full and bs.diff_hash == scope.diff_hash and bs.last_report:
        paths = report_paths(root, config.report_dir, bs.last_report)
        if paths["md"].exists():
            reused_confirmed = reused_unverified = 0
            reusable = False
            try:
                data = json.loads(paths["json"].read_text())
                reusable = data.get("outcome") == "complete"
                reused_confirmed = len(data.get("findings", []))
                reused_unverified = len(data.get("unverified", []))
            except (OSError, ValueError, AttributeError):
                pass
            run = None if not reusable else RunResult(
                scope=scope, config=config, packet=None, policy=PolicyResult(),
                tool_statuses=[], walkthrough=None, lane_results={}, cost_usd=0.0,
                duration_s=time.monotonic() - started, round=bs.round, pr_label=label,
                basename=bs.last_report, outcome="complete", incomplete_reasons=[],
                models_used={}, previous_report=bs.last_report,
                show_pre_existing=opts.show_pre_existing, reused=True,
                reused_confirmed=reused_confirmed, reused_unverified=reused_unverified,
                backend_name=getattr(backend, "name", "") if backend else "",
                config_source=config_source, test_cmd=config.verify_cmd,
                generated_at=now(),
            )
            if run is not None:
                return run, paths

    round_no = bs.round + 1
    basename = report_basename(scope.repo_name, label, round_no)
    if backend is None:
        backend = select_backend(opts, config)

    notes: list[str] = []
    packet = None
    tool_statuses: list = []
    lane_results: dict[str, ModelResult] = {}
    suppressed: list = []
    pre_dropped: list = []
    incomplete_reasons: list[str] = []
    walkthrough = None
    policy = PolicyResult()
    verify_failures = 0
    models = config.models

    triaged_out: list[str] = []
    triage_notes: dict[str, str] = {}
    outside_packet: dict[str, list[str]] = {}
    model_stages = False
    packets: list = []
    loss: list[tuple[str, str]] = []
    reviewed_paths: list[str] | None = None
    not_reviewable: list[tuple[str, str]] = []
    finder_dropped: list[dict] = []
    plan_tiers: dict[str, str] = {}

    tri = triage(scope, config, backend)
    lane_results.update(tri.lane_results)
    triaged_out = [p for p, d in tri.per_file.items() if d == "APPROVED"]
    triage_notes = dict(tri.notes)

    if not scope.files or only_docs_or_lockfiles(scope):
        notes.append(
            "empty diff: model stages skipped" if not scope.files
            else "docs/lockfile-only diff: model stages skipped"
        )
        policy = apply_policy([], config, bs, round_no)
    else:
        review_scope = dataclasses.replace(
            scope, files=[fc for fc in scope.files if tri.per_file.get(fc.path) == NEEDS_REVIEW]
        )
        model_stages = bool(review_scope.files)
        index = RepoIndex(root, filters)

        def qualname(f) -> str | None:
            try:
                sym = index.enclosing_symbol(f.file, f.start_line)
            except Exception:  # non-python files, parse errors
                sym = None
            return sym.qualname if sym else None

        tool_findings, tool_statuses = run_tools(scope, config)
        for f in tool_findings:
            f.stable_id = findings_mod.stable_id(f, qualname(f))

        all_paths = [f.path for f in scope.files]
        companions = mine_cochange(root, all_paths)
        companion_found = companion_findings(companions, scope)
        finder_findings: list = []

        if not model_stages:
            notes.append(f"triage: {tri.reason or 'no files need model review'}; model stages skipped")
            packet = None
            reviewed_paths = []
            kept, suppressed, pre_dropped = prefilter(tool_findings + companion_found, scope, config, [])
            vout = run_verifier(
                cluster(kept), None, config, backend, scope, VerdictCache(root), ""
            )
            lane_results.update(vout.results)
            verify_failures = vout.failures
            policy = apply_policy(vout.findings, config, bs, round_no)
        else:
            changed = [f.path for f in review_scope.files]
            guidelines = discover_guidelines(root, changed, config, resolved_base)
            syms = changed_symbols(review_scope, index)
            query = query_from_scope(review_scope, [s.qualname for s in syms])
            all_learnings = read_learnings(root, resolved_base)
            learnings = retrieve_learnings(all_learnings, changed, config, backend, query=query)
            n_matching = len(filter_by_path(all_learnings, changed))
            if n_matching > config.learnings.cap:
                notes.append(
                    f"learnings: {len(learnings)} of {n_matching} (cap {config.learnings.cap}, "
                    f"rerank {'on' if config.learnings.rerank else 'off'})"
                )
            history = retrieve_history(root, changed, query)
            intent = gather_intent(
                scope, config, _intent_gh(gh_runner) if config.intent.issues else None
            )
            fan_in: dict[str, int] = {}
            for s in syms:
                try:
                    n = len(find_callers(s, index, review_scope))
                except Exception:
                    n = 0
                fan_in[s.file] = fan_in.get(s.file, 0) + n
            plan = plan_batches(review_scope, index, BATCH_DIFF_TOKENS, companions, fan_in=fan_in)
            plan_tiers = dict(plan.risk_tiers)
            n_batches = len(plan.batches)
            not_reviewable = list(plan.not_reviewable)

            runnable, skipped = validate_lenses(config.lenses)
            for lens in skipped:
                notes.append(f"lens '{lens}' is planned, not yet implemented: skipped")
            system_prompt = prompts.load_prompt("system")

            missing: dict[str, str] = {}  # path -> reason a part was not reviewed
            delivered_parts: dict[str, set[int]] = {}
            expected_parts: dict[str, int] = {}
            for path, reason in plan.oversized:
                missing.setdefault(path, reason)
            for b in plan.batches:
                for pth in b.paths:
                    expected_parts[pth] = max(expected_parts.get(pth, 0), b.split.get(pth, (1, 1))[1])
            outside_sets: dict[str, set[str]] = {}
            lanes_total = lanes_failed = 0
            batch_costs: list[float] = []
            packets_by_path: dict[str, list[tuple[object, Batch]]] = {}

            def _mark_missing(b, reason: str) -> None:
                for pth in b.paths:
                    missing.setdefault(pth, reason)

            for pos, batch in enumerate(plan.batches):
                if pos > 0:
                    spent = sum(r.cost_usd for r in lane_results.values())
                    est = max(batch_costs) if batch_costs else 0.0
                    if spent + est > config.budget_usd:
                        reason = (
                            f"run budget exhausted (USD {spent:.2f} spent, next batch est. "
                            f"USD {est:.2f}, budget USD {config.budget_usd:.2f})"
                        )
                        for rest in plan.batches[pos:]:
                            _mark_missing(rest, reason)
                        break
                bpaths = set(batch.paths)
                bscope = dataclasses.replace(review_scope, files=batch.files)
                bsyms = changed_symbols(bscope, index)
                callers_map = {s.qualname: find_callers(s, index, bscope) for s in bsyms}
                callees_map = {s.qualname: find_callees(s, index) for s in bsyms}
                tests = tests_touching(bsyms, root)
                packet = build_packet(
                    bscope, index, config,
                    [
                        f for f in tool_findings
                        if f.lane in (Lane.inline, Lane.nitpick) and f.file in bpaths
                    ],
                    guidelines, learnings, intent,
                    [c for c in companions if c.changed in bpaths],
                    callers_map, callees_map, tests,
                    history=history,
                    batch_note=batch_note(batch, n_batches) if n_batches > 1 else None,
                    diff_cap=BATCH_DIFF_TOKENS,
                )
                packets.append(packet)
                if pos == 0:
                    try:
                        index.save()
                    except OSError:
                        pass
                suffix = "" if n_batches == 1 else f"@b{batch.index}"
                tag = "" if n_batches == 1 else f" (batch {batch.index})"

                sec = None
                if config.security_lane:
                    with ThreadPoolExecutor(max_workers=1) as sec_pool:
                        sec_future = sec_pool.submit(
                            run_security_lane, packet, config, backend, bscope,
                            system_prompt, index, suffix,
                        )
                        outcome = run_finders(
                            packet, config.lenses, config, backend, bscope, system_prompt,
                            index, lane_suffix=suffix,
                        )
                        sec = sec_future.result()
                else:
                    outcome = run_finders(
                        packet, config.lenses, config, backend, bscope, system_prompt,
                        index, lane_suffix=suffix,
                    )
                lane_results.update(outcome.lane_results)
                incomplete_reasons += [f"lane failed: {lens}{tag}" for lens in outcome.incomplete]
                finder_findings += list(outcome.findings)
                for lens, paths_ in outcome.files_read_outside_packet.items():
                    outside_sets.setdefault(lens, set()).update(paths_)
                b_total = len(runnable)
                b_failed = len(outcome.incomplete)
                b_cost = sum(r.cost_usd for r in outcome.lane_results.values())
                if sec is not None:
                    finder_findings += list(sec.findings)
                    lane_results.update(sec.lane_results)
                    incomplete_reasons += [f"lane failed: {x}{tag}" for x in sec.incomplete]
                    for lens, paths_ in sec.files_read_outside_packet.items():
                        outside_sets.setdefault(lens, set()).update(paths_)
                    b_total += 1
                    b_failed += len(sec.incomplete)
                    b_cost += sum(r.cost_usd for r in sec.lane_results.values())
                for raw, reason in outcome.dropped:
                    finder_dropped.append({
                        "stage": "finder", "lane": "finder", "batch": batch.index,
                        "reason": reason, "candidate": raw,
                    })
                if sec is not None:
                    for raw, reason in sec.dropped:
                        finder_dropped.append({
                            "stage": "security", "lane": "security", "batch": batch.index,
                            "reason": reason, "candidate": raw,
                        })
                _write_raw(root, basename, batch.index, outcome.lane_results, notes)
                if sec is not None:
                    _write_raw(root, basename, batch.index, sec.lane_results, notes)
                batch_costs.append(b_cost)
                lanes_total += b_total
                lanes_failed += b_failed

                if b_total and b_failed >= b_total:
                    _mark_missing(batch, f"all lanes failed (batch {batch.index})")
                    continue
                omitted = set(packet.omitted)
                in_packet = set(packet.files)
                for pth in batch.paths:
                    if pth in in_packet and pth not in omitted:
                        delivered_parts.setdefault(pth, set()).add(batch.split.get(pth, (1, 1))[0])
                        packets_by_path.setdefault(pth, []).append((packet, batch))
                    else:
                        missing.setdefault(pth, "diff budget (packet)")

            outside_packet.update({k: sorted(v) for k, v in outside_sets.items()})
            reviewed_paths = []
            for pth in [fc.path for fc in review_scope.files]:
                if pth in missing:
                    reason = missing[pth]
                    if delivered_parts.get(pth):
                        reason = "partially reviewed: " + reason
                    loss.append((pth, reason))
                elif len(delivered_parts.get(pth, ())) >= expected_parts.get(pth, 1) and pth in delivered_parts:
                    reviewed_paths.append(pth)
            reviewed_paths = list(dict.fromkeys(reviewed_paths))
            if loss:
                cov = coverage(
                    scope, reviewed_paths, loss + not_reviewable, triaged_out, triage_notes
                )
                n_src = sum(
                    1 for p, _r in cov.not_reviewed
                    if (plan_tiers.get(p) or risk_tier(p)) == "source"
                )
                incomplete_reasons.append(
                    f"coverage: {len(cov.not_reviewed)} of {cov.total} changed files not reviewed "
                    f"(incl. {n_src} source; {len(loss)} lost to budget/limits)"
                )
            if lanes_total and lanes_failed >= lanes_total:
                incomplete_reasons.append("all lanes failed")

            kept, suppressed, pre_dropped = prefilter(
                finder_findings + tool_findings + companion_found, scope, config, learnings
            )
            clustered = cluster(kept)

            cache = VerdictCache(root)
            vout = run_verifier(
                clustered, packets[0] if packets else None, config, backend, review_scope,
                cache, system_prompt, packet_for=lambda f: packet_for_finding(f, packets_by_path.get(f.file)),
            )
            lane_results.update(vout.results)
            verify_failures = vout.failures
            policy = apply_policy(vout.findings, config, bs, round_no)

            if config.walkthrough:
                walkthrough, wres = run_walkthrough_stage(scope, policy, config, backend)
                if wres is not None:
                    lane_results["walkthrough"] = wres

    if opts.incremental and not opts.full:
        seen = {
            f.stable_id
            for group in (policy.report, policy.unverified, policy.pre_existing, policy.nit_capped)
            for f in group
            if f.stable_id
        }
        seen |= {f.stable_id for f, _ in policy.dropped if f.stable_id}
        seen |= {sid for sid, _ in policy.previously_reported}
        extra = [(sid, st.status) for sid, st in bs.findings.items() if sid not in seen]
        policy.previously_reported = sorted(set(policy.previously_reported) | set(extra))

    for sid, _st in policy.previously_reported:
        fs = bs.findings.get(sid)
        if sid not in policy.previous_info and fs is not None and fs.title and fs.file:
            policy.previous_info[sid] = (fs.title, fs.file, fs.start_line or 0)

    cost = sum(r.cost_usd for r in lane_results.values())
    if verify_failures:
        incomplete_reasons.append(f"verifier failures: {verify_failures}")
    if cost > config.budget_usd:
        incomplete_reasons.append(f"budget exceeded: USD {cost:.2f} > {config.budget_usd:.2f}")
    outcome_name = "incomplete" if incomplete_reasons else "complete"

    run = RunResult(
        scope=scope, config=config, packet=packets[0] if packets else None, policy=policy,
        tool_statuses=tool_statuses, walkthrough=walkthrough, lane_results=lane_results,
        cost_usd=cost, duration_s=time.monotonic() - started, round=round_no,
        pr_label=label, basename=basename, outcome=outcome_name,
        incomplete_reasons=incomplete_reasons,
        models_used={
            "finder": models.finder_deep if config.mode == "deep" else models.finder,
            "verifier": models.verifier,
            "verifier_major": models.verifier_major,
            **({"security": models.security} if config.security_lane else {}),
        },
        previous_report=bs.last_report, suppressed=suppressed, prefilter_dropped=pre_dropped,
        show_pre_existing=opts.show_pre_existing, reused=False, notes=notes,
        backend_name=getattr(backend, "name", ""), config_source=config_source,
        claude_code_version=claude_version() if getattr(backend, "name", "") == "cli" else None,
        test_cmd=config.verify_cmd, generated_at=now(),
        triaged_out=triaged_out, triage_notes=triage_notes,
        files_read_outside_packet=outside_packet,
        reviewed_paths=reviewed_paths, not_reviewed=loss + not_reviewable,
        batches=len(packets), packet_shas=[p.sha for p in packets],
        finder_dropped=finder_dropped,
        risk_tiers={**{fc.path: risk_tier(fc.path) for fc in scope.files}, **plan_tiers},
    )
    paths = write_outputs(run, report_paths(root, config.report_dir, basename))

    bs.round = round_no
    bs.diff_hash = scope.diff_hash
    bs.last_reviewed_sha = scope.head_sha
    bs.last_report = basename
    for f in policy.report + policy.unverified:
        if not f.stable_id:
            continue
        existing = bs.findings.get(f.stable_id)
        if existing is None:
            bs.findings[f.stable_id] = FindingState(
                status="open", first_round=round_no, last_seen_round=round_no,
                title=f.title, file=f.file, start_line=f.start_line,
            )
        else:
            existing.last_seen_round = round_no
            existing.title, existing.file, existing.start_line = f.title, f.file, f.start_line
    save_state(root, state)
    return run, paths


def summary_line(run: RunResult, paths: dict[str, Path]) -> str:
    md = _rel(paths["md"])
    if run.reused:
        return f"preflight: diff unchanged; reusing {md}"
    from preflight_review.findings import Severity, severity_rank

    confirmed = len(run.policy.report)
    major = sum(
        1 for f in run.policy.report
        if severity_rank(f.severity) >= severity_rank(Severity.major)
    )
    line = (
        f"preflight: {run.outcome}: {confirmed} confirmed ({major} major+), "
        f"{len(run.policy.unverified)} unverified → {md}"
    )
    if run.outcome != "complete":
        line += " — merge risk unknown (review incomplete; see Not reviewed)"
    return line
