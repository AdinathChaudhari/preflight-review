"""Usage-limit circuit breaker: detection, pause, and `review --resume`."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend, ModelRequest, ModelResult
from preflight_review.model.cli_backend import CliBackend
from preflight_review.model.limits import (
    PAUSED_SUBTYPE,
    BreakerBackend,
    classify_limit,
    load_calls,
    result_limit,
)
from preflight_review.pipeline import ReviewOptions, run_review, summary_line
from preflight_review.state import load_state

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE, no_gh

LIMIT_TEXT = "You've hit your 5-hour limit · resets 4:40pm (Asia/Calcutta)"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fake" / "run1.json").read_text())


@pytest.mark.parametrize("text,resets", [
    (LIMIT_TEXT, "4:40pm (Asia/Calcutta)"),
    ("Claude AI usage limit reached|1700000000", None),
    ("API Error: 429 Too Many Requests", None),
    ('{"type":"error","error":{"type":"rate_limit_error"}}', None),
    ("Error: HTTP 429 from api", None),
    ("You've hit your weekly limit. Resets at Nov 9, 9am.", "Nov 9, 9am"),
])
def test_classify_usage_limit(text, resets):
    hit = classify_limit(text)
    assert hit is not None and hit.resets == resets


@pytest.mark.parametrize("text", [
    "API Error: 529 overloaded_error",
    '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    "error_max_turns",
    "line 429 has a bug",
    "Found an error at line 429 of handler.py",
    "The rate limit middleware in api/throttle.py skips admin tokens; no findings.",
    None,
    "",
])
def test_classify_not_usage_limit(text):
    assert classify_limit(text) is None


def _req(role="finder:x", prompt="p") -> ModelRequest:
    return ModelRequest(role=role, model="m", system_prompt="s", prompt=prompt, schema={}, cwd=Path("."))


def test_cli_backend_does_not_retry_usage_limit():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        out = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": LIMIT_TEXT})
        return subprocess.CompletedProcess(argv, 1, out, "")

    res = CliBackend(runner=runner, retries=2).run(_req())
    assert not res.ok and len(calls) == 1


def test_model_prose_about_limits_does_not_trip_breaker():
    """A finished answer with no structured output carries the model's prose, not an error."""
    prose = "You've hit your usage limit check in throttle.py: rate limit exceeded at status 429."

    def runner(argv, **kw):
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": prose})
        return subprocess.CompletedProcess(argv, 0, out, "")

    res = CliBackend(runner=runner, retries=0).run(_req())
    assert res.subtype == "error_no_structured_output" and result_limit(res) is None
    b = BreakerBackend(CliBackend(runner=runner, retries=0))
    b.run(_req())
    assert not b.tripped


def test_replay_restores_num_turns():
    inner = FakeBackend(default=lambda req: {"findings": []})
    first = BreakerBackend(inner)
    res = first.run(_req())
    assert res.num_turns == 1
    cached: dict[str, list[dict]] = {}
    for e in first.successes:
        cached.setdefault(e["key"], []).append(e)
    replay = BreakerBackend(inner, cached).run(_req())
    assert replay.raw == {"resumed": True} and replay.num_turns == 1


def test_cli_backend_still_retries_overloaded():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        out = json.dumps({"type": "result", "subtype": "error_during_execution",
                          "result": "API Error: 529 overloaded_error"})
        return subprocess.CompletedProcess(argv, 1, out, "")

    CliBackend(runner=runner, retries=1).run(_req())
    assert len(calls) == 2


def test_breaker_skips_calls_after_trip():
    inner = FakeBackend({"finder:x": [BackendError(LIMIT_TEXT)]}, default={"findings": []})
    b = BreakerBackend(inner)
    first = b.run(_req())
    assert first.subtype == PAUSED_SUBTYPE and b.tripped
    assert b.limit.resets == "4:40pm (Asia/Calcutta)"
    for i in range(5):
        assert b.run(_req("finder:y", str(i))).subtype == PAUSED_SUBTYPE
    assert len(inner.calls) == 1 and (b.done, b.skipped) == (1, 5)


def test_breaker_lets_in_flight_calls_finish():
    gate = threading.Event()

    def slow(req):
        gate.wait(5)
        return {"findings": []}

    inner = FakeBackend({"slow": slow, "limit": BackendError(LIMIT_TEXT)})
    b = BreakerBackend(inner)
    out: list[ModelResult] = []
    t = threading.Thread(target=lambda: out.append(b.run(_req("slow"))))
    t.start()
    while not inner.calls:
        pass
    b.run(_req("limit"))
    gate.set()
    t.join()
    assert out[0].ok and b.tripped


# --- pipeline -------------------------------------------------------------


# Committed on the base branch: config is read from the base ref, so a worktree-only file
# would be ignored. One call in flight (BreakerBackend's gate) makes the call sequence exact:
# with several in flight, calls that start before the limited one returns still reach the
# model (they started before the trip), so an exact count would depend on thread timing.
# No Haiku triage, so the counts do not depend on how triage batches its calls.
LIMIT_CONFIG = "concurrency: 1\ncache_warm_seconds: 0\ntriage:\n  haiku: false\n"


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "README.md": "hi\n", ".preflight/config.yaml": LIMIT_CONFIG,
    })
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change service")
    return r


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def limited_backend(after: int) -> FakeBackend:
    """run1 fixture responses until `after` model calls, then the usage-limit error."""
    n = {"calls": 0}

    def respond(req):
        n["calls"] += 1
        if n["calls"] > after:
            return BackendError(LIMIT_TEXT)
        role = req.role.split(":security")[0]
        return FIXTURE.get(role) or {"findings": [], "files_read_outside_packet": []}

    return FakeBackend(default=respond)


