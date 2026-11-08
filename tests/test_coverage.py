"""Coverage guarantees: batching, run budget, lane failure, reuse (fake backend only)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from preflight_review import cli, naming, pipeline
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review

from .conftest import branch, commit, make_repo

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x.py", "symbol_or_hunk": "f", "inputs_considered": "none", "result": "no issue"}],
}
DIRS = ("app/api", "app/billing", "app/core")
N_SMALL = 30
BIG = "app/core/big_module.py"


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def _small(i: int, new: bool) -> str:
    rows = [f"def helper_{i}_{j}(value):" for j in range(6)]
    body = []
    for j, r in enumerate(rows):
        body.append(r)
        body.append(f"    return value + {j}{'0' if new else ''}  # file{i} padding text here to bulk")
    return "\n".join(body) + "\n"


def _big(new: bool) -> str:
    out = []
    for i in range(300):
        if new and i % 12 == 0:
            out.append(f"changed_marker_{i:03d} = 'added line number {i} with some padding text'")
        else:
            out.append(f"value_{i:03d} = {i}  # untouched line with padding text")
    return "\n".join(out) + "\n"


@pytest.fixture
def big_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    monkeypatch.setattr(pipeline, "BATCH_DIFF_TOKENS", 1000)
    base = {f"{DIRS[i % 3]}/mod_{i:02d}.py": _small(i, False) for i in range(N_SMALL)}
    base[BIG] = _big(False)
    r = make_repo(tmp_path, base)
    branch(r, "feature/big")
    commit(r, {**{p: _small(i, True) for i, p in enumerate(sorted(k for k in base if k != BIG), 0)},
               BIG: _big(True)}, "big change")
    return r



def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def _finder_systems(be) -> str:
    return "\n".join(c.system_prompt for c in be.calls if c.role.startswith("finder:"))


def test_large_diff_is_fully_covered(big_repo):
    be = FakeBackend(default=EMPTY)
    run, paths = review(big_repo, be, budget_usd=100.0)
    m = len(run.scope.files)
    assert m == N_SMALL + 1
    assert run.batches > 1
    assert run.outcome == "complete", run.incomplete_reasons
    system = _finder_systems(be)
    for fc in run.scope.files:
        assert fc.path in system, fc.path
    for i in range(0, 300, 12):
        assert f"changed_marker_{i:03d}" in system
    md = paths["md"].read_text()
    assert f"reviewed {m}/{m}" in md
    assert "## Not reviewed (0)" in md
    assert run.not_reviewed == []
    assert any(k.startswith("finder:") and "@b" in k for k in run.lane_results)
    assert len(run.packet_shas) == run.batches


def test_default_config_has_no_budget_and_reviews_every_batch(big_repo):
    be = FakeBackend(default=EMPTY)
    run, _paths = review(big_repo, be)
    assert run.config.budget_usd is None
    assert run.batches > 1 and run.not_reviewed == []
    assert run.outcome == "complete", run.incomplete_reasons
    assert be.calls and all(c.max_budget_usd is None for c in be.calls)


def test_budget_exhaustion_marks_rest_not_reviewed(big_repo):
    be = FakeBackend(default=EMPTY)
    # Explicit standard: under auto the crucial billing files would form the first packet.
    run, paths = review(big_repo, be, budget_usd=0.08, mode="standard")
    m = len(run.scope.files)
    assert run.batches == 1
    assert run.outcome == "incomplete"
    assert any(r.startswith("coverage:") for r in run.incomplete_reasons)
    assert run.not_reviewed
    assert all("run budget exhausted" in reason for _p, reason in run.not_reviewed)
    assert any(p == BIG and reason.startswith("partially reviewed") for p, reason in run.not_reviewed)  # riskiest file goes first
    md = paths["md"].read_text()
    k = len(run.not_reviewed)
    assert f"## Not reviewed ({k})" in md
    for p, _r in run.not_reviewed:
        assert p in md.split("## Not reviewed", 1)[1]
    n_rev = len(run.reviewed_paths)
    assert n_rev < m and n_rev + k == m
    assert f"reviewed {n_rev}/{m}" in md
    data = json.loads(paths["json"].read_text())
    assert data["scope"]["not_reviewed"]
    assert data["outcome"] == "incomplete"


def test_cli_budget_exhaustion_exits_1(big_repo, monkeypatch, tmp_path, capsys):
    resp = tmp_path / "resp.json"
    resp.write_text(json.dumps({"__default__": EMPTY}))
    monkeypatch.chdir(big_repo)
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(resp))
    monkeypatch.setattr(
        pipeline, "pr_label", lambda root, br, gh_runner=None: naming.pr_label(root, br, no_gh)
    )
    code = cli.main(["review", "--backend", "fake", "--base", "main", "--budget-usd", "0.08"])
    assert code == 1
    assert "incomplete" in capsys.readouterr().err


def test_all_lanes_failing_in_batch_marks_paths(big_repo):
    be = FakeBackend(default={"__never__": 1})
    be.default = None  # every finder/security role has no response -> errors
    run, paths = review(big_repo, be, budget_usd=100.0)
    assert run.outcome == "incomplete"
    assert "all lanes failed" in run.incomplete_reasons
    assert run.not_reviewed
    assert all("all lanes failed" in reason for _p, reason in run.not_reviewed)
    assert run.reviewed_paths == []


@pytest.fixture
def small_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    r = make_repo(tmp_path, {"app/service.py": _small(1, False)})
    branch(r, "feature/x")
    commit(r, {"app/service.py": _small(1, True)}, "change")
    return r


def test_single_batch_keeps_plain_lane_names(small_repo):
    be = FakeBackend(default=EMPTY)
    run, _ = review(small_repo, be)
    assert run.batches == 1 and run.outcome == "complete"
    assert "finder:correctness" in run.lane_results
    assert not any("@b" in k for k in run.lane_results)
    assert run.reviewed_paths == ["app/service.py"]


def test_shallow_lens_is_retried_and_noted_in_run_section(small_repo):
    shallow = {"findings": [], "files_read_outside_packet": [], "coverage": []}
    be = FakeBackend({"finder:correctness": shallow}, default=EMPTY)
    run, paths = review(small_repo, be)
    assert run.outcome == "complete"
    assert "finder:correctness#retry" in run.lane_results
    assert [c.role for c in be.calls].count("finder:correctness") == 2
    note = next(n for n in run.notes if n.startswith("shallow lens: correctness"))
    assert "retry was also shallow" in note
    assert "- Note: shallow lens: correctness" in paths["md"].read_text()


def test_incomplete_run_is_not_reused(big_repo):
    run1, _ = review(big_repo, FakeBackend(default=EMPTY), budget_usd=0.08)
    assert run1.outcome == "incomplete"
    be = FakeBackend(default=EMPTY)
    run2, _ = review(big_repo, be, budget_usd=100.0)
    assert not run2.reused and be.calls
    assert run2.outcome == "complete"


def test_reuse_json_counts_from_complete_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change")
    monkeypatch.chdir(r)
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(FIXTURES / "run1.json"))
    monkeypatch.setattr(
        pipeline, "pr_label", lambda root, br, gh_runner=None: naming.pr_label(root, br, no_gh)
    )
    args = ["review", "--backend", "fake", "--base", "main", "--json"]
    cli.main(args)
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["confirmed"] > 0
    assert cli.main(args) == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["reused"] is True
    assert second["confirmed"] == first["confirmed"]
    assert second["unverified"] == first["unverified"]


def test_previously_reported_line_has_title_and_location(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change")
    be = lambda: FakeBackend.from_file(FIXTURES / "run1.json")
    first, _ = review(r, be())
    f = first.policy.report[0]
    commit(r, {"app/service.py": SERVICE_BAD + "\n\ndef extra():\n    return 1\n"}, "more")
    run, paths = review(r, be(), incremental=True)
    md = paths["md"].read_text()
    line = next(ln for ln in md.splitlines() if f.stable_id in ln and "—" in ln)
    assert f.title in line and f"{f.file}:" in line
    assert re.search(rf"{re.escape(f.file)}:\d+", line)
