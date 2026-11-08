"""Pipeline orchestration: scope -> tools -> context -> finders -> verify -> policy -> report."""

from __future__ import annotations

import dataclasses
import json
import os
import posixpath
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from preflight_review import findings as findings_mod
from preflight_review import prompts
from preflight_review.config import (
    Config,
    legacy_defaults_note,
    load_config,
    merge_overrides,
    worktree_config_diff,
)
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
    Batch,
    batch_note,
    build_packet,
    context_bounds,
    plan_batches,
)
from preflight_review.context.risk import risk_tier
from preflight_review.context.symbols import RepoIndex
from preflight_review.depth import (
    MODES,
    Estimate,
    crucial_paths,
    depth_menu,
    estimate_run,
    file_mode,
    parse_choice,
    votes_per_candidate,
)
from preflight_review.errors import ConfigError, GitError
from preflight_review.findings import Lane
from preflight_review.gitutil import (
    blob_shas,
    current_branch,
    toplevel,
    worktree_blob_sha,
)
from preflight_review.model import FakeBackend, ModelResult, claude_version
from preflight_review.model.limits import BreakerBackend, is_paused, load_calls
from preflight_review.naming import pr_label, report_basename, report_paths
from preflight_review.render import RunResult, write_outputs
from preflight_review.render.report import coverage, n_unverified_nits
from preflight_review.reviewed_cache import ReviewedCache, file_key, fingerprint
from preflight_review.scope import (
    _is_docs_or_lockfile,
    docs_only_skip,
    only_docs_or_lockfiles,
    resolve_scope,
)
from preflight_review.stages.cluster import cluster, merge_verified, prefilter
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
from preflight_review.stages.sweep import run_sweep
from preflight_review.stages.verify import VerdictCache, run_verifier
from preflight_review.state import BranchState, FindingState, load_state, save_state
from preflight_review.tools import degraded_partial, degraded_tools, run_tools
from preflight_review.tools.cochange_finding import companion_findings
from preflight_review.tools.key_consumers import key_consumer_signals

INTERNAL_FILTERS = ["!.preflight/**"]
DEFAULT_BASE = "origin/main"
# Per-packet diff budget override: None uses `config.packet_tokens` (per depth); an int
# (tests monkeypatch it) is the budget of every packet, whatever its depth.
BATCH_DIFF_TOKENS: int | None = None


@dataclass
class BatchOutcome:
    """One batch's model work, returned by a batch worker (workers share no lists)."""

    finders: object  # FinderOutcome
    security: object | None  # FinderOutcome of the security lane, or None
    cost: float  # finder + security lanes
    total: int = 0  # lanes that ran (path-gated lenses excluded)
    failed: int = 0
    notes: list[str] = field(default_factory=list)
    verify: object | None = None  # VerifyOutcome when the batch verified its own findings
    verify_keys: dict = field(default_factory=dict)  # stable_id -> sort key (_verify_sort_keys)
    suppressed: list = field(default_factory=list)  # its prefilter's suppressed
    pre_dropped: list = field(default_factory=list)  # its prefilter's dropped


@dataclass
class ReviewOptions:
    repo_root: Path
    base: str | None = None
    full: bool = False
    # Always on: only file changes not reviewed before are reviewed (`full` turns it off).
    incremental: bool = True
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


def _verify_sort_keys(findings, packet_of) -> dict[str, tuple]:
    """stable_id -> the order run_verifier calls a finding in: by packet sha, then by the
    candidate order (severity, inline first, file, line), then input order. Taken before the
    verifier runs: a verdict can lower the severity."""
    keys: dict[str, tuple] = {}
    for pos, f in enumerate(findings):
        sid = f.stable_id or ""
        keys.setdefault(sid, (
            getattr(packet_of(f), "sha", ""), -findings_mod.severity_rank(f.severity),
            f.lane != Lane.inline, f.file, f.start_line, pos,
        ))
    return keys


def _keyed_calls(results: dict, keys: dict, offset: int, dup_suffix: str) -> list[tuple]:
    """(sort key, result key, result, suffix) per verifier/adjudicator result. A run records
    every finding's first two votes, then the extra votes on splits, then adjudications."""
    out = []
    for i, (key, res) in enumerate(results.items()):
        kind, _, rest = key.partition(":")
        sid, _, tail = rest.partition("#")
        if kind == "adjudicator":
            phase = 2
        else:
            phase = 1 if tail.startswith("v") and tail[1:].isdigit() and int(tail[1:]) > 2 else 0
        out.append(((phase, keys.get(sid, ("",)), offset + i), key, res, dup_suffix))
    return out


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


MODE_RANK = {"quick": 0, "standard": 1, "deep": 2}


