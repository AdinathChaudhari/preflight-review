"""Seeded cases, materialisation, run_eval and the eval CLI (fake backend only)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from preflight_review.cli import main
from preflight_review.errors import ConfigError, SchemaError
from preflight_review.evaluation.runner import load_clean_cases, run_eval
from preflight_review.evaluation.seeds import load_seeded_cases, materialise

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "eval" / "seeded" / "cases"
FAKE = ROOT / "tests" / "fixtures" / "fake" / "eval_seeded.json"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("preflight_review.pipeline.run_tools", lambda scope, config: ([], []))
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(FAKE))


def test_load_seeded_cases():
    cases = load_seeded_cases(CASES)
    assert len(cases) == 30
    assert [c.name for c in cases] == sorted(c.name for c in cases)
    by = {c.name: c for c in cases}
    assert by["benign_refactor"].expected == []
    assert by["fstring_sql"].expected[0].id == "fstring_sql:0"
    assert all(len(c.after.splitlines()) <= 60 for c in cases)


def test_load_seeded_missing_file(tmp_path):
    (tmp_path / "broken").mkdir()
    with pytest.raises(SchemaError):
        load_seeded_cases(tmp_path)


def test_seeded_cases_have_distinct_paths():
    cases = load_seeded_cases(CASES)
    assert len(cases) == 30
    assert len({c.file for c in cases}) == 30
    assert all(c.file != "app/target.py" for c in cases)
    for c in cases:
        assert all(g.file == c.file for g in c.expected)


@pytest.mark.parametrize("bad", ["", "/abs/x.py", "../x.py", "a/../../x.py", 3, None])
def test_meta_json_validation(tmp_path, bad):
    d = tmp_path / "c"
    d.mkdir()
    for n in ("before.py", "after.py"):
        (d / n).write_text("x = 1\n")
    (d / "expected.json").write_text("[]")
    (d / "meta.json").write_text(json.dumps({"file": bad}))
    with pytest.raises(SchemaError):
        load_seeded_cases(tmp_path)


def test_meta_json_absent_defaults_to_target(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    for n in ("before.py", "after.py"):
        (d / n).write_text("x = 1\n")
    (d / "expected.json").write_text("[]")
    assert load_seeded_cases(tmp_path)[0].file == "app/target.py"


def test_two_cases_materialise_without_collision(tmp_path):
    cases = {c.name: c for c in load_seeded_cases(CASES)}
    a = materialise(cases["shell_true"], tmp_path)
    b = materialise(cases["fstring_sql"], tmp_path)
    assert (a / cases["shell_true"].file).is_file()
    assert (b / cases["fstring_sql"].file).is_file()
    assert cases["shell_true"].file != cases["fstring_sql"].file


def test_materialise_two_commits(tmp_path):
    case = next(c for c in load_seeded_cases(CASES) if c.name == "shell_true")
    repo = materialise(case, tmp_path)
    log = subprocess.run(
        ["git", "log", "--oneline", "seed/shell_true"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip().splitlines()
    assert len(log) == 2
    assert "shell=True" in (repo / case.file).read_text()
    assert (repo / ".preflight/config.yaml").is_file()


def test_materialise_multi_commit_case(tmp_path):
    case = next(c for c in load_seeded_cases(CASES) if c.name == "stale_doc_ledger")
    repo = materialise(case, tmp_path)
    log = subprocess.run(
        ["git", "log", "--format=%s", "seed/stale_doc_ledger"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.splitlines()
    assert log == ["stale_doc_ledger: 02-drop-guard", "stale_doc_ledger: 01-add-guard",
                   "stale_doc_ledger: before"]
    # The doc still claims the guard; the final code no longer has it.
    assert "_CODE_RE" in (repo / "docs/CHANGES.md").read_text()
    assert "_CODE_RE" not in (repo / "app/billing/coupons.py").read_text()
    changed = subprocess.run(["git", "diff", "--name-only", "main"], cwd=repo, capture_output=True,
                             text=True, check=True).stdout.split()
    assert changed == ["app/billing/coupons.py", "docs/CHANGES.md"]


def test_dot_prefix_written_as_dotfile(tmp_path):
    d = tmp_path / "c"
    (d / "commits" / "01" / "cfg").mkdir(parents=True)
    (d / "commits" / "01" / "cfg" / "dot_env.local").write_text("A=1\n")
    (d / "expected.json").write_text("[]")
    (d / "meta.json").write_text(json.dumps({"file": "cfg/.env.local"}))
    (case,) = load_seeded_cases(tmp_path)
    assert case.commits == [("01", {"cfg/.env.local": "A=1\n"})]
    assert case.after == "A=1\n"


def test_materialise_ignores_user_global_gitignore(tmp_path, monkeypatch):
    d = tmp_path / "cases" / "c"
    (d / "commits" / "01" / "cfg").mkdir(parents=True)
    (d / "commits" / "01" / "cfg" / "dot_env.local").write_text("A=1\n")
    (d / "expected.json").write_text("[]")
    ignore = tmp_path / "global_ignore"
    ignore.write_text(".env*\n")
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text(f"[core]\n\texcludesFile = {ignore}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    (case,) = load_seeded_cases(tmp_path / "cases")
    repo = materialise(case, tmp_path / "out")
    out = subprocess.run(["git", "diff", "--name-only", "main"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    assert out.split() == ["cfg/.env.local"]


@pytest.mark.parametrize("parts", [("dot_git", "hooks", "post-commit"), ("dot_.", "x.py"),
                                   ("dot_GIT", "config")])
def test_dot_prefix_refuses_escape_and_git_dir(tmp_path, parts):
    d = tmp_path / "c"
    f = d / "commits" / "01" / Path(*parts)
    f.parent.mkdir(parents=True)
    f.write_text("#!/bin/sh\necho hi\n")
    (d / "expected.json").write_text("[]")
    with pytest.raises(SchemaError):
        load_seeded_cases(tmp_path)


def test_multi_commit_layout_validation(tmp_path):
    d = tmp_path / "c"
    (d / "commits" / "01").mkdir(parents=True)
    (d / "expected.json").write_text("[]")
    with pytest.raises(SchemaError):  # empty commit dir
        load_seeded_cases(tmp_path)
    (d / "commits" / "01" / "x.py").write_text("x = 1\n")
    (d / "expected.json").write_text(json.dumps([{"file": "../x.md", "line_start": 1, "line_end": 1,
                                                  "category": "correctness", "severity": "minor",
                                                  "description": "d"}]))
    with pytest.raises(SchemaError):  # expected file escapes the repo
        load_seeded_cases(tmp_path)


def test_run_eval_seeded_fake(tmp_path):
    res = run_eval("seeded", ROOT, backend_name="fake", judge=False, out_dir=tmp_path)
    assert res["ep_recall"] == pytest.approx(25 / 26)
    assert res["precision"] == 1.0 and res["precision_reported"] == 1.0
    assert res["n_unverified"] == 0 and all(c["unverified"] == 0 for c in res["cases"])
    assert res["agreement_rate"] is None and res["judge"] == "none"
    assert len(res["cases"]) == 30
    # Verifier-stage seeds for classes the security verifier's exclusions used to refute.
    # Prefilter doc-scope seeds: a docs-only diff and a finding on a prompt file.
    # deploy_ci seeds: a workflow_run deploy, and a runbook-only diff kept by the path gate.
    # 1.1 release seeds: a prompt-only contradiction between layers, a vacuous test, and a
    # migration the bootstrap schema does not mirror.
    for name in ("committed_env_file", "prompt_injection_grade", "cors_wildcard_credentials",
                 "plan_progress_drift", "prompt_md_kept",
                 "runbook_job_order", "prompt_rule_contradiction", "vacuous_test",
                 "migration_bootstrap_drift"):
        case = next(c for c in res["cases"] if c["name"] == name)
        assert len(case["matched"]) == 1, name
    deploy = next(c for c in res["cases"] if c["name"] == "workflow_run_pr_deploy")
    assert len(deploy["matched"]) == 2  # unchecked trigger, and no concurrency group
    stale = next(c for c in res["cases"] if c["name"] == "stale_doc_ledger")
    assert len(stale["matched"]) == 1  # doc finding with code evidence survives the prefilter
    on_disk = json.loads(Path(res["path"]).read_text())
    for key in ("set", "generated_at", "backend", "judge", "precision", "precision_reported",
                "ep_recall", "snr", "snr_note", "f1", "n_findings", "n_goldens", "n_unverified",
                "per_category_recall",
                "agreement_rate", "cases", "skipped", "failed", "cost_usd"):
        assert key in on_disk
    assert Path(res["path"]).name.endswith("-seeded.json")


def test_golden_requires_repo_path(tmp_path):
    with pytest.raises(ConfigError):
        run_eval("golden", ROOT, backend_name="fake", judge=False, out_dir=tmp_path)


def test_unknown_set(tmp_path):
    with pytest.raises(ConfigError):
        run_eval("nope", ROOT, out_dir=tmp_path)


def test_clean_loader_skips_comments(tmp_path):
    p = tmp_path / "cases.jsonl"
    p.write_text('# comment\n\n{"repo_path": "/x", "base_sha": "a", "head_sha": "b", "note": "n"}\n')
    rows = load_clean_cases(p)
    assert rows == [{"repo_path": "/x", "base_sha": "a", "head_sha": "b", "note": "n"}]
    assert load_clean_cases(ROOT / "eval" / "clean" / "cases.jsonl") == []


def test_cli_eval_seeded(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    rc = main(["eval", "--set", "seeded", "--backend", "fake", "--no-judge", "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "eval seeded: precision=1.000 ep_recall=0.962" in out
    assert "precision_reported=1.000 unverified=0" in out
    assert list(tmp_path.glob("*-seeded.json"))


def test_cli_compare_bundled(capsys):
    assert main(["eval", "--set", "seeded", "--compare-bundled"]) == 0
    assert "not implemented in v1" in capsys.readouterr().out


def test_run_eval_failed_runs_are_not_scored(tmp_path):
    """A case whose every lane failed (rate limit, auth) is reported, not counted as a miss."""
    from preflight_review.model.backend import ModelResult

    class DeadBackend:
        name = "dead"

        def run(self, req):
            return ModelResult(ok=False, structured=None, subtype="error_subprocess",
                               error="429 usage limit")

    res = run_eval("seeded", ROOT, backend=DeadBackend(), judge=False, out_dir=tmp_path)
    assert res["cases"] == []
    assert len(res["failed"]) == 30
    assert all("all lanes failed" in c["reasons"] for c in res["failed"])
    assert res["n_goldens"] == 0



def test_run_eval_paused_runs_are_not_scored(tmp_path):
    """A usage limit after some lanes succeeded pauses the case: failed, not a low score."""
    from preflight_review.model.backend import ModelResult

    class LimitedBackend:
        name = "limited"
        n = 0

        def run(self, req):
            self.n += 1
            if self.n == 1:
                return ModelResult(ok=True, structured={"findings": [], "files_read_outside_packet": []},
                                   subtype="success", num_turns=5)
            return ModelResult(ok=False, structured=None, subtype="error_subprocess",
                               error="You've hit your 5-hour limit")

    res = run_eval("seeded", ROOT, backend=LimitedBackend(), judge=False, out_dir=tmp_path)
    assert res["cases"] == [] and res["n_goldens"] == 0
    assert len(res["failed"]) == 30
    assert any(r.startswith("paused: usage limit") for r in res["failed"][0]["reasons"])


def _deploy_case_with_unknown_verifier(tmp_path, noise: bool):
    """Run workflow_run_pr_deploy with the general verifier answering UNKNOWN, so the
    concurrency finding lands in Unverified (the security finding stays confirmed)."""
    from preflight_review.model import FakeBackend

    cases = tmp_path / "cases"
    cases.mkdir()
    name = "workflow_run_pr_deploy"
    subprocess.run(["cp", "-R", str(CASES / name), str(cases / name)], check=True)
    responses = json.loads(FAKE.read_text())["__per_case__"][name]
    responses["verifier"] = dict(responses["verifier"], verdict="UNKNOWN", evidence=[])
    if noise:  # the unverified finding then matches no golden: a false positive
        responses["finder:deploy_ci"]["findings"][1]["category"] = "maintainability"
    p = tmp_path / "fake.json"
    p.write_text(json.dumps(responses))
    return run_eval("seeded", ROOT, backend=FakeBackend.from_file(p), judge=False,
                    cases_dir=cases, out_dir=tmp_path / "out")


def test_run_eval_counts_unverified_separately(tmp_path):
    res = _deploy_case_with_unknown_verifier(tmp_path, noise=True)
    assert res["cases"][0]["unverified"] == 1 and res["n_unverified"] == 1
    assert res["n_findings"] == 2
    assert res["precision"] == 0.5  # incl. unverified: the UNKNOWN noise counts against it
    assert res["precision_reported"] == 1.0  # report only: the confirmed finding matched
    assert res["ep_recall"] == 0.5


def test_run_eval_unverified_golden_counts_for_recall(tmp_path):
    res = _deploy_case_with_unknown_verifier(tmp_path, noise=False)
    assert res["n_unverified"] == 1 and len(res["cases"][0]["matched"]) == 2
    assert res["ep_recall"] == 1.0  # a golden that lands in Unverified was still surfaced


def test_eval_options_always_review_in_full(tmp_path):
    """An eval never reads the reviewed-file cache: every case is reviewed from scratch."""
    from preflight_review.evaluation.runner import _options

    opts = _options(tmp_path, "main", "fake", None)
    assert opts.full is True and opts.yes is True
    assert _options(tmp_path, "main", "fake", {"mode": "quick"}).full is True
