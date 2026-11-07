"""Usage-limit circuit breaker and the resume cache of successful model calls.

When the Claude account hits its usage limit every later call fails instantly. The breaker
trips on the first usage-limited result and answers every not-yet-started call with a
"paused" result, without running a subprocess. A paused run saves its successful calls so
`review --resume` replays them and only calls the model for what is missing.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from preflight_review.model.backend import ModelRequest, ModelResult

PAUSED_SUBTYPE = "paused_usage_limit"
PAUSED_ERROR = "paused: usage limit"
CALLS_FILE = "calls.json"

# "You've hit your 5-hour limit", "usage limit reached", "rate_limit_error",
# "rate limit exceeded", "HTTP 429" / "status: 429" / "API Error: 429" / "429 Too Many
# Requests". overloaded_error (529) is transient: it is not a usage limit and keeps the
# normal retry. Anchored tightly: these only ever see CLI/API error text, but a stray
# "rate limit" or "line 429" must still not pause a run.
_LIMIT_RES = [
    re.compile(r"\bhit your\b[^\n]{0,40}\blimit\b", re.IGNORECASE),
    re.compile(r"\busage[ _-]limit", re.IGNORECASE),
    re.compile(r"\brate_limit_error\b", re.IGNORECASE),
    re.compile(r"\brate[ _-]limit(?:ed)?\b[ _-]?(?:exceeded|reached|hit)\b", re.IGNORECASE),
    re.compile(r"\b(?:http|status|code|api error)[\s:=\"']{0,4}429\b", re.IGNORECASE),
    re.compile(r"\b429\s+too many requests\b", re.IGNORECASE),
]
_OVERLOADED = re.compile(r"overloaded", re.IGNORECASE)
_RESET = re.compile(r"\bresets?\s+(?:at\s+)?([^\n·|]+)", re.IGNORECASE)


@dataclass(frozen=True)
class UsageLimit:
    message: str
    resets: str | None = None


def classify_limit(text: str | None) -> UsageLimit | None:
    """A UsageLimit when `text` is a usage/rate-limit message, else None."""
    if not text:
        return None
    if _OVERLOADED.search(text) and not re.search(r"\bhit your\b|\busage[ _-]limit", text, re.IGNORECASE):
        return None
    if not any(p.search(text) for p in _LIMIT_RES):
        return None
    m = _RESET.search(text)
    resets = m.group(1).strip().rstrip(".").strip() if m else None
    return UsageLimit(message=text.strip()[:200], resets=resets or None)


def result_limit(res: ModelResult) -> UsageLimit | None:
    """Usage limit carried by a failed result (error text or the CLI's result text).

    A result the model finished on its own (CLI subtype "success" without is_error, e.g.
    error_no_structured_output) carries the model's prose, not an error: never classified,
    so a review of rate-limit code cannot trip the breaker.
    """
    if res.ok:
        return None
    raw = res.raw if isinstance(res.raw, dict) else {}
    if raw.get("api_error_status") == 429:
        return classify_limit(str(raw.get("result") or "HTTP 429")) or UsageLimit("HTTP 429")
    if raw.get("subtype") == "success" and not raw.get("is_error"):
        return None
    for text in (res.error, raw.get("result"), raw.get("error")):
        hit = classify_limit(text if isinstance(text, str) else None)
        if hit is not None:
            return hit
    return None


def call_key(req: ModelRequest) -> str:
    """Cache key: lane (role), model, and a hash of the packet-bearing prompts and schema."""
    blob = json.dumps(
        [req.role, req.model, req.system_prompt, req.prompt, req.schema, req.max_turns, req.effort],
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def paused_result(req: ModelRequest) -> ModelResult:
    return ModelResult(
        ok=False, structured=None, subtype=PAUSED_SUBTYPE, error=PAUSED_ERROR, model=req.model,
    )


def is_paused(res: ModelResult | None) -> bool:
    return res is not None and res.subtype == PAUSED_SUBTYPE


class BreakerBackend:
    """Wraps a backend with the usage-limit breaker and the resume cache.

    In-flight calls finish normally; calls started after the trip are skipped. Thread-safe:
    stages call `run` from thread pools.
    """

    def __init__(self, inner, cached: dict[str, list[dict]] | None = None) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "")
        self._lock = threading.Lock()
        self._cached = {k: list(v) for k, v in (cached or {}).items()}
        self.limit: UsageLimit | None = None
        self.done = 0  # calls that reached the model
        self.skipped = 0  # calls answered "paused" without a model call
        self.reused = 0  # calls answered from the resume cache
        self.successes: list[dict] = []

    @property
    def tripped(self) -> bool:
        return self.limit is not None

    def run(self, req: ModelRequest) -> ModelResult:
        key = call_key(req)
        with self._lock:
            entries = self._cached.get(key)
            if entries:
                entry = entries.pop(0)
                self.reused += 1
                self.successes.append(entry)
                return ModelResult(
                    ok=True, structured=entry["structured"], subtype="success",
                    model=entry.get("model") or req.model, raw={"resumed": True},
                    num_turns=int(entry.get("num_turns") or 0),
                )
            if self.limit is not None:
                self.skipped += 1
                return paused_result(req)
        res = self.inner.run(req)
        hit = result_limit(res)
        with self._lock:
            self.done += 1
            if hit is not None:
                if self.limit is None:
                    self.limit = hit
                res = replace(res, subtype=PAUSED_SUBTYPE, error=f"{PAUSED_ERROR}: {hit.message}")
            elif res.ok and isinstance(res.structured, dict):
                self.successes.append(
                    {"key": key, "role": req.role, "model": res.model or req.model,
                     "structured": res.structured, "num_turns": res.num_turns}
                )
        return res

    def merge_saved(self, saved: dict[str, list[dict]]) -> None:
        """Add saved calls of an earlier paused run (same diff) that this run did not make."""
        have = Counter(e["key"] for e in self.successes)
        for key, entries in saved.items():
            self.successes.extend(entries[have[key]:])

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / CALLS_FILE).write_text(
            json.dumps(self.successes, indent=2, sort_keys=True, default=str)
        )


def load_calls(run_dir: Path) -> dict[str, list[dict]]:
    """Saved successful calls of a paused run, keyed by call_key (in call order)."""
    try:
        data = json.loads((run_dir / CALLS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    if not isinstance(data, list):
        return out
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and isinstance(
            entry.get("structured"), dict
        ):
            out.setdefault(entry["key"], []).append(entry)
    return out
