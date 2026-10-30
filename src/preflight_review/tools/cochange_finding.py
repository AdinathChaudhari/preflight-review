"""Co-change companion findings: files that usually change together but are absent from the diff."""

from __future__ import annotations

from preflight_review import findings as findings_mod
from preflight_review.context.cochange import Companion
from preflight_review.findings import Category, Finding, Lane, Severity, Verdict
from preflight_review.scope import Scope, glob_to_regex

MIN_CONFIDENCE = 0.6
MIN_SUPPORT = 3

# Companion classes: tests and migrations always; docs only when the changed file is app code.
COMPANION_CLASS_PATTERNS: dict[str, list[str]] = {
    "any": ["tests/**", "**/test_*.py", "**/migrations/**"],
    "app_only": ["**/*.md"],
}
_APP_GLOB = "app/**"

_ANY_RES = [glob_to_regex(g) for g in COMPANION_CLASS_PATTERNS["any"]]
_APP_ONLY_RES = [glob_to_regex(g) for g in COMPANION_CLASS_PATTERNS["app_only"]]
_APP_RE = glob_to_regex(_APP_GLOB)


def _is_companion_class(changed: str, companion: str) -> bool:
    if any(r.search(companion) for r in _ANY_RES):
        return True
    return bool(_APP_RE.search(changed)) and any(r.search(companion) for r in _APP_ONLY_RES)


def companion_findings(companions: list[Companion], scope: Scope) -> list[Finding]:
    in_diff = {f.path for f in scope.files}
    out: list[Finding] = []
    seen: set[str] = set()
    for c in companions:
        if c.confidence < MIN_CONFIDENCE or c.support < MIN_SUPPORT:
            continue
        if c.companion in in_diff or c.companion in seen:
            continue
        if not _is_companion_class(c.changed, c.companion):
            continue
        seen.add(c.companion)
        sentence = (
            f"{c.companion} usually changes with {c.changed} "
            f"({c.support}/{c.total} commits) but is not in this diff"
        )
        f = Finding(
            file=c.changed,
            start_line=1,
            end_line=1,
            title=sentence,
            claim=sentence,
            severity=Severity.trivial,
            category=Category.maintainability,
            lane=Lane.nitpick,
            source="cochange",
            verdict=Verdict.SKIPPED,
        )
        f.stable_id = findings_mod.stable_id(f)
        out.append(f)
    return out
