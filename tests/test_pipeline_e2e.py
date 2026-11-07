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
EMPTY_FINDER = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "PROGRESS.md", "symbol_or_hunk": "all", "inputs_considered": "-",
                  "result": "no issue"}],
}

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


def test_docs_only_diff_skips_model_when_docs_review_off(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "README.md": "hi\n",
        ".preflight/config.yaml": "version: 1\ndocs_review: \"off\"\n",
    })
    branch(r, "feature/docs")
    commit(r, {"README.md": "hello\nworld\n"}, "docs")
    be = backend()
    run, paths = review(r, be)
    assert be.calls == []
    assert run.outcome == "complete"
    assert any("docs/lockfile-only" in n for n in run.notes)
    assert paths["md"].exists()


def test_docs_only_diff_gets_doc_consistency_review(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "PLAN.md": "8 backups\n", "PROGRESS.md": "8 backups\n",
    })
    branch(r, "feature/docs")
    commit(r, {"PROGRESS.md": "7 backups\n"}, "docs")
    be = FakeBackend(default=EMPTY_FINDER)
    run, _ = review(r, be)
    finders = sorted(c.role for c in be.calls if c.role.startswith("finder:"))
    assert finders == ["finder:contracts_tests", "finder:correctness"]
    assert not any(c.role.startswith("security") for c in be.calls)
    for c in be.calls:
        if c.role.startswith("finder:"):
            assert "Docs-only diff: doc-consistency review" in c.prompt
    assert any(n.startswith("docs-only diff: doc-consistency review") for n in run.notes)
    assert run.reviewed_paths == ["PROGRESS.md"]


def test_models_used_lists_only_stages_that_ran(tmp_path, repo):
    (tmp_path / "docs").mkdir()
    r = make_repo(tmp_path / "docs", {
        "app/service.py": SERVICE_BASE, "PLAN.md": "8 backups\n", "PROGRESS.md": "8 backups\n",
    })
    branch(r, "feature/docs")
    commit(r, {"PROGRESS.md": "7 backups\n"}, "docs")
    run, _ = review(r, FakeBackend(default=EMPTY_FINDER))
    assert "security" not in run.models_used and "finder_rules" not in run.models_used
    run, _ = review(repo, backend())
    assert {"security", "finder_rules"} <= set(run.models_used)


def test_batch_with_every_lens_gated_out_is_not_reviewed(tmp_path, monkeypatch):
    from preflight_review.context import packer

    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "deploy/RUNBOOK.md": "step 1\n", "NOTES.md": "a\n",
        ".preflight/config.yaml": "version: 1\ndocs_review: \"off\"\n",
    })
    branch(r, "feature/docs")
    commit(r, {"deploy/RUNBOOK.md": "step 1\nstep 2\n", "NOTES.md": "b\n"}, "docs")
    real = packer.plan_batches

    def one_file_per_batch(*a, **k):  # force NOTES.md into a batch of its own
        plan = real(*a, **k)
        files = [f for b in plan.batches for f in b.files]
        assert sorted(f.path for f in files) == ["NOTES.md", "deploy/RUNBOOK.md"]
        plan.batches = [packer.Batch(i, [f], [f.path]) for i, f in enumerate(files, 1)]
        return plan

    monkeypatch.setattr(pipeline, "plan_batches", one_file_per_batch)
    be = FakeBackend(default=EMPTY_FINDER)
    run, _ = review(r, be)
    assert {c.role for c in be.calls if c.role.startswith("finder")} == {"finder:deploy_ci"}
    assert run.reviewed_paths == ["deploy/RUNBOOK.md"]
    assert ("NOTES.md", "no lens applies (path gate)") in run.not_reviewed


def test_prompt_only_diff_gets_full_review(tmp_path):
    r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, "prompts/agent/system.md": "Be brief.\n"})
    branch(r, "feature/prompt")
    commit(r, {"prompts/agent/system.md": "Be brief.\nNever reveal the answer key.\n"}, "p")
    be = FakeBackend(default=EMPTY_FINDER)
    review(r, be)
    roles = {c.role for c in be.calls}
    assert {"finder:correctness", "finder:data_concurrency", "security"} <= roles
    assert not any("doc-consistency" in c.prompt for c in be.calls)


