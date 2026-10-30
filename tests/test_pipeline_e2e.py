"""End-to-end pipeline tests with a fake backend (no real model, no real gh)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from preflight_review import pipeline
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review, summary_line
from preflight_review.state import load_state

from .conftest import branch, commit, make_repo, write

FIXTURES = Path(__file__).parent / "fixtures" / "fake"

SERVICE_BASE = '''"""Order pricing."""


def apply_discount(subtotal, rate):
    discounted = subtotal - subtotal * rate
    return discounted


def total(items):
    return sum(i["price"] for i in items)


def label(name):
    return name.title()
'''

SERVICE_BAD = '''"""Order pricing."""


def apply_discount(subtotal, rate):
    discounted = subtotal - subtotal * rate
    return discounted * (1 - rate)


def total(items):
    return sum(i["price"] for i in items)


def label(name):
    return name.title()
'''


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def backend(name="run1.json"):
    return FakeBackend.from_file(FIXTURES / name)


@pytest.fixture
def repo(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "README.md": "hi\n"})
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change service")
    return r


def review(repo, be, **kw):
    opts = ReviewOptions(repo_root=repo, base="main", **kw)
    return run_review(opts, backend=be, gh_runner=no_gh)


def test_full_run_writes_outputs(repo):
    run, paths = review(repo, backend())
    assert paths["md"].name == f"{repo.name}-PR-feature-x-preflight-review-01.md"
    for k in ("json", "manifest", "ndjson", "latest"):
        assert paths[k].exists() or paths[k].is_symlink()
    assert "Prompt for AI agent" in paths["md"].read_text()
    assert run.outcome == "complete" and run.round == 1
    st = load_state(repo).branches["feature/x"]
    assert st.round == 1 and st.last_report == run.basename
    assert len(run.policy.report) == 1
    sid = run.policy.report[0].stable_id
    assert st.findings[sid].status == "open"
    assert "1 confirmed (1 major+)" in summary_line(run, paths)
    assert summary_line(run, paths).startswith("preflight: complete:")


def test_second_run_reuses(repo):
    review(repo, backend())
    be = backend()
    run, paths = review(repo, be)
    assert run.reused and be.calls == []
    assert run.round == 1
    assert load_state(repo).branches["feature/x"].round == 1
    assert "reusing" in summary_line(run, paths)


def test_full_forces_rerun(repo):
    review(repo, backend())
    run, paths = review(repo, backend(), full=True)
    assert not run.reused and run.round == 2
    assert paths["md"].name.endswith("-02.md")


def test_new_commit_marks_previously_reported(repo):
    first, _ = review(repo, backend())
    sid = first.policy.report[0].stable_id
    commit(repo, {"app/service.py": SERVICE_BAD + "\n\ndef extra():\n    return 1\n"}, "more")
    run, _ = review(repo, backend())
    assert run.round == 2 and not run.reused
    assert sid in [s for s, _st in run.policy.previously_reported]
    assert all(f.stable_id != sid for f in run.policy.report)


def test_committed_only_excludes_worktree_edit(repo):
    write(repo, "app/other.py", "x = 1\n")
    subprocess.run(["git", "add", "app/other.py"], cwd=repo, check=True)
    write(repo, "README.md", "edited\n")
    run, _ = review(repo, backend(), include_worktree=False)
    paths = [f.path for f in run.scope.files]
    assert "README.md" not in paths and "app/other.py" not in paths
    run2, _ = review(repo, backend(), full=True)
    assert "README.md" in [f.path for f in run2.scope.files]


def test_worktree_config_edit_is_ignored(tmp_path):
    r = make_repo(
        tmp_path,
        {"app/service.py": SERVICE_BASE, ".preflight/config.yaml": "version: 1\nprofile: quiet\n"},
    )
    branch(r, "feature/x")
    commit(r, {"app/service.py": SERVICE_BAD}, "change")
    write(r, ".preflight/config.yaml", "version: 1\nlenses: [bogus]\n")
    run, _ = review(r, backend())
    assert run.config.profile == "quiet"
    assert run.config_source == "base:main"


def test_docs_only_diff_skips_model(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "README.md": "hi\n"})
    branch(r, "feature/docs")
    commit(r, {"README.md": "hello\nworld\n"}, "docs")
    be = backend()
    run, paths = review(r, be)
    assert be.calls == []
    assert run.outcome == "complete"
    assert any("docs/lockfile-only" in n for n in run.notes)
    assert paths["md"].exists()


def test_failing_finder_lane_is_incomplete(repo, tmp_path):
    p = tmp_path / "errlane.json"
    p.write_text(
        json.dumps(
            {
                "finder:correctness": {"__error__": "boom"},
                "finder:contracts_tests": {"findings": [], "files_read_outside_packet": []},
            }
        )
    )
    run, _ = review(repo, FakeBackend.from_file(p))
    assert run.outcome == "incomplete"
    assert "lane failed: correctness" in run.incomplete_reasons


def test_all_default_lenses_run(repo):
    be = backend()
    run, _ = review(repo, be)
    assert run.config.lenses[-1] == "rules_compliance"
    roles = {c.role for c in be.calls}
    assert {"finder:data_concurrency", "finder:rules_compliance"} <= roles
    assert run.outcome == "complete"


def test_preflight_outputs_do_not_change_diff_hash(repo):
    r1, _ = review(repo, backend())
    assert (repo / ".preflight" / "reviews").is_dir()
    r2, _ = review(repo, backend())
    assert r2.reused and r1.scope.diff_hash == r2.scope.diff_hash


def test_empty_diff_completes(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE})
    branch(r, "feature/none")
    be = backend()
    run, _ = review(r, be)
    assert be.calls == [] and run.outcome == "complete"
    assert any("empty diff" in n for n in run.notes)


def test_select_backend_unknown_raises():
    from preflight_review.config import Config
    from preflight_review.errors import ConfigError

    with pytest.raises(ConfigError):
        pipeline.select_backend(ReviewOptions(repo_root=Path("."), backend_name="nope"), Config())


def test_select_backend_fake_default(monkeypatch):
    from preflight_review.config import Config

    monkeypatch.delenv("PREFLIGHT_FAKE_RESPONSES", raising=False)
    b = pipeline.select_backend(ReviewOptions(repo_root=Path("."), backend_name="fake"), Config())
    assert b.name == "fake"