def test_limit_mid_run_pauses_and_stops_calls(repo):
    full = FakeBackend(default=lambda req: FIXTURE.get(req.role.split(":security")[0])
                       or {"findings": [], "files_read_outside_packet": []})
    review(repo, full, full=True)
    total_calls = len(full.calls)
    assert total_calls > 4
    # Fresh repo state: a second run would be reused, and cached verdicts skip verifier calls.
    (repo / ".preflight" / "state.json").unlink()
    (repo / ".preflight" / "cache" / "verdicts.json").unlink(missing_ok=True)

    be = limited_backend(after=2)
    run, paths = review(repo, be, full=True)
    assert len(be.calls) == 3  # two answered, one limited, everything after skipped
    assert run.outcome == "paused"
    # M counts the calls this run asked for: verifications of findings a skipped lane would
    # have produced are never requested, so M can be below a complete run's call count.
    assert run.calls_done == 3 and 3 < run.calls_total <= total_calls
    m = run.calls_total
    assert any(r.startswith("paused: usage limit") for r in run.incomplete_reasons)
    assert not any(r.startswith("lane failed") for r in run.incomplete_reasons)
    md = paths["md"].read_text()
    assert (
        "Result: PAUSED — Claude usage limit reached (resets 4:40pm (Asia/Calcutta)). "
        f"3 of {m} calls done. Resume with: preflight-review review --base main "
        "--full --resume"
    ) in md
    assert json.loads(paths["json"].read_text())["outcome"] == "paused"
    assert summary_line(run, paths).startswith("preflight: paused: Claude usage limit reached")
    st = load_state(repo).branches["feature/x"]
    assert st.round == 0 and st.paused_report == run.basename
    calls = json.loads((repo / ".preflight" / "runs" / run.basename / "calls.json").read_text())
    assert len(calls) == 2


def test_resume_reuses_cached_calls_and_completes(repo):
    be = limited_backend(after=2)
    paused, _ = review(repo, be, full=True)
    assert paused.outcome == "paused"

    be2 = limited_backend(after=10_000)
    run, paths = review(repo, be2, full=True, resume=True)
    assert run.outcome == "complete"
    assert run.round == paused.round and run.basename == paused.basename
    assert run.calls_done == run.calls_total
    # The two cached calls were not sent again.
    assert len(be2.calls) == run.calls_total - 2
    assert f"resumed from round {run.round:02d}; reused 2 cached call(s)" in paths["md"].read_text()
    st = load_state(repo).branches["feature/x"]
    assert st.round == 1 and st.paused_report is None and st.last_report == run.basename


def test_resume_after_diff_change_runs_normally(repo):
    first, _ = review(repo, limited_backend(after=2), full=True)
    assert first.outcome == "paused"
    commit(repo, {"app/other.py": "X = 1\n"}, "more")
    be2 = limited_backend(after=10_000)
    run, paths = review(repo, be2, full=True, resume=True)
    assert run.outcome == "complete"
    assert run.calls_done == len(be2.calls)  # nothing reused
    assert "diff changed since paused run" in paths["md"].read_text()


def test_resume_without_paused_run_runs_normally(repo):
    run, paths = review(repo, limited_backend(after=10_000), full=True, resume=True)
    assert run.outcome == "complete"
    assert "no paused run on this branch" in paths["md"].read_text()


def test_second_paused_run_keeps_resume_cache(repo):
    """A non-resume run that pauses again on the same diff merges, never wipes, the cache."""
    paused, _ = review(repo, limited_backend(after=2), full=True)
    calls_file = repo / ".preflight" / "runs" / paused.basename / "calls.json"
    assert len(json.loads(calls_file.read_text())) == 2
    again, _ = review(repo, limited_backend(after=0), full=True)
    assert again.outcome == "paused" and again.basename == paused.basename
    assert len(json.loads(calls_file.read_text())) == 2

    be = limited_backend(after=10_000)
    run, _ = review(repo, be, full=True, resume=True)
    assert run.outcome == "complete" and len(be.calls) == run.calls_total - 2
    # Replayed calls keep the paused run's raw output, not the {"resumed": true} stub.
    raws = [json.loads(f.read_text()) for f in (repo / ".preflight" / "runs" / run.basename / "raw").glob("*.json")]
    assert raws and not any(r["raw"] == {"resumed": True} for r in raws)


def test_paused_run_on_other_scope_keeps_earlier_resume_point(repo):
    """The pre-push hook pausing on a different scope does not replace a richer resume point."""
    paused, _ = review(repo, limited_backend(after=2), full=True)
    st = load_state(repo).branches["feature/x"]
    commit(repo, {"app/other.py": "X = 1\n"}, "more")
    other, paths = review(repo, limited_backend(after=0), full=True)
    assert other.outcome == "paused"
    assert f"kept the resume point of paused run {paused.basename}" in paths["md"].read_text()
    st2 = load_state(repo).branches["feature/x"]
    assert (st2.paused_report, st2.paused_diff_hash) == (st.paused_report, st.paused_diff_hash)
    assert sum(map(len, load_calls(repo / ".preflight" / "runs" / paused.basename).values())) == 2