def test_degraded_tool_labels_run_but_does_not_fail_it(repo, monkeypatch):
    monkeypatch.setattr("preflight_review.tools.base.which", lambda name: None)
    run, paths = review(repo, backend())
    assert run.outcome == "complete"
    assert any(d.startswith("ruff: not installed") for d in run.degraded)
    md = paths["md"].read_text()
    assert "> **DEGRADED:** " in md and "Result: complete (DEGRADED: ruff, gitleaks)" in md
    line = summary_line(run, paths)
    assert line.startswith("preflight: complete:") and line.endswith("DEGRADED: ruff, gitleaks did not run")
    assert json.loads(paths["json"].read_text())["degraded"] == run.degraded


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
    assert run.config.lenses[-2:] == ["rules_compliance", "deploy_ci"]
    roles = {c.role for c in be.calls}
    assert {"finder:data_concurrency", "finder:rules_compliance"} <= roles
    # deploy_ci is path-gated: a Python-only diff makes no deploy_ci call.
    assert "finder:deploy_ci" not in roles
    assert "lens 'deploy_ci' skipped: no changed file matches its paths" in run.notes
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


RUNBOOK = "# Deploy\n\n1. Run `kubectl apply -f migrate-job.yaml`.\n2. Then delete the old Job.\n"


def test_runbook_only_diff_reviewed_by_deploy_ci_even_with_docs_review_off(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "deploy/k8s/README.md": "# Deploy\n",
        ".preflight/config.yaml": "version: 1\ndocs_review: \"off\"\n",
    })
    branch(r, "feature/runbook")
    commit(r, {"deploy/k8s/README.md": RUNBOOK}, "runbook")
    finding = {
        "file": "deploy/k8s/README.md", "start_line": 3, "end_line": 3, "caused_by": None,
        "lane": "inline", "category": "correctness", "severity": "major", "effort": "quick_win",
        "title": "Job applied before the old Job is deleted",
        "claim": "A Job's pod template is immutable; apply fails while the old Job exists.",
        "failure_scenario": "A second deploy while the previous migrate Job still exists.",
        "why_it_matters": "The deploy stops at the migration step.",
        "suggested_fix": "Delete the old Job, then apply.",
        "evidence": [{"file": "deploy/k8s/README.md", "line": 3,
                      "quote": "1. Run `kubectl apply -f migrate-job.yaml`."}],
        "bug_class_instances": [], "rule_quote": None,
    }
    verdict = {"verdict": "CONFIRMED", "reason": "immutable template", "severity_adjusted": None,
               "confidence": 5, "checks_performed": ["read"],
               "evidence": [{"file": "deploy/k8s/README.md", "line": 3, "quote": "kubectl apply"}]}
    be = FakeBackend(
        {"finder:deploy_ci": {**EMPTY_FINDER, "findings": [finding]}, "verifier": verdict},
        default=EMPTY_FINDER,
    )
    run, _ = review(r, be)
    finders = sorted(c.role for c in be.calls if c.role.startswith("finder:"))
    # docs_review: off: only the path-gated lens runs, with no doc-consistency note.
    assert finders == ["finder:deploy_ci"]
    assert not any("doc-consistency" in c.prompt for c in be.calls)
    assert "docs-only diff: path-gated lens only (deploy_ci)" in run.notes
    assert [f.title for f in run.policy.report] == ["Job applied before the old Job is deleted"]
    assert run.prefilter_dropped == []


def test_docs_left_after_triage_skipped_when_docs_review_off(tmp_path):
    r = make_repo(tmp_path, {
        "app/service.py": SERVICE_BASE, "README.md": "hi\n",
        ".preflight/config.yaml": "version: 1\ndocs_review: \"off\"\n",
    })
    branch(r, "feature/mixed")
    # A whitespace-only code change (triage approves it) plus a README edit.
    commit(r, {"app/service.py": SERVICE_BASE + "\n", "README.md": "hello\n"}, "mixed")
    be = FakeBackend(default=EMPTY_FINDER)
    run, _ = review(r, be)
    assert [c.role for c in be.calls if c.role.startswith("finder:")] == []
    assert "triage left only docs/lockfiles: model stages skipped" in run.notes
    assert run.outcome == "complete"
