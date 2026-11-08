"""Parallel packets under one in-flight cap: same report as the serial pipeline.

The baseline under tests/fixtures/parallel_baseline/ was rendered by the serial pipeline
(main @ 0f04348, before batches ran in parallel) from `parallel_repo` below. Regenerate it
only from a serial build: PREFLIGHT_REGEN_PARALLEL_BASELINE=1 pytest tests/test_parallel_batches.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from preflight_review import pipeline
from preflight_review.findings import Category, Finding, Lane, Severity
from preflight_review.model import FakeBackend, ModelRequest, ModelResult
from preflight_review.model.limits import BreakerBackend
from preflight_review.pipeline import ReviewOptions, run_review

from .conftest import branch, commit, make_repo

BASELINE = Path(__file__).parent / "fixtures" / "parallel_baseline"
REGEN = bool(os.environ.get("PREFLIGHT_REGEN_PARALLEL_BASELINE"))
MODS = [f"src/pkg/mod_{i}.py" for i in range(4)]
EMPTY_SEC = {"findings": [], "files_read_outside_packet": []}


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def _body(i: int, new: bool) -> str:
    rows = []
    for j in range(8):
        rows.append(f"def fn_{i}_{j}(value):")
        rows.append(f"    return value + {j}{'0' if new else ''}  # padding text for bulk {i}")
    if new:
        rows.append(f'BATCH_MARKER_{i} = "marker {i}"')
    return "\n".join(rows) + "\n"


def _config(concurrency: int, extra: str = "") -> str:
    return (
        "mode: standard\n"
        f"concurrency: {concurrency}\n"
        "cache_warm_seconds: 0\n"
        "walkthrough: false\n"
        "confirm_above_usd: null\n"
        "triage:\n  haiku: false\n"
        "learnings:\n  rerank: false\n"
    ) + extra


def tool_finding() -> Finding:
    """A deterministic tool hit in mod_1: passes through verification as tool-verified."""
    return Finding(
        file=MODS[1], start_line=9, end_line=9, title="F841 local variable assigned but never used",
        claim="ruff F841", severity=Severity.minor, category=Category.maintainability,
        lane=Lane.inline, source="ruff", rule_id="F841",
    )


def make_parallel_repo(tmp_path, monkeypatch, concurrency: int, extra: str = "") -> Path:
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 300)

    def fake_tools(scope, config, *a):
        return [tool_finding()], []

    monkeypatch.setattr(pipeline, "run_tools", fake_tools)
    base = {p: _body(i, False) for i, p in enumerate(MODS)}
    base[".preflight/config.yaml"] = _config(concurrency, extra)
    r = make_repo(tmp_path, base)
    branch(r, "feature/par")
    commit(r, {p: _body(i, True) for i, p in enumerate(MODS)}, "change every module")
    return r


def batch_of(req: ModelRequest) -> int | None:
    """Which module's packet this call carries (by the marker only that file's diff has)."""
    hits = [i for i in range(len(MODS)) if f"BATCH_MARKER_{i}" in req.system_prompt]
    return hits[0] if len(hits) == 1 else None


def _finding(i: int, lens: str, line: int) -> dict:
    return {
        "file": MODS[i], "start_line": line, "end_line": line, "caused_by": None,
        "lane": "inline", "category": "correctness", "severity": "major",
        "effort": "quick_win", "title": f"{lens} bug {i} at line {line}",
        "claim": f"fn_{i} returns the wrong value", "failure_scenario": "value=1 returns 11",
        "why_it_matters": "wrong totals", "suggested_fix": "return value + 1",
        "evidence": [{"file": MODS[i], "line": line, "quote": f"return value + {line}"}],
        "bug_class_instances": [], "rule_quote": None,
    }


def _coverage(i: int | None) -> list[dict]:
    f = MODS[i] if i is not None else MODS[0]
    return [{"file": f, "symbol_or_hunk": "changed hunks", "inputs_considered": "the diff",
             "result": "checked"}]


def respond(req: ModelRequest) -> dict:
    """Order-independent answers: keyed on role and on the packet/finding the call carries."""
    i = batch_of(req)
    role = req.role
    if role.startswith("finder:"):
        lens = role.split(":", 1)[1].split("#", 1)[0]
        findings = []
        if lens == "correctness" and i is not None:
            findings.append(_finding(i, lens, 2))
        if lens == "data_concurrency" and i in (0, 3):
            findings.append(_finding(i, lens, 12))
        return {"findings": findings, "files_read_outside_packet": [], "coverage": _coverage(i)}
    if role.startswith("security"):
        return dict(EMPTY_SEC)
    if role.startswith(("verifier", "adjudicator")):
        m = re.search(r'"title": "([^"]+)"', req.prompt)
        title = m.group(1) if m else ""
        m2 = re.search(r'"file": "([^"]+)"', req.prompt)
        file = m2.group(1) if m2 else MODS[0]
        refuted = title.startswith("correctness bug 2")
        return {
            "verdict": "REFUTED" if refuted else "CONFIRMED",
            "evidence": [{"file": file, "line": 2, "quote": "return value + 1"}],
            "reason": f"{'not reachable' if refuted else 'reproduced'}: {title}",
            "severity_adjusted": None, "confidence": 4,
            "checks_performed": [f"read {file}"],
        }
    return {"findings": [], "files_read_outside_packet": []}


def review(repo: Path, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


_VOLATILE = [
    (re.compile(r'"(duration_s|duration_ms|generated_at|timestamp|started_at|finished_at)": [^,\n]+'),
     r'"\1": "<X>"'),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?"), "<TIME>"),
    (re.compile(r"\b\d+(\.\d+)?\s?(s|min|ms)\b"), "<DUR>"),
]


def _shas(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "rev-list", "--all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.split()
    return out


def normalize(text: str, repo: Path) -> str:
    """Drop what differs between two runs of the same review: tmp paths, commit shas (the
    commit time is in them), timestamps and durations."""
    for p in sorted({str(repo), str(repo.resolve()), str(repo.parent), str(repo.parent.resolve())},
                    key=len, reverse=True):
        text = text.replace(p, "<TMP>")
    for sha in _shas(repo):
        text = text.replace(sha, "<SHA>").replace(sha[:7], "<SHA>")
    for rx, sub in _VOLATILE:
        text = rx.sub(sub, text)
    return text


# Lines and keys that other parts of the tool own and that do not depend on scheduling: the
# pre-run estimate (its minutes divide by concurrency), the prompt-set sha, and the
# estimate-vs-actual line. They are dropped before comparing.
_UNSCHEDULED_LINE = re.compile(r"estimate|Estimate|Prompts sha")
_UNSCHEDULED_KEYS = {
    "estimate", "prompt_shas", "prompts_sha", "schema_shas", "mode_estimates", "preflight_version",
}


def normalize_md(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _UNSCHEDULED_LINE.search(ln)) + "\n"


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _UNSCHEDULED_KEYS
                and not (isinstance(v, str) and _UNSCHEDULED_LINE.search(v) and k == "note")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj if not (isinstance(v, str) and "estimate" in v)]
    return obj


def normalize_json(text: str) -> dict:
    return _strip(json.loads(text))


def subset(base, got, path="$") -> list[str]:
    """Differences where `got` does not carry `base`: keys only in `got` are allowed (other
    parts of the tool add report fields); every value present in `base` must match."""
    if isinstance(base, dict) and isinstance(got, dict):
        out = []
        for k, v in base.items():
            if k not in got:
                out.append(f"{path}.{k}: missing")
            else:
                out += subset(v, got[k], f"{path}.{k}")
        return out
    if isinstance(base, list) and isinstance(got, list):
        if len(base) != len(got):
            return [f"{path}: {len(base)} items != {len(got)}"]
        return [d for i, (a, b) in enumerate(zip(base, got)) for d in subset(a, b, f"{path}[{i}]")]
    return [] if base == got else [f"{path}: {base!r} != {got!r}"]


def run_and_render(tmp_path, monkeypatch, concurrency: int) -> tuple[str, str, object, FakeBackend]:
    repo = make_parallel_repo(tmp_path, monkeypatch, concurrency)
    be = FakeBackend(default=respond)
    run, paths = review(repo, be)
    md = normalize(paths["md"].read_text(), repo)
    js = normalize(paths["json"].read_text(), repo)
    return md, js, run, be


# --- (2) byte-identical to the serial baseline --------------------------------------


def test_baseline_shape(tmp_path, monkeypatch):
    md, js, run, be = run_and_render(tmp_path, monkeypatch, 1)
    if REGEN:
        BASELINE.mkdir(parents=True, exist_ok=True)
        (BASELINE / "report.md").write_text(md)
        (BASELINE / "report.json").write_text(js)
    assert run.batches >= 3
    roles = {c.role for c in be.calls}
    assert "verifier" in roles
    assert run.outcome == "complete"
    finder_hits = [c for c in be.calls if c.role == "finder:correctness"]
    assert len(finder_hits) == run.batches


@pytest.mark.skipif(REGEN, reason="regenerating the baseline")
@pytest.mark.parametrize("concurrency", [1, 8])
def test_report_identical_to_serial_baseline(tmp_path, monkeypatch, concurrency):
    md, js, run, _ = run_and_render(tmp_path, monkeypatch, concurrency)
    assert run.batches >= 3
    base_md = normalize_md((BASELINE / "report.md").read_text())
    got_md = normalize_md(md)
    # Other report lines may be added around these; every baseline line stays, in order.
    got_lines = iter(got_md.splitlines())
    missing = [ln for ln in base_md.splitlines() if not any(ln == g for g in got_lines)]
    assert not missing, missing
    diffs = subset(normalize_json((BASELINE / "report.json").read_text()), normalize_json(js))
    assert not diffs, diffs


@pytest.mark.skipif(REGEN, reason="regenerating the baseline")
def test_concurrency_8_matches_concurrency_1(tmp_path, monkeypatch):
    a = run_and_render(tmp_path / "a", monkeypatch, 1)
    b = run_and_render(tmp_path / "b", monkeypatch, 8)
    assert normalize_md(a[0]) == normalize_md(b[0])
    assert normalize_json(a[1]) == normalize_json(b[1])
    assert sorted(c.role for c in a[3].calls) == sorted(c.role for c in b[3].calls)


# --- (1) one global cap on calls in flight -------------------------------------------


def test_gate_caps_calls_in_flight():
    lock = threading.Lock()
    full = threading.Event()
    state = {"active": 0, "max": 0}

    class Slow:
        name = "slow"

        def run(self, req):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
                if state["active"] == 3:
                    full.set()
            full.wait(5)  # the first three overlap for sure; the gate must stop a fourth
            time.sleep(0.005)
            with lock:
                state["active"] -= 1
            return ModelResult(ok=True, structured={"findings": []}, subtype="success")

    b = BreakerBackend(Slow(), max_inflight=3)
    reqs = [ModelRequest(role=f"r{i}", model="m", system_prompt="s", prompt=str(i), schema={},
                         cwd=Path(".")) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(b.run, reqs))
    assert full.is_set() and state["max"] == 3
    assert all(r.ok for r in results) and b.done == 12


def test_call_queued_at_gate_is_skipped_after_trip():
    """A call queued at the gate when the breaker trips is skipped, never started."""
    from preflight_review.errors import BackendError

    release = threading.Event()
    started: list[str] = []

    def slow(req):
        started.append(req.role)
        release.wait(5)
        return BackendError("You've hit your 5-hour limit · resets 4pm")

    inner = FakeBackend({"limit": slow}, default={"findings": []})
    b = BreakerBackend(inner, max_inflight=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(b.run, ModelRequest("limit", "m", "s", "p", {}, Path(".")))
        while not started:
            time.sleep(0.001)
        queued = pool.submit(b.run, ModelRequest("other", "m", "s", "p", {}, Path(".")))
        time.sleep(0.02)  # `queued` is waiting for the only slot
        release.set()
        assert first.result().subtype == "paused_usage_limit"
        assert queued.result().subtype == "paused_usage_limit"
    assert [c.role for c in inner.calls] == ["limit"] and (b.done, b.skipped) == (1, 1)


# --- (3) a batch's verification overlaps later batches ---------------------------------


def test_verify_starts_before_last_batch_finders_finish(tmp_path, monkeypatch):
    repo = make_parallel_repo(tmp_path, monkeypatch, 8)
    verifier_started = threading.Event()
    waited = {"ok": None}

    def overlapping(req):
        if req.role.startswith("verifier"):
            verifier_started.set()
        elif req.role == "finder:correctness" and batch_of(req) == 3:
            # Held until another batch's verifier call has started: a pipeline that verifies
            # only after every batch would never start one (the wait times out instead).
            waited["ok"] = verifier_started.wait(10)
        return respond(req)

    run, _ = review(repo, FakeBackend(default=overlapping))
    assert waited["ok"] is True
    assert run.outcome == "complete" and run.batches >= 3


# --- (4) a run budget keeps the serial, stop-early semantics ---------------------------


def test_budget_runs_batches_serially_and_marks_rest_missing(tmp_path, monkeypatch):
    repo = make_parallel_repo(tmp_path, monkeypatch, 8)
    spans: dict[int, list[float]] = {}
    lock = threading.Lock()

    def timed(req):
        t0 = time.monotonic()
        time.sleep(0.002)
        out = respond(req)
        i = batch_of(req)
        if req.role.startswith(("finder:", "security")) and i is not None:
            with lock:
                spans.setdefault(i, []).extend([t0, time.monotonic()])
        return out

    # Each batch's lanes cost USD 0.05 (5 calls at 0.01): two batches fit, a third does not.
    run, paths = review(repo, FakeBackend(default=timed), budget_usd=0.13)
    assert run.outcome == "incomplete"
    reasons = {p: r for p, r in run.not_reviewed}
    assert sum("run budget exhausted" in r for r in reasons.values()) == len(MODS) - 2
    assert len(spans) == 2
    (_a0, a1), (b0, _b1) = sorted((min(v), max(v)) for v in spans.values())
    assert a1 <= b0  # the second batch started after the first finished
    assert "run budget exhausted" in paths["md"].read_text()


# --- (5) cross-batch duplicates merge after verification ------------------------------


def _mk(file="a.py", s=10, e=10, sev=Severity.major, title="t", source="llm:correctness", **kw):
    return Finding(file=file, start_line=s, end_line=e, title=title, claim="c", severity=sev,
                   category=Category.correctness, source=source, **kw)


def test_merge_verified_keeps_confirmed_member_across_groups():
    from preflight_review.findings import Verdict
    from preflight_review.stages.cluster import merge_verified

    refuted = _mk(s=10, e=11, sev=Severity.critical, title="dup", verdict=Verdict.REFUTED,
                  verifier_reason="no", stable_id="aaa")
    confirmed = _mk(s=12, e=12, title="dup", verdict=Verdict.CONFIRMED, verifier_reason="yes",
                    stable_id="bbb")
    other = _mk(file="b.py", title="other", verdict=Verdict.CONFIRMED, stable_id="ccc")
    out = merge_verified([[refuted, other], [confirmed]])
    assert out == [confirmed, other]
    assert confirmed.verdict == Verdict.CONFIRMED and confirmed.verifier_reason == "yes"
    assert (confirmed.start_line, confirmed.end_line) == (10, 12)
    assert confirmed.cluster_members == ["aaa", "bbb"]
    assert confirmed.severity == Severity.major  # a refuted duplicate does not raise it


def test_merge_verified_same_group_is_a_noop():
    from preflight_review.stages.cluster import merge_verified

    a, b = _mk(s=10, title="x"), _mk(s=11, title="y")
    assert merge_verified([[a, b]]) == [a, b]
    assert a.cluster_members == [] and b.cluster_members == [] and a.start_line == 10


def test_merge_verified_appends_tool_evidence_and_unions_members():
    from preflight_review.findings import Verdict
    from preflight_review.stages.cluster import merge_verified

    llm = _mk(s=5, verdict=Verdict.CONFIRMED, stable_id="l1", cluster_members=["l1", "l0"])
    tool = _mk(s=6, source="ruff", rule_id="F841", title="unused", verdict=Verdict.SKIPPED,
               stable_id="t1")
    out = merge_verified([[llm], [tool]])
    assert out == [llm]
    assert llm.cluster_members == ["l1", "l0", "t1"]
    assert any("ruff F841: unused" in e.quote for e in llm.evidence)


# --- (6) transient 429 is retried; the usage limit is not ------------------------------


def _cli_req():
    return ModelRequest(role="finder:x", model="m", system_prompt="s", prompt="p", schema={},
                        cwd=Path("."))


def test_cli_backend_retries_transient_429_then_succeeds():
    from preflight_review.model.cli_backend import CliBackend

    outs = [
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "api_error_status": 429, "result": "API Error: 429 Too Many Requests",
         "total_cost_usd": 0.01},
        {"type": "result", "subtype": "success", "is_error": False,
         "structured_output": {"findings": []}, "total_cost_usd": 0.02},
    ]
    calls, sleeps = [], []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(outs.pop(0)), "")

    b = BreakerBackend(CliBackend(runner=runner, retries=0, sleep=sleeps.append))
    res = b.run(_cli_req())
    assert res.ok and not b.tripped
    assert len(calls) == 2 and sleeps == [20]
    assert abs(res.cost_usd - 0.03) < 1e-9


def test_cli_backend_transient_429_gives_up_after_two_waits_and_trips():
    from preflight_review.model.cli_backend import CliBackend

    calls, sleeps = [], []

    def runner(argv, **kw):
        calls.append(argv)
        out = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "result": '{"type":"error","error":{"type":"rate_limit_error"}}'}
        return subprocess.CompletedProcess(argv, 1, json.dumps(out), "")

    b = BreakerBackend(CliBackend(runner=runner, retries=1, sleep=sleeps.append))
    res = b.run(_cli_req())
    assert len(calls) == 3 and sleeps == [20, 60]
    assert b.tripped and res.subtype == "paused_usage_limit"


def test_cli_backend_usage_limit_not_retried_and_pauses():
    from preflight_review.model.cli_backend import CliBackend

    calls, sleeps = [], []

    def runner(argv, **kw):
        calls.append(argv)
        out = {"type": "result", "subtype": "success", "is_error": True,
               "result": "You've hit your 5-hour limit · resets 4:40pm"}
        return subprocess.CompletedProcess(argv, 1, json.dumps(out), "")

    b = BreakerBackend(CliBackend(runner=runner, retries=2, sleep=sleeps.append))
    res = b.run(_cli_req())
    assert len(calls) == 1 and sleeps == []
    assert b.tripped and res.subtype == "paused_usage_limit"
    assert b.limit.transient is False


def test_classify_transient_vs_usage_limit():
    from preflight_review.model.limits import classify_limit, result_limit

    assert classify_limit("API Error: 429 Too Many Requests").transient is True
    assert classify_limit("rate limit exceeded").transient is True
    assert classify_limit("You've hit your weekly limit").transient is False
    assert classify_limit("Claude AI usage limit reached|1700").transient is False
    res = ModelResult(ok=False, structured=None, subtype="error_during_execution",
                      raw={"api_error_status": 429, "result": "Claude AI usage limit reached"})
    assert result_limit(res).transient is False
    res = ModelResult(ok=False, structured=None, subtype="error_during_execution",
                      raw={"api_error_status": 429, "result": "slow down"})
    assert result_limit(res).transient is True


# --- a file split across packets: one finding, verified per packet, merged once --------


def _big(new: bool) -> str:
    out = []
    for i in range(300):
        if new and i % 12 == 0:
            out.append(f"changed_marker_{i:03d} = 'added line number {i} with some padding text'")
        else:
            out.append(f"value_{i:03d} = {i}  # untouched line with padding text")
    return "\n".join(out) + "\n"


def test_split_file_duplicate_is_verified_per_packet_and_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 1000)
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config, *a: ([], []))
    r = make_repo(tmp_path, {"big.py": _big(False), ".preflight/config.yaml": _config(8)})
    branch(r, "feature/big")
    commit(r, {"big.py": _big(True)}, "big change")
    dup = {
        "file": "big.py", "start_line": 1, "end_line": 1, "caused_by": None, "lane": "inline",
        "category": "correctness", "severity": "major", "effort": "quick_win",
        "title": "marker constant shadows value", "claim": "c", "failure_scenario": "f",
        "why_it_matters": "w", "suggested_fix": "s",
        "evidence": [{"file": "big.py", "line": 1, "quote": "changed_marker_000"}],
        "bug_class_instances": [], "rule_quote": None,
    }

    def answer(req):
        if req.role == "finder:correctness":
            return {"findings": [dup], "files_read_outside_packet": [],
                    "coverage": [{"file": "big.py", "symbol_or_hunk": "all",
                                  "inputs_considered": "-", "result": "checked"}]}
        return respond(req)

    run, _ = review(r, FakeBackend(default=answer))
    assert run.batches >= 2, (run.outcome, run.notes, run.incomplete_reasons, run.triaged_out)
    assert len(run.policy.report) == 1
    votes = [k for k in run.lane_results if k.startswith("verifier:")]
    # Each packet verifies its copy unless an earlier packet's verdict is already in the
    # verdict cache; every result is kept under its own key, so the cost counts it.
    assert 2 <= len(votes) <= 2 * run.batches and len(votes) % 2 == 0
    assert abs(run.cost_usd - sum(x.cost_usd for x in run.lane_results.values())) < 1e-9
    assert run.policy.report[0].verdict.value == "CONFIRMED"
