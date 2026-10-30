"""Verifier stage: one adversarial check per finding, with an on-disk verdict cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path

from preflight_review import prompts
from preflight_review.config import Config
from preflight_review.context.packer import Packet
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
from preflight_review.model.schemas import load_schema
from preflight_review.scope import Scope
from preflight_review.stages.finders import packet_system_prompt
from preflight_review.stages.security import security_verdict_rule, security_verifier_prompt

VERIFY_CATEGORIES = {
    Category.correctness,
    Category.contracts,
    Category.data_integrity,
    Category.concurrency,
    Category.error_handling,
    Category.security,
}
PASSTHROUGH_SOURCES = {"gitleaks", "osv"}
_DEMOTE = {
    Severity.critical: Severity.major,
    Severity.major: Severity.minor,
    Severity.minor: Severity.trivial,
    Severity.trivial: Severity.trivial,
    Severity.info: Severity.info,
}


def select_for_verification(
    findings: list[Finding], cap: int
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    passthrough: list[Finding] = []
    candidates: list[Finding] = []
    for f in findings:
        if f.verdict == Verdict.SKIPPED or f.source in PASSTHROUGH_SOURCES:
            if f.verdict is None:
                f.verdict = Verdict.SKIPPED
            passthrough.append(f)
        else:
            candidates.append(f)
    candidates.sort(
        key=lambda f: (-severity_rank(f.severity), f.lane != Lane.inline, f.file, f.start_line)
    )
    cap = max(0, cap)
    return candidates[:cap], passthrough, candidates[cap:]


def verifier_model(finding: Finding, config: Config) -> str:
    if config.mode == "quick":
        return config.models.verifier
    if severity_rank(finding.severity) >= severity_rank(Severity.major) and (
        finding.category in VERIFY_CATEGORIES
    ):
        return config.models.verifier_major
    return config.models.verifier


class VerdictCache:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self.path = self.repo_root / ".preflight" / "cache" / "verdicts.json"
        self._data: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text())
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def key(self, finding: Finding) -> str:
        try:
            content = hashlib.sha1((self.repo_root / finding.file).read_bytes()).hexdigest()
        except OSError:
            content = ""
        return hashlib.sha1((content + (finding.stable_id or "")).encode()).hexdigest()

    def get(self, finding: Finding) -> dict | None:
        return self._load().get(self.key(finding))

    def put(self, finding: Finding, verdict: dict) -> None:
        self._load()[self.key(finding)] = verdict

    def save(self) -> None:
        data = self._load()
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


def _apply(f: Finding, s: dict) -> bool:
    """Apply a verifier structured result; return False if it was unusable."""
    verdict = Verdict(s["verdict"])
    evidence = [Evidence(**e) for e in s["evidence"]]
    reason = s["reason"]
    if verdict in (Verdict.CONFIRMED, Verdict.REFUTED) and not evidence:
        verdict = Verdict.UNKNOWN
        reason = "no evidence quoted; " + reason
    if f.category == Category.security:
        ruled = security_verdict_rule({**s, "verdict": verdict.value})
        if verdict == Verdict.CONFIRMED and ruled == Verdict.UNKNOWN:
            reason += " (confidence_10 < 8)"
        verdict = ruled
    f.verdict = verdict
    f.verifier_reason = reason
    f.verifier_checks = list(s["checks_performed"])
    f.verifier_evidence = evidence
    adj = s.get("severity_adjusted")
    if adj:
        new = Severity(adj)
        if severity_rank(new) < severity_rank(f.severity):
            f.severity = new
    return True


VERIFIER_MIN_BUDGET_USD = 0.75


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

    # Per-call safety cap, not the run budget: an Opus verifier legitimately spends ~$0.25-0.45
    # (cache write dominates), so an even split of a small budget would abort real verifications.
    budget = min(config.budget_usd, max(config.budget_usd / (len(selected) + 2), VERIFIER_MIN_BUDGET_USD))
    to_call: list[Finding] = []
    for f in selected:
        hit = cache.get(f)
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

    def call(f: Finding) -> ModelResult:
        security = f.category == Category.security
        name = "verifier_security" if security else "verifier"
        if security:
            prompt = security_verifier_prompt(f)
        else:
            prompt = prompts.render(name, finding_json=json.dumps(to_dict(f), indent=2))
        req = ModelRequest(
            role="verifier:security" if security else "verifier",
            model=verifier_model(f, config),
            system_prompt=packet_system_prompt(system_prompt, pk_of(f)),
            prompt=prompt,
            schema=load_schema(name),
            cwd=scope.repo_root,
            max_turns=6,
            max_budget_usd=budget,
        )
        return backend.run(req)

    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        futures = [(f, pool.submit(call, f)) for f in to_call]
        for f, fut in futures:
            try:
                res = fut.result()
            except Exception as exc:
                res = ModelResult(ok=False, structured=None, subtype="exception", error=str(exc))
            out.results[f"verifier:{f.stable_id}"] = res
            applied = False
            if res.ok and isinstance(res.structured, dict):
                try:
                    _apply(f, res.structured)
                    cache.put(f, res.structured)
                    applied = True
                except (KeyError, TypeError, ValueError) as exc:
                    res = ModelResult(
                        ok=False, structured=None, subtype="bad_structured", error=str(exc)
                    )
            if not applied:
                f.verdict = Verdict.UNKNOWN
                f.verifier_reason = (
                    f"verifier failed (verifier_failed=True): {res.error or res.subtype}"
                )
                f.severity = _DEMOTE[f.severity]
                out.failures += 1

    cache.save()
    out.findings = list(findings)
    return out
