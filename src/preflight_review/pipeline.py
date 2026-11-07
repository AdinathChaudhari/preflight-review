"""Pipeline orchestration: scope -> tools -> context -> finders -> verify -> policy -> report."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from preflight_review import findings as findings_mod
from preflight_review import prompts
from preflight_review.config import Config, legacy_defaults_note, load_config, merge_overrides
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
from preflight_review.depth import (
    MODES,
    Estimate,
    depth_menu,
    estimate_run,
    parse_choice,
    resolve_mode,
)
from preflight_review.errors import ConfigError, GitError
from preflight_review.findings import Lane
from preflight_review.gitutil import current_branch, toplevel
from preflight_review.model import FakeBackend, ModelResult, claude_version
from preflight_review.model.limits import BreakerBackend, is_paused, load_calls
from preflight_review.naming import pr_label, report_basename, report_paths
from preflight_review.render import RunResult, write_outputs
from preflight_review.render.report import coverage, n_unverified_nits
from preflight_review.scope import (
    _is_docs_or_lockfile,
    docs_only_skip,
    only_docs_or_lockfiles,
    resolve_scope,
)
from preflight_review.stages.cluster import cluster, prefilter
from preflight_review.extensions import detect as detect_extensions
from preflight_review.stages.finders import (
    DOCS_REVIEW_LENSES,
    LENSES,
    effective_lenses,
    lens_applies,
    run_finders,
    validate_lenses,
)
from preflight_review.stages.policy import PolicyResult, apply_policy
from preflight_review.stages.security import run_security_lane
from preflight_review.stages.triage import NEEDS_REVIEW, triage
from preflight_review.stages.walkthrough import walkthrough as run_walkthrough_stage
from preflight_review.stages.sweep import run_sweep, sweep_enabled
from preflight_review.stages.verify import VerdictCache, run_verifier, verifier_votes
from preflight_review.state import BranchState, FindingState, load_state, save_state
from preflight_review.tools import degraded_partial, degraded_tools, run_tools
from preflight_review.tools.cochange_finding import companion_findings
from preflight_review.tools.key_consumers import key_consumer_signals

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
    yes: bool = False  # skip the confirm_above_usd depth menu
    resume: bool = False  # reuse the saved calls of a run paused by the usage limit


def choose_tty(menu: str) -> str | None:
    """Show the depth menu on the terminal and return the raw answer; None when stdin is not
    a TTY (hook, skill, CI)."""
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    print(menu, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


def rerun_command(opts: ReviewOptions, *extra: str) -> str:
    """The `review` command line for these options plus `extra` flags."""
    parts = ["preflight-review", "review"]
    if opts.base:
        parts += ["--base", opts.base]
    if opts.full:
        parts.append("--full")
    if opts.incremental:
        parts.append("--incremental")
    if opts.mode and "--mode" not in extra:
        parts += ["--mode", opts.mode]
    if opts.backend_name:
        parts += ["--backend", opts.backend_name]
    if not opts.include_worktree:
        parts.append("--committed-only")
    if opts.show_pre_existing:
        parts.append("--show-pre-existing")
    if opts.walkthrough is False:
        parts.append("--no-walkthrough")
    if opts.budget_usd is not None:
        parts += ["--budget-usd", f"{opts.budget_usd:g}"]
    return " ".join(parts + list(extra))


def select_backend(opts: ReviewOptions, config: Config):
    name = opts.backend_name or config.backend
    if name == "cli":
        from preflight_review.model.cli_backend import CliBackend

        return CliBackend(timeout_s=config.call_timeout_s)
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
            out = d / f"{_lane_name(role)}@b{batch_index}.json"
            if isinstance(res.raw, dict) and res.raw.get("resumed") and out.exists():
                continue  # a replayed call: keep the paused run's original raw output
            payload = {
                "role": role, "model": res.model, "ok": res.ok, "subtype": res.subtype,
                "error": res.error, "cost_usd": res.cost_usd,
                "structured": res.structured, "raw": res.raw,
            }
            out.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str)
            )
        except OSError as e:
            msg = f"raw output not saved: {e}"
            if msg not in notes:
                notes.append(msg)
            return


def _lane_reason(lens: str, tag: str, results: dict[str, ModelResult]) -> str:
    """'paused: usage limit: <lens>' when the breaker skipped the lane, else 'lane failed'."""
    paused = any(
        is_paused(r) for role, r in results.items() if _lane_name(role).split("#", 1)[0] == lens
    )
    return f"{'paused: usage limit' if paused else 'lane failed'}: {lens}{tag}"


def _run_dir(root: Path, basename: str) -> Path:
    return root / ".preflight" / "runs" / basename


def _gated_matches(lenses: list[str], paths: list[str]) -> list[str]:
    """Path-gated lenses (in `lenses`) whose gate some path matches."""
    return [
        x for x in lenses if x in LENSES and LENSES[x].paths and lens_applies(LENSES[x], paths)
    ]


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
    choose: Callable[[str], str | None] = choose_tty,
) -> tuple[RunResult, dict[str, Path]]:
    started = time.monotonic()
    root = toplevel(opts.repo_root)
    branch = current_branch(root)

    base_ref = opts.base or DEFAULT_BASE
    config = load_config(root, base_ref)
    if opts.base is None and config.base != base_ref:
        base_ref = config.base
        config = load_config(root, base_ref)
    # Checked on the file config: CLI overrides and the resolved auto mode are not keys
    # the user can remove from .preflight/config.yaml.
    legacy = legacy_defaults_note(config)
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

    # Depth is fixed once, before any model call: every later `config.mode` check sees the
    # resolved mode. A started review is never cut short for cost (see the estimate gate).
    mode_requested = config.mode
    mode_resolved, crucial = resolve_mode(config, [f.path for f in scope.files])
    config = dataclasses.replace(config, mode=mode_resolved)

    label = pr_label(root, branch, gh_runner or subprocess.run)

    # Reuse: same diff as the last review and its report is still on disk.
    if not opts.full and not opts.resume and bs.diff_hash == scope.diff_hash and bs.last_report:
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
                generated_at=now(), mode_requested=mode_requested, crucial_paths=crucial,
            )
            if run is not None:
                return run, paths

    round_no = bs.round + 1
    basename = report_basename(scope.repo_name, label, round_no)
    if backend is None:
        backend = select_backend(opts, config)

    notes: list[str] = []
    # A paused run leaves the round unadvanced, so a resume reuses its round and basename.
    cached_calls: dict[str, list[dict]] = {}
    resumed_from: str | None = None
    if opts.resume:
        if not bs.paused_report:
            notes.append("--resume: no paused run on this branch; ran a normal review")
        elif bs.paused_diff_hash != scope.diff_hash:
            notes.append(
                f"--resume: the diff changed since paused run {bs.paused_report}; "
                "ran a normal review"
            )
        else:
            resumed_from = bs.paused_report
            basename = bs.paused_report
            cached_calls = load_calls(_run_dir(root, basename))
    backend = BreakerBackend(backend, cached_calls)
    if legacy:
        notes.append(legacy)
    packet = None
    tool_statuses: list = []
    lane_results: dict[str, ModelResult] = {}
    suppressed: list = []
    pre_dropped: list = []
    incomplete_reasons: list[str] = []
    walkthrough = None
    policy = PolicyResult()
    verify_failures = 0
    adjudicated_by: dict[str, int] = {}
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
    ran_lenses: set[str] = set()  # lenses that ran on at least one batch
    ran_security = False

    all_paths = [f.path for f in scope.files]
    autos = detect_extensions(root) if config.auto_lenses else []
    lens_list, lens_notes = effective_lenses(config, all_paths, autos)
    gated_hit = _gated_matches(lens_list, all_paths)

    estimate: Estimate | None = None
    if scope.files and not (docs_only_skip(scope, config.docs_review) and not gated_hit):
        runnable_all, _ = validate_lenses(lens_list)
        n_lanes = sum(1 for x in runnable_all if lens_applies(LENSES[x], all_paths))
        n_lanes += 1 if config.security_lane else 0

        def estimate_for(mode: str) -> Estimate:
            return estimate_run(scope, config, mode, n_lanes, BATCH_DIFF_TOKENS)

        estimate = estimate_for(config.mode)
        print(f"preflight: estimated {estimate.describe()}", file=sys.stderr)
        notes.append(f"pre-run estimate: {estimate.describe()}")
        threshold = config.confirm_above_usd
        # A resumed run was already confirmed when it first started.
        if threshold is not None and estimate.usd > threshold and not (opts.yes or resumed_from):
            estimates = {m: estimate_for(m) for m in MODES}
            default = config.mode if config.mode in MODES else "standard"
            answer = choose(depth_menu(config, estimates, default, mode_requested == "auto"))
            picked = None if answer is None else parse_choice(answer, default)
            if picked in MODES:
                if picked != config.mode:
                    notes.append(
                        f"depth chosen at the pre-run menu: {picked} "
                        f"(was {config.mode}{', from auto' if mode_requested == 'auto' else ''})"
                    )
                    config = dataclasses.replace(config, mode=picked)
                    mode_requested = "menu"
                    estimate = estimates[picked]
                    notes.append(f"pre-run estimate ({picked}): {estimate.describe()}")
            else:
                commands = [rerun_command(opts, "--mode", m, "--yes") for m in MODES]
                why = "cancelled at the menu" if answer is not None else "no terminal to choose"
                reason = (
                    f"{why}; estimated {estimate.describe()}, above "
                    f"confirm_above_usd ${threshold:g}"
                )
                for m in MODES:
                    if m != config.mode:
                        notes.append(f"--mode {m} estimate: {estimates[m].describe()}")
                run = RunResult(
                    scope=scope, config=config, packet=None, policy=PolicyResult(),
                    tool_statuses=[], walkthrough=None, lane_results={}, cost_usd=0.0,
                    duration_s=time.monotonic() - started, round=round_no, pr_label=label,
                    basename=basename, outcome="not_started", incomplete_reasons=[reason],
                    models_used={}, previous_report=bs.last_report,
                    show_pre_existing=opts.show_pre_existing, notes=notes,
                    backend_name=getattr(backend, "name", ""), config_source=config_source,
                    test_cmd=config.verify_cmd, generated_at=now(), reviewed_paths=[],
                    mode_requested=mode_requested, crucial_paths=crucial,
                    estimate=estimate, rerun_commands=commands, mode_estimates=estimates,
                )
                # State is untouched: the next run reuses this round number.
                return run, write_outputs(run, report_paths(root, config.report_dir, basename))

    # review_docs: a path-gated lens covers some changed doc, not just any changed file
    # (a workflow change must not send every unrelated doc past Haiku triage).
    gated_docs = _gated_matches(lens_list, [p for p in all_paths if _is_docs_or_lockfile(p)])
    tri = triage(scope, config, backend, review_docs=bool(gated_docs))
    lane_results.update(tri.lane_results)
    triaged_out = [p for p, d in tri.per_file.items() if d == "APPROVED"]
    triage_notes = dict(tri.notes)

    # A docs-only diff that touches a path-gated lens's files (a deploy runbook) is reviewed
    # by that lens even under docs_review: off.
    if not scope.files or (docs_only_skip(scope, config.docs_review) and not gated_hit):
        notes.append(
            "empty diff: model stages skipped" if not scope.files
            else "docs/lockfile-only diff: model stages skipped"
        )
        policy = apply_policy([], config, bs, round_no)
    else:
        review_scope = dataclasses.replace(
            scope, files=[fc for fc in scope.files if tri.per_file.get(fc.path) == NEEDS_REVIEW]
        )
        # Triage can leave only docs in review_scope (code files approved): the same
        # docs_review rule then applies to what is left.
        docs_left_skip = docs_only_skip(review_scope, config.docs_review) and not _gated_matches(
            lens_list, [f.path for f in review_scope.files]
        )
        model_stages = bool(review_scope.files) and not docs_left_skip
        index = RepoIndex(root, filters)

        def qualname(f) -> str | None:
            try:
                sym = index.enclosing_symbol(f.file, f.start_line)
            except Exception:  # non-python files, parse errors
                sym = None
            return sym.qualname if sym else None

        rule_dirs = [d for hit in autos for d in hit.ast_rule_dirs]
        tool_findings, tool_statuses = (
            run_tools(scope, config, rule_dirs) if rule_dirs else run_tools(scope, config)
        )
        for f in tool_findings:
            f.stable_id = findings_mod.stable_id(f, qualname(f))

        companions = mine_cochange(root, all_paths)
        companion_found = companion_findings(companions, scope)
        key_signals = key_consumer_signals(scope, config)
        finder_findings: list = []

        if not model_stages:
            if review_scope.files:
                notes.append("triage left only docs/lockfiles: model stages skipped")
            else:
                notes.append(
                    f"triage: {tri.reason or 'no files need model review'}; model stages skipped"
                )
            packet = None
            reviewed_paths = []
            kept, suppressed, pre_dropped = prefilter(tool_findings + companion_found, scope, config, [])
            vout = run_verifier(
                cluster(kept), None, config, backend, scope, VerdictCache(root), ""
            )
            lane_results.update(vout.results)
            verify_failures = vout.failures
            adjudicated_by.update(vout.adjudicated_by)
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

            review_paths = [f.path for f in review_scope.files]
            run_lenses, lens_note, run_security = list(lens_list), "", config.security_lane
            if only_docs_or_lockfiles(review_scope):
                # Docs-only review scope: the security lane has nothing to trace. Any
                # path-gated lens whose files (a deploy runbook) changed runs; under
                # docs_review auto|on the correctness/contracts lenses add a doc-consistency
                # review, and under off (or with no prose) only the gated lenses run.
                gated = _gated_matches(lens_list, review_paths)
                run_security = False
                if docs_only_skip(review_scope, config.docs_review):
                    run_lenses = gated
                    notes.append(f"docs-only diff: path-gated lens only ({', '.join(gated)})")
                else:
                    run_lenses = [
                        x for x in lens_list if x in DOCS_REVIEW_LENSES
                    ] or [DOCS_REVIEW_LENSES[0]]
                    run_lenses += gated
                    lens_note = prompts.render("docs_review")
                    notes.append(
                        f"docs-only diff: doc-consistency review ({', '.join(run_lenses)}; "
                        "docs_review: off would skip it)"
                    )
            runnable, skipped = validate_lenses(run_lenses)
            for lens in skipped:
                notes.append(f"lens '{lens}' is planned, not yet implemented: skipped")
            notes += lens_notes
            for lens in runnable:
                if not lens_applies(LENSES[lens], review_paths):
                    notes.append(f"lens '{lens}' skipped: no changed file matches its paths")
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
            sweep_targets: list[tuple[object, Batch, object]] = []  # (packet, batch, bscope)

            def _mark_missing(b, reason: str) -> None:
                for pth in b.paths:
                    missing.setdefault(pth, reason)

            for pos, batch in enumerate(plan.batches):
                if pos > 0 and config.budget_usd is not None:
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
                    candidates=[k.render() for k in key_signals if k.file in bpaths],
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
                if run_security:
                    with ThreadPoolExecutor(max_workers=1) as sec_pool:
                        sec_future = sec_pool.submit(
                            run_security_lane, packet, config, backend, bscope,
                            system_prompt, index, suffix,
                        )
                        outcome = run_finders(
                            packet, run_lenses, config, backend, bscope, system_prompt,
                            index, lane_suffix=suffix, lens_note=lens_note,
                        )
                        sec = sec_future.result()
                else:
                    outcome = run_finders(
                        packet, run_lenses, config, backend, bscope, system_prompt,
                        index, lane_suffix=suffix, lens_note=lens_note,
                    )
                lane_results.update(outcome.lane_results)
                incomplete_reasons += [
                    _lane_reason(lens, tag, outcome.lane_results) for lens in outcome.incomplete
                ]
                finder_findings += list(outcome.findings)
                notes += [f"{n}{tag}" for n in outcome.shallow]
                for lens, paths_ in outcome.files_read_outside_packet.items():
                    outside_sets.setdefault(lens, set()).update(paths_)
                b_total = len(runnable) - len(outcome.gated)
                ran_lenses.update(x for x in runnable if x not in outcome.gated)
                b_failed = len(outcome.incomplete)
                b_cost = sum(r.cost_usd for r in outcome.lane_results.values())
                if sec is not None:
                    finder_findings += list(sec.findings)
                    lane_results.update(sec.lane_results)
                    incomplete_reasons += [
                        _lane_reason(x, tag, sec.lane_results) for x in sec.incomplete
                    ]
                    for lens, paths_ in sec.files_read_outside_packet.items():
                        outside_sets.setdefault(lens, set()).update(paths_)
                    b_total += 1
                    ran_security = True
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

                if b_total == 0:
                    # Every lens was path-gated out of this batch (docs_review: off): its
                    # files were not reviewed, by design, so no sweep runs on them either.
                    not_reviewable += [(pth, "no lens applies (path gate)") for pth in batch.paths
                                       if pth not in dict(not_reviewable)]
                    continue
                if b_total and b_failed >= b_total:
                    paused = any(
                        is_paused(r) for r in [
                            *outcome.lane_results.values(),
                            *(sec.lane_results.values() if sec is not None else []),
                        ]
                    )
                    _mark_missing(
                        batch,
                        f"{'paused: usage limit' if paused else 'all lanes failed'} "
                        f"(batch {batch.index})",
                    )
                    continue
                sweep_targets.append((packet, batch, bscope))
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

            def packet_for(f):
                return packet_for_finding(f, packets_by_path.get(f.file))

            vout = run_verifier(
                clustered, packets[0] if packets else None, config, backend, review_scope,
                cache, system_prompt, packet_for=packet_for,
            )
            lane_results.update(vout.results)
            verify_failures = vout.failures
            adjudicated_by.update(vout.adjudicated_by)
            verified = list(vout.findings)

            if sweep_enabled(config) and sweep_targets:
                # One sweep per run. It gets every candidate found so far, before clustering
                # (merged-away members count as known too), and the changed files; a
                # single-batch run also gets that batch's packet.
                known = finder_findings + tool_findings + companion_found
                sweep_packet = sweep_targets[0][0] if len(sweep_targets) == 1 else None
                sweep_files = [fc.path for fc in review_scope.files]
                sw = run_sweep(
                    sweep_packet, known, config, backend, review_scope, system_prompt, index,
                    changed_files=sweep_files,
                )
                lane_results.update(sw.lane_results)
                incomplete_reasons += [_lane_reason(x, "", sw.lane_results) for x in sw.incomplete]
                for lens, paths_ in sw.files_read_outside_packet.items():
                    outside_packet[lens] = sorted(set(outside_packet.get(lens, [])) | set(paths_))
                # Raw output: under the batch it saw, or b0 when it covered the whole run.
                sw_index = sweep_targets[0][1].index if sweep_packet is not None else 0
                for raw, reason in sw.dropped:
                    finder_dropped.append({
                        "stage": "sweep", "lane": "sweep", "batch": sw_index,
                        "reason": reason, "candidate": raw,
                    })
                _write_raw(root, basename, sw_index, sw.lane_results, notes)
                swept = list(sw.findings)
                known_ids = {f.stable_id for f in known if f.stable_id}
                new: dict[str, object] = {}
                for f in swept:
                    if f.stable_id and f.stable_id not in known_ids:
                        new.setdefault(f.stable_id, f)
                notes.append(
                    f"sweep: {len(new)} new candidate(s) from 1 call over "
                    f"{len(sweep_files)} changed file(s)"
                )
                s_kept, s_suppressed, s_dropped = prefilter(
                    list(new.values()), scope, config, learnings
                )
                suppressed += s_suppressed
                pre_dropped += s_dropped
                if s_kept:
                    svout = run_verifier(
                        cluster(s_kept), packets[0] if packets else None, config, backend,
                        review_scope, cache, system_prompt, packet_for=packet_for,
                    )
                    lane_results.update(svout.results)
                    verify_failures += svout.failures
                    for m, n in svout.adjudicated_by.items():
                        adjudicated_by[m] = adjudicated_by.get(m, 0) + n
                    verified += svout.findings

            policy = apply_policy(verified, config, bs, round_no)

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
    if config.budget_usd is not None and cost > config.budget_usd:
        incomplete_reasons.append(f"budget exceeded: USD {cost:.2f} > {config.budget_usd:.2f}")
    outcome_name = "incomplete" if incomplete_reasons else "complete"
    keep_paused = False  # an earlier paused run's resume point outranks this run's
    if backend.tripped:
        outcome_name = "paused"
        if bs.paused_report and not opts.resume:
            # A second paused run (e.g. the pre-push hook) must not wipe the resume cache.
            prev = load_calls(_run_dir(root, bs.paused_report))
            if bs.paused_diff_hash == scope.diff_hash and bs.paused_report == basename:
                backend.merge_saved(prev)
            elif sum(map(len, prev.values())) > len(backend.successes):
                keep_paused = True
                notes.append(
                    f"kept the resume point of paused run {bs.paused_report} "
                    f"({sum(map(len, prev.values()))} cached call(s)); this run's "
                    f"{len(backend.successes)} call(s) not saved"
                )
        if not keep_paused:
            try:
                backend.save(_run_dir(root, basename))
            except OSError as e:
                notes.append(f"resume cache not saved: {e}")
    if resumed_from:
        notes.append(
            f"resumed from round {round_no:02d}; reused {backend.reused} cached call(s)"
        )
    degraded = degraded_tools(tool_statuses)

    run = RunResult(
        scope=scope, config=config, packet=packets[0] if packets else None, policy=policy,
        tool_statuses=tool_statuses, walkthrough=walkthrough, lane_results=lane_results,
        cost_usd=cost, duration_s=time.monotonic() - started, round=round_no,
        pr_label=label, basename=basename, outcome=outcome_name,
        incomplete_reasons=incomplete_reasons, degraded=degraded,
        models_used={
            "finder": models.finder_deep if config.mode == "deep" else models.finder,
            **(
                {"finder_rules": models.finder_rules}
                if "rules_compliance" in ran_lenses else {}
            ),
            "verifier": models.verifier,
            **({"security": models.security} if ran_security else {}),
            **(
                {
                    "adjudicator": models.adjudicator,
                    "adjudicator_fallback": models.adjudicator_fallback,
                }
                if verifier_votes(config) > 1 else {}
            ),
            **(
                {"adjudicated_by": ", ".join(
                    f"{m} ({n})" for m, n in sorted(adjudicated_by.items())
                )}
                if adjudicated_by else {}
            ),
            **({"sweep": models.sweep} if sweep_enabled(config) else {}),
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
        mode_requested=mode_requested, crucial_paths=crucial, estimate=estimate,
        limit_resets=backend.limit.resets if backend.limit else None,
        calls_done=backend.done + backend.reused,
        calls_total=backend.done + backend.reused + backend.skipped,
        resume_command=rerun_command(opts, "--resume"),
    )
    paths = write_outputs(run, report_paths(root, config.report_dir, basename))

    if outcome_name == "paused":
        # Round and findings state wait for the resumed run; only the resume point is saved.
        if not keep_paused:
            bs.paused_report = basename
            bs.paused_diff_hash = scope.diff_hash
            save_state(root, state)
        return run, paths

    bs.paused_report = bs.paused_diff_hash = None
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
    if run.outcome == "not_started":
        est = run.estimate
        what = f"estimated {est.describe()}, " if est else ""
        per_mode = "; ".join(
            f"{m} ~{e.usd_text}, ~{e.calls} calls" for m, e in run.mode_estimates.items()
        )
        return (
            f"preflight: not started: {what}above confirm_above_usd "
            f"${run.config.confirm_above_usd:g}"
            + (f" ({per_mode})" if per_mode else "")
            + f"; re-run with --mode {'|'.join(MODES)} --yes → {md}"
        )
    if run.outcome == "paused":
        resets = f" (resets {run.limit_resets})" if run.limit_resets else ""
        return (
            f"preflight: paused: Claude usage limit reached{resets}; {run.calls_done} of "
            f"{run.calls_total} calls done, {len(run.policy.report)} confirmed, "
            f"{len(run.policy.unverified)} unverified → {md} — resume with: {run.resume_command}"
        )
    from preflight_review.findings import Severity, severity_rank

    nits = n_unverified_nits(run.policy.report)
    confirmed = len(run.policy.report) - nits
    major = sum(
        1 for f in run.policy.report
        if severity_rank(f.severity) >= severity_rank(Severity.major)
    )
    line = (
        f"preflight: {run.outcome}: {confirmed} confirmed ({major} major+), "
        + (f"{nits} unverified nits, " if nits else "")
        + f"{len(run.policy.unverified)} unverified → {md}"
    )
    if run.outcome != "complete":
        line += " — merge risk unknown (review incomplete; see Not reviewed)"
    if run.degraded:
        # Kept after the outcome so "preflight: complete:" still parses; never a clean run.
        missed = [d.split(":", 1)[0] for d in run.degraded if not degraded_partial(d)]
        partial = [d.split(":", 1)[0] for d in run.degraded if degraded_partial(d)]
        parts = ([", ".join(missed) + " did not run"] if missed else []) + (
            [", ".join(partial) + " ran partially"] if partial else []
        )
        line += " — DEGRADED: " + "; ".join(parts)
    return line
