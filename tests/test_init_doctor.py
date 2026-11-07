import shutil
from pathlib import Path

import pytest

from preflight_review.config import load_config
from preflight_review.context.learnings import parse_learnings
from preflight_review.doctor import Check, doctor_ok, format_checks, run_doctor
from preflight_review.init_templates import (
    LEARNINGS_MD,
    PREFLIGHT_GITIGNORE,
    init_repo,
)

from tests.conftest import _git, make_repo


def _fake_which(name):
    return None if name in ("ruff", "ast-grep") else f"/usr/bin/{name}"


def test_init_creates_everything(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    res = init_repo(repo)
    pf = repo / ".preflight"
    for name in ("config.yaml", "RULES.md", "learnings.md", ".gitignore"):
        assert (pf / name).is_file()
    assert (pf / "ast-rules").is_dir()
    assert len(res.created) == 5 and res.skipped == []
    assert (pf / ".gitignore").read_text() == "reviews/\nstate.json\ncache/\nintent.md\nruns/\n"
    assert "created" in res.message


def test_learnings_template_parses_empty():
    assert parse_learnings(LEARNINGS_MD) == []
    assert PREFLIGHT_GITIGNORE.count("\n") == 5


def test_init_idempotent(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    init_repo(repo)
    res = init_repo(repo)
    assert res.created == []
    assert len(res.skipped) == 5


def test_force_overwrites(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    init_repo(repo)
    rules = repo / ".preflight" / "RULES.md"
    rules.write_text("custom")
    res = init_repo(repo, force=True)
    assert rules in res.created
    assert rules.read_text().startswith("# Repo rules")


def test_coderabbit_import(tmp_path):
    cr = (
        "reviews:\n  profile: chill\n  path_filters:\n    - '!**/gen/**'\n"
        "  path_instructions:\n    - path: 'migrations/**'\n      instructions: 'Check reversibility'\n"
    )
    repo = make_repo(tmp_path, {"a.py": "x = 1\n", ".coderabbit.yaml": cr})
    res = init_repo(repo)
    assert res.coderabbit_imported
    text = (repo / ".preflight" / "config.yaml").read_text()
    assert text.startswith("# preflight-review config (imported from .coderabbit.yaml)")
    cfg = load_config(repo, None)
    assert cfg.profile == "assertive"  # CodeRabbit's profile is not imported
    assert cfg.path_filters == ["!**/gen/**"]
    assert cfg.path_instructions[0].path == "migrations/**"
    assert cfg.path_instructions[0].instructions == "Check reversibility"


def test_guideline_files(tmp_path):
    repo = make_repo(
        tmp_path, {"CLAUDE.md": "# c\n", ".claude/rules/x.md": "r\n", "a.py": "x\n"}
    )
    res = init_repo(repo)
    assert res.guideline_files == [".claude/rules/x.md", "CLAUDE.md"]
    assert "CLAUDE.md" in res.message


def test_doctor_ok_with_missing_optional(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "2.1.314")
    by = {c.name: c for c in checks}
    for n in ("git", "config", "base", "claude"):
        assert by[n].ok and by[n].required
    assert by["config"].detail == "not present; defaults apply"
    assert by["claude"].detail == "2.1.314"
    assert not by["ruff"].ok and not by["ruff"].required
    assert "not installed" in by["ruff"].detail
    assert "uv tool install ruff" in by["ruff"].detail
    assert doctor_ok(checks)
    out = format_checks(checks)
    assert "[ok] git:" in out
    assert "[--] ruff: not installed (install: uv tool install ruff)" in out


def test_doctor_skips_model_probe_by_default(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "2.1.314")
    assert "adjudicator model" not in {c.name for c in checks}


def test_doctor_probes_adjudicator_alias(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    seen = []

    def probe(alias, cwd):
        seen.append(alias)
        return True, f"{alias} -> claude-fable-x"

    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "2.1.314",
                        model_probe_fn=probe)
    by = {c.name: c for c in checks}
    assert seen == ["opus"] and by["adjudicator model"].ok
    assert not by["adjudicator model"].required


def test_doctor_unavailable_adjudicator_is_optional_and_names_fallback(tmp_path):
    repo = make_repo(tmp_path, {
        "a.py": "x = 1\n", ".preflight/config.yaml": "version: 1\nmodels:\n  adjudicator: fable\n",
    })
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "2.1.314",
                        model_probe_fn=lambda alias, cwd: (False, "model not found"))
    by = {c.name: c for c in checks}
    assert not by["adjudicator model"].ok and doctor_ok(checks)
    assert by["adjudicator model"].detail == (
        "fable unavailable (model not found); split votes adjudicate on opus"
    )


def test_doctor_no_model_probe_without_claude(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: None,
                        model_probe_fn=lambda alias, cwd: (True, "x"))
    assert "adjudicator model" not in {c.name for c in checks}


def test_doctor_claude_missing(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: None)
    assert not doctor_ok(checks)
    assert "[FAIL] claude:" in format_checks(checks)


def test_doctor_broken_config(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / ".preflight").mkdir()
    (repo / ".preflight" / "config.yaml").write_text("profile: [unclosed\n")
    checks = run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "2.1.314")
    by = {c.name: c for c in checks}
    assert not by["config"].ok
    assert not doctor_ok(checks)


def test_doctor_not_a_repo(tmp_path):
    checks = run_doctor(tmp_path, which=_fake_which, claude_version_fn=lambda: "1")
    assert not checks[0].ok
    assert isinstance(checks[0], Check)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_doctor_real_machine():
    repo = Path(__file__).resolve().parent.parent
    checks = run_doctor(repo)
    for c in checks:
        if c.required:
            assert c.ok, f"{c.name}: {c.detail}"


def test_rules_template_has_stale_doc_and_rerun_rules():
    from preflight_review.init_templates import RULES_MD

    assert "removes or renames" in RULES_MD and "changed docs" in RULES_MD
    assert "preflight-review review --incremental" in RULES_MD


def test_doctor_reads_config_from_base_branch(tmp_path):
    # review reads .preflight/config.yaml from the base branch; doctor checks the same file.
    repo = make_repo(tmp_path, {"a.py": "x = 1\n",
                                ".preflight/config.yaml": "version: 1\nprofile: chill\n"})
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / ".preflight" / "config.yaml").write_text("version: 1\nprofile: assertive\n")
    by = {c.name: c for c in run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "1")}
    assert by["config"].ok
    assert by["config"].detail == ".preflight/config.yaml ok (read from main, as review reads it)"
    assert "profile: chill" in by["config defaults"].detail


def test_doctor_falls_back_to_working_tree_config_with_note(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    (repo / ".preflight").mkdir()
    (repo / ".preflight" / "config.yaml").write_text("version: 1\nprofile: chill\n")
    by = {c.name: c for c in run_doctor(repo, which=_fake_which, claude_version_fn=lambda: "1")}
    assert by["config"].ok
    assert by["config"].detail.startswith(".preflight/config.yaml not on main, so review uses")
    assert "working-tree file" in by["config"].detail
    assert "profile: chill" in by["config defaults"].detail
