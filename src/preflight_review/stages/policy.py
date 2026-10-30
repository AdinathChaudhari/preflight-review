"""Report policy: bucket, filter by profile, cap nitpicks, order and number findings."""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight_review import findings as findings_mod
from preflight_review.config import Config
from preflight_review.findings import Finding, Lane, Severity, Verdict, severity_rank
from preflight_review.state import BranchState

ROUND_MESSAGE = "{n} review rounds on this branch: step back and reconsider the approach"
_PREVIOUS = {"open", "addressed", "dismissed"}


@dataclass
class PolicyResult:
    report: list[Finding] = field(default_factory=list)
    unverified: list[Finding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    pre_existing: list[Finding] = field(default_factory=list)
    previously_reported: list[tuple[str, str]] = field(default_factory=list)  # (stable_id, status)
    nit_capped: list[Finding] = field(default_factory=list)
    round_message: str | None = None
    previous_info: dict[str, tuple[str, str, int]] = field(default_factory=dict)  # sid -> (title, file, line)


def _is_nit(f: Finding) -> bool:
    return f.lane == Lane.nitpick or f.severity == Severity.trivial


def _sort_key(f: Finding) -> tuple[int, str, int]:
    return (-severity_rank(f.severity), f.file, f.start_line)


def apply_policy(
    findings: list[Finding], config: Config, branch_state: BranchState, round: int
) -> PolicyResult:
    res = PolicyResult()
    candidates: list[Finding] = []
    unverified: list[Finding] = []

    for f in findings:
        if f.lane == Lane.pre_existing:
            res.pre_existing.append(f)
            continue
        sid = f.stable_id or findings_mod.stable_id(f)
        st = branch_state.findings.get(sid)
        if st is not None and st.status in _PREVIOUS and st.last_seen_round < round:
            res.previously_reported.append((sid, st.status))
            res.previous_info[sid] = (f.title, f.file, f.start_line)
            continue
        if f.verdict == Verdict.REFUTED:
            res.dropped.append((f, f"refuted: {f.verifier_reason}"))
        elif f.verdict is None or f.verdict == Verdict.UNKNOWN:
            if severity_rank(f.severity) > severity_rank(Severity.minor):
                f.severity = Severity.minor
            unverified.append(f)
        else:
            candidates.append(f)

    # Nitpicks vanish after the first round.
    if round >= 2:
        def strip_nits(items: list[Finding]) -> list[Finding]:
            kept = []
            for f in items:
                if _is_nit(f):
                    res.dropped.append((f, "nitpicks suppressed after round 1"))
                else:
                    kept.append(f)
            return kept

        candidates = strip_nits(candidates)
        unverified = strip_nits(unverified)

    candidates.sort(key=_sort_key)
    unverified.sort(key=_sort_key)
    profile = config.profile

    def below(f: Finding) -> bool:
        rank = severity_rank(f.severity)
        if profile == "quiet":
            return rank < severity_rank(Severity.major)
        if profile == "chill":
            return rank < severity_rank(Severity.minor) and not (
                _is_nit(f) and f.severity != Severity.info
            )
        return False

    per_file: dict[str, int] = {}
    nits = 0
    for f in candidates:
        if below(f):
            res.dropped.append((f, f"below profile {profile}"))
            continue
        if profile == "chill" and _is_nit(f):
            if per_file.get(f.file, 0) >= config.nitpicks.per_file or nits >= config.nitpicks.per_run:
                res.nit_capped.append(f)
                continue
            per_file[f.file] = per_file.get(f.file, 0) + 1
            nits += 1
        res.report.append(f)

    for f in unverified:
        rank = severity_rank(f.severity)
        too_low = (profile == "quiet" and rank < severity_rank(Severity.major)) or (
            profile == "chill" and rank < severity_rank(Severity.minor)
        )
        if too_low:
            res.dropped.append((f, f"below profile {profile}"))
        else:
            res.unverified.append(f)

    if round > config.max_rounds:
        res.round_message = ROUND_MESSAGE.format(n=config.max_rounds)

    for i, f in enumerate(res.report + res.unverified, start=1):
        f.run_id = f"F{i}"
    return res
