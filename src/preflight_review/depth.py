"""Review depth: resolve `mode: auto`, and estimate a run's cost and time before it starts.

A review is never cut short once it starts. Depth is chosen up front from what the change
touches, and a run whose estimate is above `confirm_above_usd` offers a choice of depth
with each one's estimate first (or, when nobody can answer, is not started at all).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

from preflight_review.config import Config
from preflight_review.context.packer import DEFAULT_BATCH_DIFF_TOKENS
from preflight_review.context.risk import risk_tier
from preflight_review.context.tokens import estimate_tokens
from preflight_review.scope import Scope, glob_to_regex

MANIFEST_CRUCIAL_CAP = 10  # crucial paths recorded in the run manifest


@lru_cache(maxsize=256)
def _glob(pattern: str):
    return glob_to_regex(pattern)


# Paths that never make `mode: auto` go deep, whatever `auto.deep_paths` says: tests, eval
# cases, fixtures and docs. Eval and fixture trees hold sample migrations, workflows and
# prompts, and a test of auth code is not auth code. Agent-instruction files and prompts
# (risk tier "source", context/risk.py) still count unless they sit under one of these
# directories.
NON_CRUCIAL_DIRS = frozenset({
    "tests", "test", "__tests__", "spec", "eval", "evals", "fixtures", "testdata", "docs", "doc",
})
NON_CRUCIAL_TIERS = ("test", "docs")


def deep_eligible(path: str) -> bool:
    """False for tests, eval cases, fixtures and docs (see NON_CRUCIAL_DIRS)."""
    if set(PurePosixPath(path).parts[:-1]) & NON_CRUCIAL_DIRS:
        return False
    return risk_tier(path) not in NON_CRUCIAL_TIERS


def crucial_paths(config: Config, paths: list[str]) -> list[str]:
    """Changed paths that match any `auto.deep_paths` glob and are not tests, eval cases,
    fixtures or docs, in diff order."""
    globs = [g for g in config.auto.deep_paths if g]
    return [p for p in paths if deep_eligible(p) and any(_glob(g).match(p) for g in globs)]


def resolve_mode(config: Config, paths: list[str]) -> tuple[str, list[str]]:
    """(mode to run, crucial paths that decided it). An explicit mode is returned as is;
    `auto` becomes `deep` when any changed path is crucial, else `standard`."""
    if config.mode != "auto":
        return config.mode, []
    hits = crucial_paths(config, paths)
    return ("deep" if hits else "standard"), hits


@dataclass(frozen=True)
class _Rates:
    # USD per call as if the call ran on Opus; model_price() scales it to the configured model.
    finder_base: float  # per finder call
    finder_per_ktok: float  # plus per 1k diff tokens in the call's batch (packet size)
    security_factor: float  # a security-lane call costs this many finder calls
    sweep_factor: float  # the one deep-mode sweep call costs this many finder calls
    vote_base: float  # per verifier vote or adjudication
    vote_per_ktok: float  # plus per 1k diff tokens of the packet it is verified against
    candidates_per_batch: float  # model findings per batch that reach the LLM verifier
    disagree_share: float  # share whose first two votes disagree (the rest run)
    escalate_share: float  # share that end in an Opus adjudication
    batch_min: float  # wall minutes per packet (its lanes run in parallel)
    vote_min: float  # wall minutes per round of parallel votes


# USD rates fitted to measured runs (2026-11-07), all on Opus: an incremental 74-file deep
# review in 10 batches made 56 finder calls for ~USD 14.8 (~0.26 each at ~12k diff tokens per
# batch) and 11 security calls for USD 3.7 (~0.34 each); the seeded eval in deep mode averaged
# ~USD 0.90 per 1-3 file case (~0.1 per finder call, ~0.03 per vote). Tool findings, trivial
# findings and nitpicks never reach the verifier, so candidates_per_batch counts model findings
# only. Minutes (batch_min, vote_min) are Sonnet wall time per packet with its lanes in
# parallel; the USD rates are unchanged. Recalibrate from the run manifest, see
# docs/measurements.md. Real cost depends mostly on how much each model reads beyond its packet: the estimate
# is an order of magnitude, not a quote.
RATES = {
    "quick": _Rates(0.03, 0.006, 1.3, 1.5, 0.01, 0.005, 3, 0.3, 0.05, 0.6, 0.5),
    "standard": _Rates(0.06, 0.012, 1.3, 1.5, 0.015, 0.01, 3, 0.3, 0.05, 1.2, 0.5),
    "deep": _Rates(0.08, 0.015, 1.3, 1.5, 0.02, 0.015, 3, 0.3, 0.05, 2.5, 0.5),
}
TRIAGE_CALL = 0.04  # Opus-equivalent USD of one triage call (one file's diff, no packet)
WALKTHROUGH_CALL = 0.10  # Opus-equivalent USD of the walkthrough call
MODES = ("quick", "standard", "deep")
TRIAGE_FILES_PER_CALL = 20  # Haiku triage batches this many files per call
DEFAULT_BUDGET = DEFAULT_BATCH_DIFF_TOKENS


@dataclass(frozen=True)
class Estimate:
    mode: str
    usd: float
    minutes: int
    files: int
    batches: int
    finder_calls: int = 0
    security_calls: int = 0
    sweep_calls: int = 0
    verify_calls: int = 0  # votes plus adjudications (expected, so approximate)
    adjudicator_calls: int = 0  # of verify_calls, escalated splits (Opus by default)
    triage_calls: int = 0
    walkthrough_calls: int = 0
    batches_by_mode: dict[str, int] = field(default_factory=dict)

    @property
    def calls(self) -> int:
        return (
            self.finder_calls + self.security_calls + self.sweep_calls + self.verify_calls
            + self.triage_calls + self.walkthrough_calls
        )

    @property
    def usd_text(self) -> str:
        return f"${self.usd:.1f}" if self.usd < 10 else f"${self.usd:.0f}"

    def calls_text(self) -> str:
        """`~42 calls: 30 finders, 6 security, 1 sweep, ~5 verify, 10 triage, 1 walkthrough`."""
        parts = [f"{self.finder_calls} finders"]
        if self.security_calls:
            parts.append(f"{self.security_calls} security")
        if self.sweep_calls:
            parts.append(f"{self.sweep_calls} sweep")
        verify = f"~{self.verify_calls} verify"
        if self.adjudicator_calls:
            verify += f" (~{self.adjudicator_calls} adjudicated)"
        parts.append(verify)
        if self.triage_calls:
            parts.append(f"{self.triage_calls} triage")
        if self.walkthrough_calls:
            parts.append(f"{self.walkthrough_calls} walkthrough")
        return f"~{self.calls} calls: " + ", ".join(parts)

    def describe(self) -> str:
        if self.mode == "auto":
            by = self.batches_by_mode
            return (
                f"~{self.usd_text}, ~{self.minutes} min for {self.files} files in auto mode "
                f"({by.get('deep', 0)} deep, {by.get('standard', 0)} standard packets; "
                f"{self.calls_text()}; rough)"
            )
        return (
            f"~{self.usd_text}, ~{self.minutes} min for {self.files} files in {self.mode} "
            f"mode ({self.batches} batch{'es' if self.batches != 1 else ''}; "
            f"{self.calls_text()}; rough)"
        )


def diff_tokens(scope: Scope) -> int:
    """Estimated tokens of the reviewable (non-binary) diff."""
    total = 0
    for fc in scope.files:
        if fc.is_binary:
            continue
        body = "\n".join(line for h in fc.hunks for line in h.lines)
        total += estimate_tokens(f"--- {fc.path}\n{body}")
    return total


def votes_per_candidate(config: Config, mode: str) -> int:
    """Verifier votes for one candidate in `mode`: 1 in quick mode, else `verifier.votes`."""
    return 1 if mode == "quick" else max(1, config.verifier.votes)


# Price per call relative to Opus, by model family (Sonnet ~1/2, Haiku ~1/40 of Opus; the
# user's working figures, 2026-11-07). An unknown name (fable, a full model id) counts as Opus.
MODEL_PRICE = {"opus": 1.0, "sonnet": 0.5, "haiku": 0.025}


def model_price(model: str) -> float:
    low = model.lower()
    return next((v for k, v in MODEL_PRICE.items() if k in low), 1.0)


def _file_tokens(fc) -> int:
    if fc.is_binary:
        return 0
    body = "\n".join(line for h in fc.hunks for line in h.lines)
    return estimate_tokens(f"--- {fc.path}\n{body}")


def estimate_run(
    scope: Scope, config: Config, mode: str, n_lanes: int, batch_tokens: int | dict[str, int],
    security: bool | None = None, *, file_modes: dict[str, str] | None = None,
) -> Estimate:
    """Rough USD, calls and wall time of a review of `scope` in `mode` with `n_lanes` lanes
    per packet (finder lenses plus the security lane when `security`, which defaults to
    `config.security_lane`), priced by the configured model of each stage.

    `batch_tokens` is the diff budget per packet: an int (the same for every mode) or a dict
    per mode. `mode` may be "auto": each file is `file_modes[path]` or `file_mode(config,
    path)`, and packets are counted per mode. Packets run `concurrency // n_lanes` at a time,
    verification overlaps all but the last packet, and a deep run adds one sweep (when
    `sweep` is on). Haiku triage is one call per TRIAGE_FILES_PER_CALL changed files."""
    models = config.models
    security = config.security_lane if security is None else security
    budgets = (
        {m: max(1, batch_tokens) for m in MODES} if isinstance(batch_tokens, int)
        else {m: max(1, batch_tokens.get(m, DEFAULT_BUDGET)) for m in MODES}
    )
    mode_tokens = dict.fromkeys(MODES, 0)
    for fc in scope.files:
        m = (file_modes or {}).get(fc.path) or file_mode(config, fc.path) if mode == "auto" else mode
        mode_tokens[m if m in mode_tokens else "deep"] += _file_tokens(fc)
    packets = {
        m: math.ceil(t / budgets[m]) if t > 0 else 0 for m, t in mode_tokens.items()
    }
    if not any(packets.values()):  # empty or binary-only diff: one nominal packet
        packets["standard" if mode == "auto" else (mode if mode in packets else "deep")] = 1
    total_packets = sum(packets.values())

    n_sec = 1 if security and n_lanes > 0 else 0
    sweep_calls = 1 if config.sweep and packets["deep"] else 0
    triage_calls = math.ceil(len(scope.files) / TRIAGE_FILES_PER_CALL) if config.triage.haiku else 0
    walkthrough_calls = 1 if config.walkthrough else 0

    usd = 0.0
    finder_calls = security_calls = sonnet_votes = adjudications = 0
    for m, n in packets.items():
        if not n:
            continue
        rates = RATES[m]
        ktok = mode_tokens[m] / n / 1000
        per_call = rates.finder_base + rates.finder_per_ktok * ktok
        per_vote = rates.vote_base + rates.vote_per_ktok * ktok
        finder_model = models.finder_deep if m == "deep" else models.finder
        sweep_here = sweep_calls if m == "deep" else 0
        candidates = (n + 0.5 * sweep_here) * rates.candidates_per_batch
        votes = votes_per_candidate(config, m)
        # Two votes first; the rest only when those two disagree (~2.3 votes for 3). A few
        # escalated splits (critical, or major+ security) go to the adjudicator.
        per_candidate = 1.0 if votes == 1 else 2 + rates.disagree_share * (votes - 2)
        adj = math.ceil(candidates * rates.escalate_share) if votes > 1 else 0
        sv = math.ceil(candidates * per_candidate) if candidates else 0
        finder_calls += n * max(0, n_lanes - n_sec)
        security_calls += n * n_sec
        sonnet_votes += sv
        adjudications += adj
        usd += (
            per_call * n * max(0, n_lanes - n_sec) * model_price(finder_model)
            + per_call * rates.security_factor * n * n_sec * model_price(models.security)
            + per_call * rates.sweep_factor * sweep_here * model_price(models.sweep)
            + per_vote * sv * model_price(models.verifier)
            + per_vote * adj * model_price(models.adjudicator)
        )
    usd += (
        TRIAGE_CALL * triage_calls * model_price(models.triage)
        + WALKTHROUGH_CALL * walkthrough_calls * model_price(models.walkthrough)
    )
    verify_calls = sonnet_votes + adjudications

    conc = max(1, config.concurrency)
    parallel = max(1.0, config.concurrency / max(1, n_lanes))
    packet_minutes = sum(RATES[m].batch_min * n for m, n in packets.items())
    vote_rates = RATES["deep" if packets["deep"] else next(m for m in MODES if packets[m])]
    minutes = math.ceil(packet_minutes / parallel)
    minutes += RATES["deep"].batch_min if sweep_calls else 0
    minutes += math.ceil(math.ceil(verify_calls / total_packets) / conc) * vote_rates.vote_min
    return Estimate(
        mode=mode, usd=usd, minutes=max(1, math.ceil(minutes)),
        files=len(scope.files), batches=total_packets, finder_calls=finder_calls,
        security_calls=security_calls, sweep_calls=sweep_calls, verify_calls=verify_calls,
        adjudicator_calls=adjudications, triage_calls=triage_calls,
        walkthrough_calls=walkthrough_calls,
        batches_by_mode={m: n for m, n in packets.items() if n},
    )


def file_mode(config: Config, path: str) -> str:
    """Depth of one file: the explicit `config.mode`, or under auto `deep` for a crucial
    path and `standard` otherwise."""
    if config.mode != "auto":
        return config.mode
    return "deep" if crucial_paths(config, [path]) else "standard"


def batch_mode(config: Config, paths: list[str]) -> str:
    """Depth of a packet: the explicit `config.mode`, or under auto `deep` when any of its
    paths is crucial, else `standard`."""
    if config.mode != "auto":
        return config.mode
    return "deep" if crucial_paths(config, paths) else "standard"


def mode_summary(config: Config, mode: str) -> str:
    """What a depth buys, for the pre-run menu: finder model, effort, votes and sweep."""
    models = config.models
    finder = models.finder_deep if mode == "deep" else models.finder
    effort = getattr(config.effort, mode, None)
    votes = votes_per_candidate(config, mode)
    verifier = models.verifier.capitalize()
    if votes == 1:
        verify = f"1 {verifier} vote"
    else:
        verify = f"up to {votes} {verifier} votes, {models.adjudicator} on critical splits"
    sweep = "sweep" if config.sweep and mode == "deep" else "no sweep"
    return (
        f"{finder.capitalize()} finders"
        + (f" at {effort} effort" if effort else "")
        + f", {verify}, {sweep}"
    )


def depth_menu(
    config: Config, estimates: dict[str, Estimate], default: str, auto: bool
) -> str:
    """The pre-run depth menu: one line per mode with its estimate, then cancel. When
    `estimates` has an "auto" key it is option 4 (mixed depth), before cancel."""
    any_est = next(iter(estimates.values()))
    batches = f"{any_est.batches} batch{'es' if any_est.batches != 1 else ''}"
    lines = [f"preflight: {any_est.files} files, {batches}. Choose depth:"]
    options = [*MODES, "auto"] if "auto" in estimates else list(MODES)
    for i, mode in enumerate(options, 1):
        est = estimates[mode]
        mark = f"  ({'auto' if auto else 'configured'})" if mode == default else ""
        summary = (
            "mixed: deep only for packets with crucial files" if mode == "auto"
            else mode_summary(config, mode)
        )
        lines.append(
            f"  {i}) {mode:<9} ~{est.usd_text:<6} ~{est.calls} calls  ~{est.minutes} min  "
            f"({summary}){mark}"
        )
    lines.append(f"  {len(options) + 1}) cancel")
    lines.append(f"Choose [{options.index(default) + 1}]: ")
    return "\n".join(lines)


def parse_choice(answer: str, default: str, options: tuple[str, ...] = MODES) -> str:
    """A menu answer as a mode, or "cancel". Enter picks `default`; a number or a mode name
    picks that mode ("auto" and 4 only when "auto" is in `options`); anything else cancels
    (nothing is spent on a typo)."""
    a = answer.strip().lower()
    if not a:
        return default
    if a.isdigit() and 1 <= int(a) <= len(options):
        return options[int(a) - 1]
    return a if a in options else "cancel"