def _packet_budgets(config: Config) -> dict[str, int]:
    """Diff tokens per packet by depth: `config.packet_tokens`, or BATCH_DIFF_TOKENS for
    every depth when it is set."""
    if BATCH_DIFF_TOKENS is not None:
        return dict.fromkeys(MODES, int(BATCH_DIFF_TOKENS))
    return {m: int(getattr(config.packet_tokens, m)) for m in MODES}


def _concrete_mode(config: Config) -> str:
    """The explicit mode, or standard under auto (for work that belongs to no packet)."""
    return "standard" if config.mode not in MODES else config.mode


def _with_mode(config: Config, mode: str) -> Config:
    return config if config.mode == mode else dataclasses.replace(config, mode=mode)


def _blob_pairs(root: Path, scope) -> dict[str, tuple[str | None, str]]:
    """path -> (old blob at the diff start or None when added, new blob or 'D' when gone).
    The new side is the worktree file, or the blob at HEAD for a committed-only review."""
    olds = {fc.path: (fc.old_path or fc.path) for fc in scope.files if fc.status != "A"}
    old_at = (
        blob_shas(root, scope.diff_from or scope.base_sha, list(dict.fromkeys(olds.values())))
        if olds else {}
    )
    paths = [fc.path for fc in scope.files]
    if scope.include_worktree:
        new = {p: worktree_blob_sha(root, p) for p in paths}
    else:
        at_head = blob_shas(root, "HEAD", paths)
        new = {p: at_head.get(p) or "D" for p in paths}
    return {p: (old_at.get(olds[p]) if p in olds else None, new[p]) for p in paths}


def _cache_keys(
    root: Path, scope, config: Config, lens_list: list[str], file_modes: dict[str, str],
    blobs: dict[str, tuple[str | None, str]], resolved_base: str, guide_memo: dict[str, str],
) -> dict[str, str]:
    """ReviewedCache key of every scope file: its exact change plus a fingerprint of its
    depth, the lenses whose gates it matches, the security lane and its guidelines.
    Path instructions are in the fingerprint already, so they are left out of the
    guideline text (which is memoized per directory)."""
    fp_memo: dict[tuple, str] = {}
    keys: dict[str, str] = {}
    for fc in scope.files:
        p = fc.path
        if p not in blobs:
            continue
        d = posixpath.dirname(p)
        if d not in guide_memo:
            found = discover_guidelines(root, [p], config, resolved_base)
            guide_memo[d] = "\n\n".join(
                f"{g.source_path}\n{g.text}" for g in found if g.kind != "path_instruction"
            )
        lenses = [x for x in lens_list if x not in LENSES or lens_applies(LENSES[x], [p])]
        mode = file_modes.get(p) or _concrete_mode(config)
        fk = (mode, tuple(lenses), d)
        if fk not in fp_memo:
            fp_memo[fk] = fingerprint(config, mode, lenses, config.security_lane, guide_memo[d])
        old, new = blobs[p]
        keys[p] = file_key(p, fc.status, old, new, fp_memo[fk])
    return keys


def _or_empty(fn, *args, **kwargs) -> list:
    try:
        return list(fn(*args, **kwargs))
    except Exception:  # noqa: BLE001 - non-python files, parse errors: no neighbours known
        return []


