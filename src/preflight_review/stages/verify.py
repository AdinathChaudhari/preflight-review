"""Verifier stage: independent adversarial votes per finding, an adjudicator for split
votes, and an on-disk verdict cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path

from preflight_review import prompts
from preflight_review.config import Config, split_call_cap, stage_effort
from preflight_review.context.packer import Packet
from preflight_review.depth import votes_per_candidate
from preflight_review.findings import (
    Category,
    Evidence,
    Finding,
    Lane,
    Severity,
    Verdict,
    severity_rank,
    to_dict,
)
from preflight_review.model import ModelRequest, ModelResult
from preflight_review.model.limits import is_paused
from preflight_review.model.schemas import load_schema
from preflight_review.scope import Scope
from preflight_review.stages.finders import packet_system_prompt, source_lens
from preflight_review.stages.security import (
    parse_conf10,
    security_verdict_rule,
    security_verifier_prompt,
)

# Deterministic tool findings are reported as tool-verified (verdict SKIPPED) and never
# sent to a model verifier: a model vote on a lint hit costs an Opus call and adds nothing.
TOOL_SOURCES = frozenset({
    "gitleaks", "osv", "secret-files", "ruff", "ast-grep", "zizmor", "actionlint", "shellcheck",
})
PASSTHROUGH_SOURCES = TOOL_SOURCES
# verifier_reason of a trivial or nitpick-lane finding that skipped the verifier.
UNVERIFIED_NIT = "unverified-nit"
_DEMOTE = {
    Severity.critical: Severity.major,
    Severity.major: Severity.minor,
    Severity.minor: Severity.trivial,
    Severity.trivial: Severity.trivial,
    Severity.info: Severity.info,
}


def is_nit(f: Finding) -> bool:
    return f.lane == Lane.nitpick or f.severity == Severity.trivial


def is_unverified_nit(f: Finding) -> bool:
    return f.verdict == Verdict.SKIPPED and f.verifier_reason == UNVERIFIED_NIT


def select_for_verification(
    findings: list[Finding], cap: int | None
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """(to verify, passthrough, over cap). Tool findings pass through as tool-verified;
    trivial and nitpick-lane findings pass through as unverified nits."""
    passthrough: list[Finding] = []
    candidates: list[Finding] = []
    for f in findings:
        if f.verdict == Verdict.SKIPPED or f.source in TOOL_SOURCES:
            if f.verdict is None:
                f.verdict = Verdict.SKIPPED
            passthrough.append(f)
        elif is_nit(f):
            f.verdict = Verdict.SKIPPED
            f.verifier_reason = UNVERIFIED_NIT
            passthrough.append(f)
        else:
            candidates.append(f)
    candidates.sort(
        key=lambda f: (-severity_rank(f.severity), f.lane != Lane.inline, f.file, f.start_line)
    )
    if cap is None:
        return candidates, passthrough, []
    cap = max(0, cap)
    return candidates[:cap], passthrough, candidates[cap:]


def verifier_model(finding: Finding, config: Config) -> str:
    """Every vote runs on `verifier` (Sonnet); Opus only adjudicates escalated splits."""
    return config.models.verifier


def verifier_votes(config: Config, finding: Finding | None = None) -> int:
    """Most votes per finding: 1 in quick mode, else `verifier.votes` (default 3). Two run
    first; the rest only when those two disagree (see run_verifier)."""
    return votes_per_candidate(config, config.mode)


class VerdictCache:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self.path = self.repo_root / ".preflight" / "cache" / "verdicts.json"
        self._data: dict[str, dict] | None = None
        # Shared by the per-packet verifiers that run in parallel.
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict]:
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> dict[str, dict]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text())
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def key(self, finding: Finding, salt: str = "") -> str:
        """`salt` scopes the entry to a verification setup (vote count, model)."""
        try:
            content = hashlib.sha1((self.repo_root / finding.file).read_bytes()).hexdigest()
        except OSError:
            content = ""
        return hashlib.sha1((content + (finding.stable_id or "") + salt).encode()).hexdigest()

    def get(self, finding: Finding, salt: str = "") -> dict | None:
        key = self.key(finding, salt)
        with self._lock:
            return self._load_locked().get(key)

    def put(self, finding: Finding, verdict: dict, salt: str = "") -> None:
        key = self.key(finding, salt)
        with self._lock:
            self._load_locked()[key] = verdict

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        data = self._load_locked()
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


@dataclass
class VerifyOutcome:
    findings: list[Finding] = field(default_factory=list)
    results: dict[str, ModelResult] = field(default_factory=dict)
    failures: int = 0
    cache_hits: int = 0
    adjudicated_by: dict[str, int] = field(default_factory=dict)  # model -> adjudications


@dataclass(frozen=True)
class Vote:
    verdict: Verdict
    evidence: list[Evidence]
    reason: str
    checks: list[str]
    severity_adjusted: Severity | None = None
    confidence_10: int | None = None


def score_vote(finding: Finding, s: dict) -> Vote:
    """Score one verifier or adjudicator result without touching the finding.

    CONFIRMED or REFUTED without quoted evidence counts as UNKNOWN; a security CONFIRMED
    needs confidence_10 >= 8. Raises KeyError/TypeError/ValueError on an unusable result.
    """
    verdict = Verdict(s["verdict"])
    evidence = [Evidence(**e) for e in s["evidence"]]
    reason = str(s["reason"])
    checks = [str(c) for c in s["checks_performed"]]
    if verdict in (Verdict.CONFIRMED, Verdict.REFUTED) and not evidence:
        verdict = Verdict.UNKNOWN
        reason = "no evidence quoted; " + reason
    if finding.category == Category.security:
        ruled = security_verdict_rule({**s, "verdict": verdict.value})
        if verdict == Verdict.CONFIRMED and ruled == Verdict.UNKNOWN:
            reason += " (confidence_10 < 8)"
        verdict = ruled
    adj = s.get("severity_adjusted")
    conf = s.get("confidence_10")
    return Vote(
        verdict=verdict,
        evidence=evidence,
        reason=reason,
        checks=checks,
        severity_adjusted=Severity(adj) if adj else None,
        confidence_10=parse_conf10(conf),
    )


def vote_label(votes: list[Vote]) -> str:
    """`votes 2/3 CONFIRMED, 1 REFUTED`: counts over the votes that returned a result."""
    n = len(votes)
    c = sum(1 for v in votes if v.verdict == Verdict.CONFIRMED)
    r = sum(1 for v in votes if v.verdict == Verdict.REFUTED)
    u = n - c - r
    parts = [f"{c}/{n} CONFIRMED"] + ([f"{r} REFUTED"] if r else []) + (
        [f"{u} UNKNOWN"] if u else []
    )
    return "votes " + ", ".join(parts)


def tally(votes: list[Vote]) -> tuple[Verdict | None, str]:
    """Aggregate scored votes by majority. Returns (verdict, label); verdict None means no
    verdict has more than half the votes (a split)."""
    n = len(votes)
    label = vote_label(votes)
    for verdict in (Verdict.CONFIRMED, Verdict.REFUTED, Verdict.UNKNOWN):
        if sum(1 for v in votes if v.verdict == verdict) * 2 > n:
            return verdict, label
    return None, label


def unanimous(votes: list[Vote]) -> bool:
    return len({v.verdict for v in votes}) == 1


def escalates(finding: Finding) -> bool:
    """A split on this finding goes to the adjudicator (Opus): critical severity, or a
    security finding at major or above. Any other split is left unverified."""
    if finding.severity == Severity.critical:
        return True
    return finding.category == Category.security and (
        severity_rank(finding.severity) >= severity_rank(Severity.major)
    )


def needs_adjudication(finding: Finding, votes: list[Vote]) -> bool:
    """No majority on an escalating finding, or any disagreement on a critical one."""
    if len(votes) < 2 or not escalates(finding):
        return False
    verdict, _ = tally(votes)
    return verdict is None or (finding.severity == Severity.critical and not unanimous(votes))


def aggregate(finding: Finding, verdict: Verdict, votes: list[Vote], reason: str) -> dict:
    """One verifier.json-shaped result from the votes that back `verdict` (all votes when
    none do). It is what the finding is updated from and what the verdict cache stores.

    Severity is lowered only when every backing vote lowered it, and then by the least.
    """
    backing = [v for v in votes if v.verdict == verdict] or votes
    evidence: list[dict] = []
    seen: set[tuple] = set()
    checks: list[str] = []
    for v in backing:
        for e in v.evidence:
            k = (e.file, e.line, e.quote)
            if k not in seen:
                seen.add(k)
                evidence.append({"file": e.file, "line": e.line, "quote": e.quote})
        for c in v.checks:
            if c not in checks:
                checks.append(c)
    adjs = [v.severity_adjusted for v in backing]
    adj = (
        max(adjs, key=severity_rank).value
        if verdict == Verdict.CONFIRMED and adjs and all(a is not None for a in adjs)
        else None
    )
    out = {
        "verdict": verdict.value,
        "evidence": evidence,
        "reason": reason,
        "checks_performed": checks,
        "severity_adjusted": adj,
    }
    if finding.category == Category.security:
        out["confidence_10"] = max((v.confidence_10 or 0 for v in backing), default=0)
    return out


def _commit(f: Finding, vote: Vote) -> None:
    f.verdict = vote.verdict
    f.verifier_reason = vote.reason
    f.verifier_checks = list(vote.checks)
    f.verifier_evidence = list(vote.evidence)
    if vote.severity_adjusted and severity_rank(vote.severity_adjusted) < severity_rank(f.severity):
        f.severity = vote.severity_adjusted


def _apply(f: Finding, s: dict) -> bool:
    """Apply one verifier structured result (or a cached aggregate) to the finding."""
    _commit(f, score_vote(f, s))
    return True


def _vote_dict(i: int, v: Vote) -> dict:
    return {
        "vote": i,
        "verdict": v.verdict.value,
        "reason": v.reason,
        "evidence": [{"file": e.file, "line": e.line, "quote": e.quote} for e in v.evidence],
        "checks_performed": list(v.checks),
    }


VERIFIER_MIN_BUDGET_USD = 0.75
VERIFIER_MAX_TURNS = 6
VERIFIER_MAX_TURNS_DEEP = 10


def run_verifier(
    findings: list[Finding],
    packet: Packet | None,
    config: Config,
    backend,
    scope: Scope,
    cache: VerdictCache,
    system_prompt: str,
    packet_for: Callable[[Finding], Packet | None] | None = None,
) -> VerifyOutcome:
    selected, passthrough, over_cap = select_for_verification(findings, config.verifier.cap)
    out = VerifyOutcome()
    for f in over_cap:
        f.verdict = Verdict.UNKNOWN
        f.verifier_reason = "not verified: over verifier cap"

    votes_of = {id(f): verifier_votes(config, f) for f in selected}  # before any demotion
    # Per-call safety cap, not the run budget: an Opus verifier legitimately spends ~$0.25-0.45
    # (cache write dominates), so an even split of a small budget would abort real verifications.
    budget = split_call_cap(config, sum(votes_of.values()), VERIFIER_MIN_BUDGET_USD)
    turns = VERIFIER_MAX_TURNS_DEEP if config.mode == "deep" else VERIFIER_MAX_TURNS

    def adjudicator_of(f: Finding) -> str:
        return config.models.adjudicator

    def salt(f: Finding) -> str:
        # The adjudicator is part of the key: a split verdict settled by one adjudicator is
        # not replayed after models.adjudicator changes (the fable-vs-opus comparison).
        return (
            f"|votes={votes_of[id(f)]}|model={verifier_model(f, config)}|adj={adjudicator_of(f)}"
        )

    to_call: list[Finding] = []
    for f in selected:
        hit = cache.get(f, salt(f))
        if hit is not None:
            try:
                _apply(f, hit)
                out.cache_hits += 1
                continue
            except (KeyError, TypeError, ValueError):
                pass
        to_call.append(f)

    def pk_of(f: Finding) -> Packet | None:
        return packet_for(f) if packet_for else packet

    to_call.sort(key=lambda f: getattr(pk_of(f), "sha", ""))  # stable: same-packet adjacent

    def brief(f: Finding) -> str:
        if f.category == Category.security:
            return security_verifier_prompt(f)
        text = prompts.render("verifier", finding_json=json.dumps(to_dict(f), indent=2))
        spec = source_lens(f.source)
        if spec is not None and spec.verifier_addendum:  # lens-keyed verification rules
            text = text.rstrip("\n") + "\n\n" + prompts.render(spec.verifier_addendum)
        return text

    def request(f: Finding, role: str, model: str, prompt: str) -> ModelRequest:
        security = f.category == Category.security
        return ModelRequest(
            role=role + (":security" if security else ""),
            model=model,
            system_prompt=packet_system_prompt(system_prompt, pk_of(f)),
            prompt=prompt,
            schema=load_schema("verifier_security" if security else "verifier"),
            cwd=scope.repo_root,
            max_turns=turns,
            max_budget_usd=budget,
            effort=stage_effort(config, model),
        )

    def safe_run(req: ModelRequest) -> ModelResult:
        try:
            return backend.run(req)
        except Exception as exc:
            return ModelResult(
                ok=False, structured=None, subtype="exception", error=str(exc), model=req.model
            )

    def vote_call(f: Finding) -> ModelResult:
        return safe_run(request(f, "verifier", verifier_model(f, config), brief(f)))

    def adjudicate(f: Finding, votes: list[Vote]):
        """Run the adjudication; on failure re-run it on the fallback model.

        Returns (attempts [(key, ModelResult)], Vote | None, model that answered, first model).
        """
        first = adjudicator_of(f)
        models = [first]
        if config.models.adjudicator_fallback != first:
            models.append(config.models.adjudicator_fallback)
        prompt = prompts.render(
            "adjudicator",
            verifier_brief=brief(f),
            votes_json=json.dumps([_vote_dict(i + 1, v) for i, v in enumerate(votes)], indent=2),
        )
        attempts: list[tuple[str, ModelResult]] = []
        for i, model in enumerate(models):
            res = safe_run(request(f, "adjudicator", model, prompt))
            key = f"adjudicator:{f.stable_id}" + ("" if i == 0 else "#fallback")
            if res.ok and isinstance(res.structured, dict):
                try:
                    vote = score_vote(f, res.structured)
                except (KeyError, TypeError, ValueError) as exc:
                    res = ModelResult(
                        ok=False, structured=None, subtype="bad_structured", error=str(exc),
                        cost_usd=res.cost_usd, model=res.model,
                    )
                else:
                    attempts.append((key, res))
                    return attempts, vote, model, first
            attempts.append((key, res))
        return attempts, None, None, first

    vmodel = config.models.verifier

    def collect(f: Finding, futs: list, first: int, votes: list[Vote], errors: list[str],
                n_votes: int) -> None:
        for i, fut in enumerate(futs, start=first):
            res = fut.result()
            key = f"verifier:{f.stable_id}" + (f"#v{i + 1}" if n_votes > 1 else "")
            if res.ok and isinstance(res.structured, dict):
                try:
                    votes.append(score_vote(f, res.structured))
                except (KeyError, TypeError, ValueError) as exc:
                    res = ModelResult(
                        ok=False, structured=None, subtype="bad_structured",
                        error=str(exc), cost_usd=res.cost_usd, model=res.model,
                    )
            if not res.ok:
                errors.append(res.error or res.subtype)
            if is_paused(res):
                paused.add(id(f))
            out.results[key] = res

    # Votes: two in parallel first (one when only one is configured). When they agree, that
    # is the verdict; otherwise the remaining votes run and the majority decides.
    state: dict[int, tuple[list[Vote], list[str], int]] = {}  # id -> (votes, errors, asked)
    # Findings with a vote the usage-limit breaker skipped: no further vote is asked for (it
    # would be skipped too, and only inflate the run's count of calls asked for).
    paused: set[int] = set()
    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        first = [
            (f, [pool.submit(vote_call, f) for _ in range(min(2, votes_of[id(f)]))])
            for f in to_call
        ]
        for f, futs in first:
            votes: list[Vote] = []
            errors: list[str] = []
            collect(f, futs, 0, votes, errors, votes_of[id(f)])
            state[id(f)] = (votes, errors, len(futs))
        more = []
        for f in to_call:
            votes, errors, asked = state[id(f)]
            n_votes = votes_of[id(f)]
            if id(f) in paused:
                continue
            if asked < n_votes and not (len(votes) == asked and unanimous(votes)):
                more.append(
                    (f, [pool.submit(vote_call, f) for _ in range(n_votes - asked)], asked)
                )
        for f, futs, asked in more:
            votes, errors, _ = state[id(f)]
            collect(f, futs, asked, votes, errors, votes_of[id(f)])
            state[id(f)] = (votes, errors, asked + len(futs))

    splits: list[tuple[Finding, list[Vote], str, int]] = []
    for f in to_call:
        votes, errors, asked = state[id(f)]
        if not votes:  # every vote failed
            f.verdict = Verdict.UNKNOWN
            f.verifier_reason = f"verifier failed (verifier_failed=True): {errors[-1]}"
            f.severity = _DEMOTE[f.severity]
            out.failures += 1
            continue
        verdict, label = tally(votes)
        label += f" ({vmodel})"
        if errors:
            label += f" ({len(errors)} of {asked} votes failed)"
        if needs_adjudication(f, votes):
            splits.append((f, votes, label, len(errors)))
            continue
        if verdict is None:
            # No majority and not critical: left unverified, no adjudicator call.
            reason = f"{label}: no majority; not escalated (only critical or major+ security is)"
            agg = aggregate(f, Verdict.UNKNOWN, votes, reason)
            _apply(f, agg)
            if not errors:
                cache.put(f, agg, salt(f))
            continue
        rep_vote = next(v for v in votes if v.verdict == verdict)
        reason = rep_vote.reason if asked == 1 else f"{label}: {rep_vote.reason}"
        agg = aggregate(f, verdict, votes, reason)
        _apply(f, agg)
        if not errors:
            cache.put(f, agg, salt(f))

    if splits:
        with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
            adj_futures = [
                (f, votes, label, n_err, pool.submit(adjudicate, f, votes))
                for f, votes, label, n_err in splits
            ]
            for f, votes, label, n_err, fut in adj_futures:
                attempts, vote, model, first = fut.result()
                out.results.update(attempts)
                if vote is None:
                    last = attempts[-1][1]
                    reason = (
                        f"{label}; adjudication failed (adjudicator_failed=True): "
                        f"{last.error or last.subtype}"
                    )
                    _apply(f, aggregate(f, Verdict.UNKNOWN, votes, reason))
                    out.failures += 1
                    continue
                out.adjudicated_by[model] = out.adjudicated_by.get(model, 0) + 1
                by = model if model == first else f"{model} (fallback from {first})"
                reason = f"{label}; adjudicated by {by}: {vote.verdict.value} — {vote.reason}"
                agg = aggregate(f, vote.verdict, [vote], reason)
                _apply(f, agg)
                if not n_err and model == first:  # a fallback verdict is never cached
                    cache.put(f, agg, salt(f))

    cache.save()
    out.findings = list(findings)
    return out
