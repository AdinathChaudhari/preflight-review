"""Finding data contract shared by every pipeline stage."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

from preflight_review.errors import SchemaError


class Severity(str, Enum):
    critical = "critical"
    major = "major"
    minor = "minor"
    trivial = "trivial"
    info = "info"


class Lane(str, Enum):
    inline = "inline"
    outside_diff = "outside_diff"
    nitpick = "nitpick"
    pre_existing = "pre_existing"


class Category(str, Enum):
    correctness = "correctness"
    contracts = "contracts"
    data_integrity = "data_integrity"
    concurrency = "concurrency"
    error_handling = "error_handling"
    security = "security"
    rules = "rules"
    domain = "domain"
    maintainability = "maintainability"
    secrets = "secrets"
    dependency = "dependency"


class Effort(str, Enum):
    quick_win = "quick_win"
    heavy_lift = "heavy_lift"
    low_value = "low_value"


class Verdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"  # gitleaks/osv findings bypass the verifier


@dataclass
class Evidence:
    file: str
    line: int
    quote: str


@dataclass
class Finding:
    file: str
    start_line: int
    end_line: int
    title: str
    claim: str
    severity: Severity
    category: Category
    lane: Lane = Lane.inline
    source: str = "llm"
    rule_id: str | None = None
    caused_by: str | None = None
    effort: Effort = Effort.quick_win
    failure_scenario: str = ""
    why_it_matters: str = ""
    suggested_fix: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    bug_class_instances: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    confidence: float | None = None  # security lane, 0-1
    finder_confidence: int | None = None  # finder lenses, 1-5, logging only
    verdict: Verdict | None = None
    verifier_reason: str = ""
    verifier_checks: list[str] = field(default_factory=list)
    verifier_evidence: list[Evidence] = field(default_factory=list)
    run_id: str | None = None
    stable_id: str | None = None
    round: int | None = None
    status: str = "new"
    response: str | None = None
    suppressed_by: str | None = None
    cluster_members: list[str] = field(default_factory=list)


_SEVERITY_RANK = {"critical": 4, "major": 3, "minor": 2, "trivial": 1, "info": 0}


def severity_rank(sev: Severity | str) -> int:
    key = sev.value if isinstance(sev, Severity) else sev
    try:
        return _SEVERITY_RANK[key]
    except KeyError:
        raise SchemaError(f"unknown severity: {sev!r}") from None


def normalise_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def stable_id(finding: Finding, enclosing_symbol: str | None = None) -> str:
    raw = f"{finding.file}|{enclosing_symbol or ''}|{normalise_title(finding.title)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


_ENUM_FIELDS: dict[str, type[Enum]] = {
    "severity": Severity,
    "category": Category,
    "lane": Lane,
    "effort": Effort,
    "verdict": Verdict,
}
_EVIDENCE_LISTS = ("evidence", "verifier_evidence")


def to_dict(finding: Finding) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(Finding):
        v = getattr(finding, f.name)
        if isinstance(v, Enum):
            v = v.value
        elif f.name in _EVIDENCE_LISTS:
            v = [{"file": e.file, "line": e.line, "quote": e.quote} for e in v]
        elif isinstance(v, list):
            v = [dict(x) if isinstance(x, dict) else x for x in v]
        out[f.name] = v
    return out


def _evidence(items: Any, name: str) -> list[Evidence]:
    if not isinstance(items, list):
        raise SchemaError(f"{name} must be a list")
    result = []
    for item in items:
        if isinstance(item, Evidence):
            result.append(item)
            continue
        try:
            result.append(Evidence(**item))
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"bad {name} entry {item!r}: {exc}") from exc
    return result


def from_dict(d: dict[str, Any]) -> Finding:
    known = {f.name for f in fields(Finding)}
    unknown = set(d) - known
    if unknown:
        raise SchemaError(f"unknown finding keys: {sorted(unknown)}")
    kwargs = dict(d)
    for name, enum_cls in _ENUM_FIELDS.items():
        if kwargs.get(name) is not None:
            try:
                kwargs[name] = enum_cls(kwargs[name])
            except ValueError as exc:
                raise SchemaError(f"bad {name}: {kwargs[name]!r}") from exc
    for name in _EVIDENCE_LISTS:
        if name in kwargs:
            kwargs[name] = _evidence(kwargs[name], name)
    try:
        return Finding(**kwargs)
    except TypeError as exc:
        raise SchemaError(f"invalid finding: {exc}") from exc