def _cache_neighbours(scope, index: RepoIndex, paths: list[str], companions) -> dict[str, set[str]]:
    """For each path: the files holding a caller or callee of its changed symbols, and the
    changed files it is a co-change companion of."""
    by_path = {fc.path: fc for fc in scope.files}
    out: dict[str, set[str]] = {}
    for p in paths:
        near: set[str] = set()
        one = dataclasses.replace(scope, files=[by_path[p]])
        for s in _or_empty(changed_symbols, one, index):
            sites = _or_empty(find_callers, s, index, scope, per_symbol=10_000, context=0)
            near.update(c.file for c in sites)
            near.update(c.file for c in _or_empty(find_callees, s, index))
        near.update(c.changed for c in companions if c.companion == p)
        near.discard(p)
        out[p] = near
    return out


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
    # Config is read from the base ref: a branch edit to it applies only once merged.
    drift = worktree_config_diff(root, base_ref)
    drift_note = (
        f".preflight/config.yaml on this branch differs from {base_ref} in: "
        f"{', '.join(drift)}; this review uses the {base_ref} version until it is merged"
    ) if drift else None
    if drift_note:
        print(f"preflight: {drift_note}", file=sys.stderr)

    state = load_state(root)
    branch_key = branch or "HEAD"
    bs = state.branches.setdefault(branch_key, BranchState())
    filters = list(config.path_filters) + INTERNAL_FILTERS
    # Always the whole base...HEAD diff: what was already reviewed is skipped per file
    # (reviewed-file cache below), not by moving the diff start.
    scope = resolve_scope(root, base_ref, opts.include_worktree, path_filters=filters)
    resolved_base = scope.base_ref
    all_paths = [f.path for f in scope.files]

    # Depth is per packet and fixed before any model call: under auto a packet holding a
    # crucial path runs deep and the rest standard; an explicit mode applies to every
    # packet. config.mode keeps the requested mode; each packet's stages get a copy with
    # its own. A started review is never cut short for cost (see the estimate gate).
    mode_requested = config.mode
    crucial = crucial_paths(config, all_paths) if config.mode == "auto" else []

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
    # One cap on model calls in flight across stages and parallel batches.
    backend = BreakerBackend(backend, cached_calls, max_inflight=max(1, config.concurrency))
    if legacy:
        notes.append(legacy)
    if drift_note:
        notes.append(drift_note)
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
    batch_modes: dict[int, str] = {}  # packet index -> its depth
    path_mode: dict[str, str] = {}  # path -> depth of the packet(s) that carried it
    unclean: set[str] = set()  # paths of a packet with a failed lane: never cached
    sweep_ran = False

    autos = detect_extensions(root) if config.auto_lenses else []
    lens_list, lens_notes = effective_lenses(config, all_paths, autos)
    gated_hit = _gated_matches(lens_list, all_paths)
    index = RepoIndex(root, filters)
    _companions: list = []

    def get_companions() -> list:
        if not _companions and all_paths:
            _companions.append(mine_cochange(root, all_paths))
        return _companions[0] if _companions else []

    # Reviewed-file cache: a file whose exact change (path, status, old and new blob) was
    # reviewed cleanly before, under the same prompts, depth, lenses and guidelines, is not
    # reviewed again; the report lists it under Reviewed earlier. --full reads no entry.
    file_modes = {p: file_mode(config, p) for p in all_paths}
    reviewed_cache = ReviewedCache(root)
    guide_memo: dict[str, str] = {}
    blobs: dict[str, tuple[str | None, str]] = {}
    cache_keys: dict[str, str] = {}
    if scope.files:
        try:
            blobs = _blob_pairs(root, scope)
            cache_keys = _cache_keys(
                root, scope, config, lens_list, file_modes, blobs, resolved_base, guide_memo
            )
        except GitError as e:
            notes.append(f"reviewed-file cache not used: {e}")
            blobs, cache_keys = {}, {}
    def lookup_hits(keys: dict[str, str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if opts.full:
            return found
        for p, key in keys.items():
            entry = reviewed_cache.get(key)
            # Its findings must still be tracked on this branch, so they can be carried
            # forward under Previously reported (an entry from another branch may not be).
            if entry is not None and all(sid in bs.findings for sid in entry.get("findings") or []):
                found[p] = entry
        if found and len(found) < len(all_paths):
            # A cached file is reviewed again when a caller or callee of its changed
            # symbols, or a file it co-changes with, is reviewed fresh this run. The symbol
            # index covers Python only: a cached non-Python file has unknown neighbours, so
            # it is reviewed again whenever any other file is reviewed fresh.
            fresh_set = {p for p in all_paths if p not in found}
            near = _cache_neighbours(scope, index, list(found), get_companions())
            demoted = 0
            moved = True
            while moved:
                moved = False
                for p in list(found):
                    unknown = not p.endswith(".py")
                    if (unknown and fresh_set) or near.get(p, set()) & fresh_set:
                        del found[p]
                        fresh_set.add(p)
                        demoted += 1
                        moved = True
            if demoted:
                notes.append(
                    f"reviewed-file cache: {demoted} file(s) reviewed again because a caller, "
                    "callee or co-change companion changed (non-Python files: any fresh file)"
                )
        return found

    hits = lookup_hits(cache_keys)
    reviewed_earlier = [
        (p, str(hits[p].get("run", "")), int(hits[p].get("round") or 0))
        for p in all_paths if p in hits
    ]
    fresh_files = [fc for fc in scope.files if fc.path not in hits]
    fresh_paths = [fc.path for fc in fresh_files]
    # Estimate, triage, planning and every model stage see only the fresh files; tools,
    # co-change and key-consumer signals stay on the whole diff (cheap, deterministic).
    fresh_scope = dataclasses.replace(scope, files=fresh_files, incremental=bool(hits))
    if config.mode == "auto":
        crucial = crucial_paths(config, fresh_paths)
    if hits:
        notes.append(
            f"reviewed-file cache: {len(hits)} of {len(all_paths)} changed files reviewed "
            "earlier (see Reviewed earlier); --full reviews them again"
        )

    budgets = _packet_budgets(config)
    skip_all = not scope.files or (docs_only_skip(scope, config.docs_review) and not gated_hit)
    estimate: Estimate | None = None
    if fresh_scope.files and not skip_all:
        runnable_all, _ = validate_lenses(lens_list)
        n_lanes = sum(1 for x in runnable_all if lens_applies(LENSES[x], fresh_paths))
        n_lanes += 1 if config.security_lane else 0

        def estimate_for(mode: str) -> Estimate:
            return estimate_run(
                fresh_scope, config, mode, n_lanes, budgets, file_modes=file_modes
            )

        estimate = estimate_for(config.mode)
        print(f"preflight: estimated {estimate.describe()}", file=sys.stderr)
        notes.append(f"pre-run estimate: {estimate.describe()}")
        threshold = config.confirm_above_usd
        # A resumed run was already confirmed when it first started.
        if threshold is not None and estimate.usd > threshold and not (opts.yes or resumed_from):
            options = (*MODES, "auto") if config.mode == "auto" else MODES
            estimates = {m: estimate_for(m) for m in options}
            default = config.mode if config.mode in options else "standard"
            answer = choose(depth_menu(config, estimates, default, mode_requested == "auto"))
            picked = None if answer is None else parse_choice(answer, default, options=options)
            if picked in options:
                if picked != config.mode:
                    # One depth for every packet.
                    notes.append(
                        f"depth chosen at the pre-run menu: {picked} (was {config.mode})"
                    )
                    config = dataclasses.replace(config, mode=picked)
                    mode_requested = "menu"
                    estimate = estimates[picked]
                    file_modes = {p: picked for p in all_paths}
                    if blobs:
                        cache_keys = _cache_keys(
                            root, scope, config, lens_list, file_modes, blobs, resolved_base,
                            guide_memo,
                        )
                        # Cached files must have been reviewed at the chosen depth too.
                        if hits:
                            hits = lookup_hits(cache_keys)
                            reviewed_earlier = [
                                (p, str(hits[p].get("run", "")), int(hits[p].get("round") or 0))
                                for p in all_paths if p in hits
                            ]
                            fresh_files = [fc for fc in scope.files if fc.path not in hits]
                            fresh_paths = [fc.path for fc in fresh_files]
                            fresh_scope = dataclasses.replace(
                                scope, files=fresh_files, incremental=bool(hits)
                            )
                    notes.append(f"pre-run estimate ({picked}): {estimate.describe()}")
            else:
                commands = [rerun_command(opts, "--mode", m, "--yes") for m in options]
                why = "cancelled at the menu" if answer is not None else "no terminal to choose"
                reason = (
                    f"{why}; estimated {estimate.describe()}, above "
                    f"confirm_above_usd ${threshold:g}"
                )
                for m in options:
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
                    reviewed_earlier=reviewed_earlier,
                )
                # State is untouched: the next run reuses this round number.
                return run, write_outputs(run, report_paths(root, config.report_dir, basename))

    # review_docs: a path-gated lens covers some changed doc, not just any changed file
    # (a workflow change must not send every unrelated doc past Haiku triage).
    gated_docs = _gated_matches(lens_list, [p for p in fresh_paths if _is_docs_or_lockfile(p)])
    rule_dirs = [d for hit in autos for d in hit.ast_rule_dirs]

    def _tools():
        return run_tools(scope, config, rule_dirs) if rule_dirs else run_tools(scope, config)

    # Triage (model) and the deterministic tools do not depend on each other: run both.
    tools_out = None
    with ThreadPoolExecutor(max_workers=1) as tools_pool:
        tools_future = None if skip_all else tools_pool.submit(_tools)
        tri = triage(fresh_scope, config, backend, review_docs=bool(gated_docs))
        if tools_future is not None:
            tools_out = tools_future.result()
    lane_results.update(tri.lane_results)
    triaged_out = [p for p, d in tri.per_file.items() if d == "APPROVED"]
    triage_notes = dict(tri.notes)

    # A docs-only diff that touches a path-gated lens's files (a deploy runbook) is reviewed
    # by that lens even under docs_review: off.
    if skip_all:
        notes.append(
            "empty diff: model stages skipped" if not scope.files
            else "docs/lockfile-only diff: model stages skipped"
        )
        policy = apply_policy([], config, bs, round_no)
    else:
        review_scope = dataclasses.replace(
            fresh_scope,
            files=[fc for fc in fresh_files if tri.per_file.get(fc.path) == NEEDS_REVIEW],
        )
        # Triage can leave only docs in review_scope (code files approved): the same
        # docs_review rule then applies to what is left.
        docs_left_skip = docs_only_skip(review_scope, config.docs_review) and not _gated_matches(
            lens_list, [f.path for f in review_scope.files]
        )
        model_stages = bool(review_scope.files) and not docs_left_skip

        def qualname(f) -> str | None:
            try:
                sym = index.enclosing_symbol(f.file, f.start_line)
            except Exception:  # non-python files, parse errors
                sym = None
            return sym.qualname if sym else None

        tool_findings, tool_statuses = tools_out
        for f in tool_findings:
            f.stable_id = findings_mod.stable_id(f, qualname(f))

        companions = get_companions()
        companion_found = companion_findings(companions, scope)
        key_signals = key_consumer_signals(scope, config)
        finder_findings: list = []

        if not model_stages:
            if not fresh_files:
                rounds = sorted({rnd for _p, _r, rnd in reviewed_earlier})
                notes.append(
                    f"all {len(all_paths)} changed files were reviewed earlier (round"
                    f"{'s' if len(rounds) != 1 else ''} {', '.join(f'{r:02d}' for r in rounds)}); "
                    "model stages skipped"
                )
            elif review_scope.files:
                notes.append("triage left only docs/lockfiles: model stages skipped")
            else:
                notes.append(
                    f"triage: {tri.reason or 'no files need model review'}; model stages skipped"
                )
            packet = None
            reviewed_paths = []
            kept, suppressed, pre_dropped = prefilter(tool_findings + companion_found, scope, config, [])
            vout = run_verifier(
                cluster(kept), None, _with_mode(config, _concrete_mode(config)), backend, scope,
                VerdictCache(root), "",
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
            plan = plan_batches(
                review_scope, index, budgets["standard"], companions, fan_in=fan_in,
                file_modes=file_modes, budget_by_mode=budgets,
            )
            bconfigs: dict[int, Config] = {}
            for b in plan.batches:
                bmode = b.mode if b.mode in MODES else _concrete_mode(config)
                bconfigs[b.index] = _with_mode(config, bmode)
                for pth in b.paths:
                    path_mode.setdefault(pth, bmode)
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

            cache = VerdictCache(root)
            # Verify each packet's findings as soon as its finders finish (overlapping later
            # packets, while its packet cache is warm). A run budget or a verifier cap is a
            # whole-run limit, so those runs verify once after every batch, as before.
            per_packet = config.budget_usd is None and config.verifier.cap is None
            n_lanes = len(runnable) + (1 if run_security else 0)
            # Tool and co-change findings are verified with the first batch (plan order) that
            # holds their file; the rest, and those of a batch that fails, after all batches.
            first_batch: dict[str, int] = {}
            for b in plan.batches:
                for pth in b.paths:
                    first_batch.setdefault(pth, b.index)
            extra_findings = tool_findings + companion_found
            assigned: dict[int, list] = {}
            if per_packet:
                for f in extra_findings:
                    if f.file in first_batch:
                        assigned.setdefault(first_batch[f.file], []).append(f)
            all_packets: dict[str, list[tuple[object, Batch]]] = {}  # every built packet

            def _prepare(pos: int, batch: Batch):
                bpaths = set(batch.paths)
                bscope = dataclasses.replace(review_scope, files=batch.files)
                bsyms = changed_symbols(bscope, index)
                callers_map = {s.qualname: find_callers(s, index, bscope) for s in bsyms}
                callees_map = {s.qualname: find_callees(s, index) for s in bsyms}
                tests = tests_touching(bsyms, root)
                bconfig = bconfigs[batch.index]
                budget = budgets[bconfig.mode]
                pkt = build_packet(
                    bscope, index, bconfig,
                    [
                        f for f in tool_findings
                        if f.lane in (Lane.inline, Lane.nitpick) and f.file in bpaths
                    ],
                    guidelines, learnings, intent,
                    [c for c in companions if c.changed in bpaths],
                    callers_map, callees_map, tests,
                    history=history,
                    batch_note=batch_note(batch, n_batches) if n_batches > 1 else None,
                    diff_cap=budget,
                    bounds=context_bounds(budget),
                    candidates=[k.render() for k in key_signals if k.file in bpaths],
                )
                packets.append(pkt)
                if pos == 0:
                    try:
                        index.save()
                    except OSError:
                        pass
                omitted = set(pkt.omitted)
                for pth in batch.paths:
                    if pth in set(pkt.files) and pth not in omitted:
                        all_packets.setdefault(pth, []).append((pkt, batch))
                return pkt, bscope

            def _run_batch(batch: Batch, pkt, bscope, bconfig: Config) -> BatchOutcome:
                """One batch's model work: finders and security lane, then (per_packet) the
                verification of its findings. Returns everything; touches no shared list."""
                suffix = "" if n_batches == 1 else f"@b{batch.index}"
                tag = "" if n_batches == 1 else f" (batch {batch.index})"
                sec = None
                if run_security:
                    with ThreadPoolExecutor(max_workers=1) as sec_pool:
                        sec_future = sec_pool.submit(
                            run_security_lane, pkt, bconfig, backend, bscope,
                            system_prompt, index, suffix,
                        )
                        outcome = run_finders(
                            pkt, run_lenses, bconfig, backend, bscope, system_prompt,
                            index, lane_suffix=suffix, lens_note=lens_note,
                        )
                        sec = sec_future.result()
                else:
                    outcome = run_finders(
                        pkt, run_lenses, bconfig, backend, bscope, system_prompt,
                        index, lane_suffix=suffix, lens_note=lens_note,
                    )
                b_total = len(runnable) - len(outcome.gated) + (1 if sec is not None else 0)
                b_failed = len(outcome.incomplete) + (len(sec.incomplete) if sec is not None else 0)
                b_cost = sum(r.cost_usd for r in outcome.lane_results.values())
                if sec is not None:
                    b_cost += sum(r.cost_usd for r in sec.lane_results.values())
                bo = BatchOutcome(
                    finders=outcome, security=sec, cost=b_cost, total=b_total, failed=b_failed,
                    notes=[f"{n}{tag}" for n in outcome.shallow],
                )
                if per_packet and b_total and b_failed < b_total:
                    mine = list(outcome.findings) + (list(sec.findings) if sec is not None else [])
                    mine += assigned.get(batch.index, [])
                    # The full run scope: prefilter's docs share is a whole-diff measure.
                    kept_b, bo.suppressed, bo.pre_dropped = prefilter(mine, scope, config, learnings)
                    bpaths = set(batch.paths)

                    def pf(f):
                        if f.file in bpaths:
                            return packet_for_finding(f, [(pkt, batch)])
                        return packet_for_finding(f, all_packets.get(f.file))

                    clustered_b = cluster(kept_b)
                    bo.verify_keys = _verify_sort_keys(clustered_b, pf)
                    bo.verify = run_verifier(
                        clustered_b, pkt, bconfig, backend, review_scope, cache,
                        system_prompt, packet_for=pf,
                    )
                return bo

            outcomes: dict[int, BatchOutcome] = {}
            prepared: dict[int, tuple] = {}
            if config.budget_usd is not None:
                # Opt-in run budget: one batch at a time, stopping before a batch the budget
                # cannot cover (the rest are reported as not reviewed).
                for pos, batch in enumerate(plan.batches):
                    if pos > 0:
                        spent = sum(r.cost_usd for r in lane_results.values()) + sum(
                            o.cost for o in outcomes.values()
                        )
                        est = max(o.cost for o in outcomes.values()) if outcomes else 0.0
                        if spent + est > config.budget_usd:
                            reason = (
                                f"run budget exhausted (USD {spent:.2f} spent, next batch est. "
                                f"USD {est:.2f}, budget USD {config.budget_usd:.2f})"
                            )
                            for rest in plan.batches[pos:]:
                                _mark_missing(rest, reason)
                            break
                    prepared[batch.index] = _prepare(pos, batch)
                    outcomes[batch.index] = _run_batch(
                        batch, *prepared[batch.index], bconfigs[batch.index]
                    )
            else:
                for pos, batch in enumerate(plan.batches):
                    prepared[batch.index] = _prepare(pos, batch)
                workers = max(1, min(n_batches, max(2, config.concurrency // max(1, n_lanes) + 1)))
                if config.concurrency <= 1 or n_batches <= 1:
                    for batch in plan.batches:
                        outcomes[batch.index] = _run_batch(
                            batch, *prepared[batch.index], bconfigs[batch.index]
                        )
                else:
                    # Batches overlap; BreakerBackend's in-flight cap is the real limiter.
                    with ThreadPoolExecutor(max_workers=workers) as batch_pool:
                        futs = {
                            b.index: batch_pool.submit(
                                _run_batch, b, *prepared[b.index], bconfigs[b.index]
                            )
                            for b in plan.batches
                        }
                        for idx, fut in futs.items():
                            outcomes[idx] = fut.result()

            # Bookkeeping in plan order, so every list and dict fills as in a serial run.
            for batch in plan.batches:
                if batch.index not in outcomes:
                    continue  # not started (run budget)
                bo = outcomes[batch.index]
                packet = prepared[batch.index][0]
                batch_modes[batch.index] = bconfigs[batch.index].mode
                if bo.failed:
                    unclean.update(batch.paths)  # a failed lane: not cached, reviewed next run
                outcome, sec = bo.finders, bo.security
                tag = "" if n_batches == 1 else f" (batch {batch.index})"
                lane_results.update(outcome.lane_results)
                incomplete_reasons += [
                    _lane_reason(lens, tag, outcome.lane_results) for lens in outcome.incomplete
                ]
                finder_findings += list(outcome.findings)
                notes += bo.notes
                for lens, paths_ in outcome.files_read_outside_packet.items():
                    outside_sets.setdefault(lens, set()).update(paths_)
                ran_lenses.update(x for x in runnable if x not in outcome.gated)
                if sec is not None:
                    finder_findings += list(sec.findings)
                    lane_results.update(sec.lane_results)
                    incomplete_reasons += [
                        _lane_reason(x, tag, sec.lane_results) for x in sec.incomplete
                    ]
                    for lens, paths_ in sec.files_read_outside_packet.items():
                        outside_sets.setdefault(lens, set()).update(paths_)
                    ran_security = True
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
                batch_costs.append(bo.cost)
                lanes_total += bo.total
                lanes_failed += bo.failed
                b_total, b_failed = bo.total, bo.failed

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
                bscope = prepared[batch.index][1]
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
                    scope, reviewed_paths, loss + not_reviewable, triaged_out, triage_notes,
                    reviewed_earlier,
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

            def packet_for(f):
                return packet_for_finding(f, packets_by_path.get(f.file))

            # Findings verified outside any one packet (tool/co-change residue, and a run
            # with a whole-run limit) get the deepest depth among the packets: never less.
            run_vconfig = _with_mode(config, max(
                batch_modes.values() or [_concrete_mode(config)], key=lambda m: MODE_RANK[m]
            ))
            if per_packet:
                verified_groups: list[list] = []
                verified_batches: set[int] = set()
                verify_calls: list[tuple[tuple, str, ModelResult, str]] = []
                for batch in plan.batches:
                    bo = outcomes.get(batch.index)
                    if bo is None or bo.verify is None:
                        continue
                    verified_batches.add(batch.index)
                    suppressed += bo.suppressed
                    pre_dropped += bo.pre_dropped
                    verify_calls += _keyed_calls(
                        bo.verify.results, bo.verify_keys, len(verify_calls), f"@b{batch.index}"
                    )
                    verify_failures += bo.verify.failures
                    for m, n in bo.verify.adjudicated_by.items():
                        adjudicated_by[m] = adjudicated_by.get(m, 0) + n
                    verified_groups.append(list(bo.verify.findings))
                # Tool/co-change findings whose batch did not verify (failed, path-gated, or
                # no batch holds their file): verified once, the way every finding used to be.
                residual = [
                    f for f in extra_findings if first_batch.get(f.file) not in verified_batches
                ]
                r_kept, r_suppressed, r_dropped = prefilter(residual, scope, config, learnings)
                suppressed += r_suppressed
                pre_dropped += r_dropped
                if r_kept:
                    r_clustered = cluster(r_kept)
                    r_keys = _verify_sort_keys(r_clustered, packet_for)
                    rvout = run_verifier(
                        r_clustered, packets[0] if packets else None, run_vconfig, backend,
                        review_scope, cache, system_prompt, packet_for=packet_for,
                    )
                    verify_calls += _keyed_calls(
                        rvout.results, r_keys, len(verify_calls), "@residual"
                    )
                    verify_failures += rvout.failures
                    for m, n in rvout.adjudicated_by.items():
                        adjudicated_by[m] = adjudicated_by.get(m, 0) + n
                    verified_groups.append(list(rvout.findings))
                # Verifier results in the order one whole-run verification records them,
                # so the report's call list does not depend on which packet finished first.
                for _sort, key, res, dup_suffix in sorted(verify_calls, key=lambda c: c[0]):
                    # The same finding verified by two packets keeps both results.
                    lane_results[key if key not in lane_results else key + dup_suffix] = res
                verified = merge_verified(verified_groups)
                cache.save()
            else:
                kept, suppressed, pre_dropped = prefilter(
                    finder_findings + tool_findings + companion_found, scope, config, learnings
                )
                clustered = cluster(kept)
                vout = run_verifier(
                    clustered, packets[0] if packets else None, run_vconfig, backend, review_scope,
                    cache, system_prompt, packet_for=packet_for,
                )
                lane_results.update(vout.results)
                verify_failures = vout.failures
                adjudicated_by.update(vout.adjudicated_by)
                verified = list(vout.findings)

            deep_targets = [t for t in sweep_targets if bconfigs[t[1].index].mode == "deep"]
            if config.sweep and deep_targets:
                # One sweep per run, over the deep packets that were delivered. It gets every
                # candidate found so far, before clustering (merged-away members count as
                # known too), and the deep packets' files; one deep packet also gets its packet.
                sweep_ran = True
                sweep_config = _with_mode(config, "deep")
                known = finder_findings + tool_findings + companion_found
                sweep_packet = deep_targets[0][0] if len(deep_targets) == 1 else None
                sweep_files = list(dict.fromkeys(p for _pk, b, _bs in deep_targets for p in b.paths))
                sw = run_sweep(
                    sweep_packet, known, sweep_config, backend, review_scope, system_prompt,
                    index, changed_files=sweep_files,
                )
                if sw.incomplete:
                    unclean.update(sweep_files)
                lane_results.update(sw.lane_results)
                incomplete_reasons += [_lane_reason(x, "", sw.lane_results) for x in sw.incomplete]
                for lens, paths_ in sw.files_read_outside_packet.items():
                    outside_packet[lens] = sorted(set(outside_packet.get(lens, [])) | set(paths_))
                # Raw output: under the batch it saw, or b0 when it covered the whole run.
                sw_index = deep_targets[0][1].index if sweep_packet is not None else 0
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
                        cluster(s_kept), packets[0] if packets else None, sweep_config, backend,
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

    if hits:
        # Files reviewed earlier keep their open findings visible: every tracked finding
        # not seen this run whose file was reviewed earlier or still exists.
        seen = {
            f.stable_id
            for group in (policy.report, policy.unverified, policy.pre_existing, policy.nit_capped)
            for f in group
            if f.stable_id
        }
        seen |= {f.stable_id for f, _ in policy.dropped if f.stable_id}
        seen |= {sid for sid, _ in policy.previously_reported}
        extra = [
            (sid, st.status) for sid, st in bs.findings.items()
            if sid not in seen and (not st.file or st.file in hits or (root / st.file).exists())
        ]
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
    # Remember the files reviewed cleanly this run. Never from a paused run, never a file
    # triaged out or not reviewed (they are not in reviewed_paths), a file of a packet with
    # a failed lane, or anything at all when a verification failed.
    if outcome_name != "paused" and not backend.tripped and reviewed_paths and cache_keys \
            and not verify_failures:
        by_file: dict[str, set[str]] = {}
        for f in policy.report + policy.unverified:
            if f.stable_id:
                by_file.setdefault(f.file, set()).add(f.stable_id)
        n_put = 0
        for pth in reviewed_paths:
            if pth in unclean or pth in hits or pth not in cache_keys:
                continue
            reviewed_cache.put(
                cache_keys[pth], run=basename, round=round_no, branch=branch_key,
                mode=path_mode.get(pth) or file_modes.get(pth) or _concrete_mode(config),
                findings=sorted(by_file.get(pth, ())),
            )
            n_put += 1
        if n_put:
            try:
                reviewed_cache.save()
            except OSError as e:
                notes.append(f"reviewed-file cache not saved: {e}")
    degraded = degraded_tools(tool_statuses)
    modes_used = set(batch_modes.values()) or {_concrete_mode(config)}

    run = RunResult(
        scope=scope, config=config, packet=packets[0] if packets else None, policy=policy,
        tool_statuses=tool_statuses, walkthrough=walkthrough, lane_results=lane_results,
        cost_usd=cost, duration_s=time.monotonic() - started, round=round_no,
        pr_label=label, basename=basename, outcome=outcome_name,
        incomplete_reasons=incomplete_reasons, degraded=degraded,
        models_used={
            "finder": models.finder_deep if modes_used == {"deep"} else models.finder,
            **(
                {"finder_deep": models.finder_deep}
                if "deep" in modes_used and modes_used != {"deep"}
                and models.finder_deep != models.finder else {}
            ),
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
                if max(votes_per_candidate(config, m) for m in modes_used) > 1 else {}
            ),
            **(
                {"adjudicated_by": ", ".join(
                    f"{m} ({n})" for m, n in sorted(adjudicated_by.items())
                )}
                if adjudicated_by else {}
            ),
            **({"sweep": models.sweep} if sweep_ran or (config.sweep and "deep" in modes_used)
               else {}),
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
        reviewed_earlier=reviewed_earlier, batch_modes=batch_modes,
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
            + f"; re-run with --mode {'|'.join(run.mode_estimates or MODES)} --yes → {md}"
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
    if getattr(run, "reviewed_earlier", None):
        n = len(run.reviewed_earlier)
        line += f" ({n} file{'s' if n != 1 else ''} reviewed earlier)"
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
