"""Reviewed-file cache end to end: a later round reviews only file changes not reviewed
before, lists the rest under Reviewed earlier, and never caches what was not reviewed
cleanly (fake backend only)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from preflight_review import pipeline, prompts
from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review, summary_line
from preflight_review.render.report import coverage_of

from .conftest import _git, branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fake" / "run1.json").read_text())
EMPTY = {"findings": [], "files_read_outside_packet": []}
LIMIT_TEXT = "You've hit your 5-hour limit · resets 4:40pm (Asia/Calcutta)"
CONFIG = (
    "version: 1\nmode: standard\nwalkthrough: false\nconfirm_above_usd: null\n"
    "cache_warm_seconds: 0\ntriage:\n  haiku: false\nlearnings:\n  rerank: false\n"
)


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def _mod(name: str, value: int) -> str:
    return f'"""{name}."""\n\n\ndef {name}_value(x):\n    return x + {value}\n'


@pytest.fixture(autouse=True)
def _no_tools(monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config, *a: ([], []))


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "app/other.py": _mod("other", 1),
        "app/third.py": _mod("third", 1), ".preflight/config.yaml": CONFIG,
    })
    branch(r, "feature/x")
    commit(r, {
        "app/service.py": SERVICE_BAD, "app/other.py": _mod("other", 2),
        "app/third.py": _mod("third", 2),
    }, "change three files")
    return r


def fixture_backend() -> FakeBackend:
    return FakeBackend(default=lambda req: FIXTURE.get(req.role.split(":security")[0]) or EMPTY)


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def finder_calls(be) -> list:
    return [c for c in be.calls if c.role.startswith("finder:")]


def earlier_paths(run) -> list[str]:
    return [p for p, _r, _n in run.reviewed_earlier]


def test_second_round_reviews_only_the_new_change(repo):
    first, _ = review(repo, fixture_backend())
    assert first.outcome == "complete" and sorted(first.reviewed_paths) == [
        "app/other.py", "app/service.py", "app/third.py"
    ]
    sid = first.policy.report[0].stable_id
    commit(repo, {"app/other.py": _mod("other", 3)}, "touch other")
    be = FakeBackend(default=EMPTY)
    run, paths = review(repo, be)
    assert run.round == 2 and run.outcome == "complete"
    assert run.reviewed_paths == ["app/other.py"]
    assert run.estimate is not None and run.estimate.files == 1  # priced on fresh work only
    assert sorted(run.reviewed_earlier) == [
        ("app/service.py", first.basename, 1), ("app/third.py", first.basename, 1),
    ]
    cov = coverage_of(run)
    assert cov.total == 3
    assert len(cov.reviewed) + len(cov.reviewed_earlier) + len(cov.not_reviewed) == cov.total
    # Only the fresh file's diff reaches a model.
    assert finder_calls(be)
    for c in finder_calls(be):
        assert "+    return x + 3" in c.system_prompt
        assert "return discounted * (1 - rate)" not in c.system_prompt
    # The cached file's open finding is still listed.
    assert (sid, "open") in run.policy.previously_reported
    md = paths["md"].read_text()
    assert "## Reviewed earlier (2)" in md and f"app/service.py — round 01 ({first.basename})" in md
    assert "reviewed 1/3" in md and "2 reviewed earlier" in md
    line = summary_line(run, paths)
    assert line.startswith("preflight: complete:") and "(2 files reviewed earlier)" in line


def test_rebase_onto_a_base_that_does_not_touch_the_files_reviews_nothing(repo):
    first, paths1 = review(repo, fixture_backend())
    _git(repo, "checkout", "-q", "main")
    commit(repo, {"docs_unrelated.txt": "x\n"}, "unrelated base change")
    _git(repo, "checkout", "-q", "feature/x")
    _git(repo, "rebase", "-q", "main")
    paths1["md"].unlink()  # past the whole-diff report reuse: the cache alone decides
    be = FakeBackend(default=EMPTY)
    run, paths = review(repo, be)
    assert be.calls == []
    assert run.outcome == "complete" and run.round == 2 and not run.reused
    assert run.reviewed_paths == []
    assert sorted(earlier_paths(run)) == ["app/other.py", "app/service.py", "app/third.py"]
    assert any("all 3 changed files were reviewed earlier (round 01)" in n for n in run.notes)
    sid = first.policy.report[0].stable_id
    assert (sid, "open") in run.policy.previously_reported
    assert "## Not reviewed (0)" in paths["md"].read_text()


def test_merge_of_main_that_touched_a_file_makes_it_fresh(tmp_path):
    long_base = _mod("other", 1) + "".join(f"\n\ndef pad_{i}():\n    return {i}\n" for i in range(8))
    r = make_repo(tmp_path, {
        "app/other.py": long_base, "app/third.py": _mod("third", 1),
        ".preflight/config.yaml": CONFIG,
    })
    branch(r, "feature/x")
    commit(r, {
        "app/other.py": long_base.replace("x + 1", "x + 2"), "app/third.py": _mod("third", 2),
    }, "change")
    _, paths1 = review(r, FakeBackend(default=EMPTY))
    _git(r, "checkout", "-q", "main")
    commit(r, {"app/other.py": long_base.replace("return 7", "return 70")}, "main edits other")
    _git(r, "checkout", "-q", "feature/x")
    _git(r, "merge", "-q", "--no-edit", "main")
    # The branch's own diff lines are unchanged, so the whole-diff reuse would answer;
    # without that report the cache decides per file: other.py's old blob moved.
    paths1["md"].unlink()
    be = FakeBackend(default=EMPTY)
    run, _ = review(r, be)
    assert run.reviewed_paths == ["app/other.py"]
    assert earlier_paths(run) == ["app/third.py"]


def test_changed_caller_demotes_the_cached_callee(tmp_path):
    lib = "def compute(x):\n    return x + 1\n"
    app = "from lib import compute\n\n\ndef main():\n    return compute(1)\n"
    r = make_repo(tmp_path, {
        "lib.py": lib, "app.py": app, "third.py": _mod("third", 1),
        ".preflight/config.yaml": CONFIG,
    })
    branch(r, "feature/x")
    commit(r, {"lib.py": lib.replace("x + 1", "x + 2"), "third.py": _mod("third", 2)}, "c1")
    review(r, FakeBackend(default=EMPTY))
    commit(r, {"app.py": app.replace("compute(1)", "compute(2)")}, "caller changes")
    run, _ = review(r, FakeBackend(default=EMPTY))
    assert sorted(run.reviewed_paths) == ["app.py", "lib.py"]
    assert earlier_paths(run) == ["third.py"]
    assert any("reviewed again because a caller" in n for n in run.notes)


def test_prompt_change_reviews_everything_again(repo, monkeypatch):
    review(repo, fixture_backend())
    commit(repo, {"app/other.py": _mod("other", 3)}, "touch other")
    real = prompts.prompt_sha
    monkeypatch.setattr(prompts, "prompt_sha", lambda name: "edited-" + real(name))
    run, _ = review(repo, fixture_backend())
    assert run.reviewed_earlier == []
    assert sorted(run.reviewed_paths) == ["app/other.py", "app/service.py", "app/third.py"]


def test_failed_lane_files_are_not_cached(repo, monkeypatch):
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 75)  # one packet per file

    def respond(req):
        if req.role.startswith("finder:correctness") and "def other_value" in req.system_prompt:
            return BackendError("lane down")
        return FIXTURE.get(req.role.split(":security")[0].split("@b")[0]) or EMPTY

    first, _ = review(repo, FakeBackend(default=respond))
    assert first.batches == 3
    assert first.outcome == "incomplete" and "app/other.py" in first.reviewed_paths
    be = FakeBackend(default=EMPTY)
    run, _ = review(repo, be)
    assert run.reviewed_paths == ["app/other.py"]
    assert sorted(earlier_paths(run)) == ["app/service.py", "app/third.py"]


def test_paused_run_caches_nothing(tmp_path):
    r = make_repo(tmp_path, {
        "app/other.py": _mod("other", 1), ".preflight/config.yaml": CONFIG + "concurrency: 1\n",
    })
    branch(r, "feature/x")
    commit(r, {"app/other.py": _mod("other", 2)}, "change")
    n = {"calls": 0}

    def respond(req):
        n["calls"] += 1
        return BackendError(LIMIT_TEXT) if n["calls"] > 2 else EMPTY

    run, _ = review(r, FakeBackend(default=respond))
    assert run.outcome == "paused"
    assert not (r / ".preflight" / "cache" / "reviewed.json").exists()
    run2, _ = review(r, FakeBackend(default=EMPTY))
    assert run2.reviewed_paths == ["app/other.py"] and run2.reviewed_earlier == []


def test_full_ignores_the_cache_but_still_writes_it(repo):
    review(repo, fixture_backend())
    commit(repo, {"app/other.py": _mod("other", 3)}, "touch other")
    run, _ = review(repo, fixture_backend(), full=True)
    assert run.reviewed_earlier == []
    assert sorted(run.reviewed_paths) == ["app/other.py", "app/service.py", "app/third.py"]
    data = json.loads((repo / ".preflight" / "cache" / "reviewed.json").read_text())
    # Same change, same key: service.py and third.py now point at round 2; other.py's
    # earlier change keeps its round-1 entry.
    assert sorted(e["round"] for e in data.values()) == [1, 2, 2, 2]
    assert {e["branch"] for e in data.values()} == {"feature/x"}


def test_incremental_option_is_a_no_op(repo):
    review(repo, fixture_backend())
    commit(repo, {"app/other.py": _mod("other", 3)}, "touch other")
    run, _ = review(repo, FakeBackend(default=EMPTY), incremental=False)
    assert run.reviewed_paths == ["app/other.py"]
    assert "--incremental" not in pipeline.rerun_command(ReviewOptions(repo_root=repo))
    assert "--full" in pipeline.rerun_command(ReviewOptions(repo_root=repo, full=True))


def test_cached_non_python_file_is_reviewed_again_when_another_file_changes(tmp_path):
    # The symbol index covers Python only, so a cached .ts file's callers are unknown: a
    # fresh change anywhere must not leave it under Reviewed earlier.
    r = make_repo(tmp_path, {
        "web/a.ts": "export function a(x) { return x + 1 }\n",
        "web/b.ts": "import { a } from './a'\nexport const b = a(1)\n",
        ".preflight/config.yaml": CONFIG,
    })
    branch(r, "feature/ts")
    commit(r, {"web/a.ts": "export function a(x, y) { return x + y }\n"}, "change a")
    first, _ = review(r, fixture_backend())
    assert first.outcome == "complete" and "web/a.ts" in first.reviewed_paths
    commit(r, {"web/b.ts": "import { a } from './a'\nexport const b = a(1, 2)\n"}, "change b")
    second, _ = review(r, fixture_backend())
    assert "web/a.ts" not in earlier_paths(second)
    assert "web/a.ts" in second.reviewed_paths


def test_unchanged_branch_still_serves_non_python_files_from_the_cache(tmp_path):
    r = make_repo(tmp_path, {
        "web/a.ts": "export function a(x) { return x + 1 }\n", ".preflight/config.yaml": CONFIG,
    })
    branch(r, "feature/ts")
    commit(r, {"web/a.ts": "export function a(x, y) { return x + y }\n"}, "change a")
    review(r, fixture_backend())
    be = fixture_backend()
    second, _ = review(r, be, full=False)
    assert earlier_paths(second) in (["web/a.ts"], [])  # same diff may reuse the old report
    assert not finder_calls(be)
