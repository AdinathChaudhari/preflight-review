"""mode: auto (depth from changed paths) and the pre-run estimate gate."""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess

import pytest
import yaml

from preflight_review import cli, naming, pipeline
from preflight_review.config import Config, config_from_dict, merge_overrides
from preflight_review.depth import (
    MODES,
    crucial_paths,
    deep_eligible,
    depth_menu,
    estimate_run,
    parse_choice,
    resolve_mode,
)
from preflight_review.errors import ConfigError
from preflight_review.model import FakeBackend
from preflight_review.pipeline import (
    ReviewOptions,
    choose_tty,
    run_review,
    summary_line,
)
from preflight_review.state import load_state

from .conftest import branch, commit, make_repo

EMPTY_FINDER = {"findings": [], "files_read_outside_packet": []}
CODE = "def f(x):\n    return x + 1\n"
CODE2 = "def f(x):\n    return x + 2\n"


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def _repo(tmp_path, changed: str, config: dict | None = None):
    base = {changed: CODE, "README.md": "hi\n"}
    if config is not None:
        base[".preflight/config.yaml"] = yaml.safe_dump(config)
    r = make_repo(tmp_path, base)
    branch(r, "feature/x")
    commit(r, {changed: CODE2}, "change")
    return r


def _review(repo, be=None, choose=None, **kw):
    be = be if be is not None else FakeBackend(default=EMPTY_FINDER)
    opts = ReviewOptions(repo_root=repo, base="main", **kw)
    extra = {"choose": choose} if choose is not None else {}
    run, paths = run_review(opts, backend=be, gh_runner=no_gh, **extra)
    return run, paths, be


def _never_ask(menu):  # pragma: no cover - fails the test if called
    raise AssertionError(f"asked: {menu}")


# ------------------------------------------------------------------- resolution


@pytest.mark.parametrize("path", [
    "db/migrations/0015.py", "alembic/versions/a1.py", "schema.sql", "app/auth/login.py",
    "app/oauth_client.py", "src/security/x.py", "app/permissions.py", ".env.local",
    "config/app_secrets.yaml", ".github/workflows/ci.yml", "docker/Dockerfile.prod",
    "deploy/run.yaml", "scripts/release.sh", "app/prompts/system.md", "t/layer.jinja2",
    "AGENTS.md", "pkg/CLAUDE.md", "skills/x/SKILL.md", "app/evaluation/scoring.py",
    "app/grading.py", "svc/billing/invoice.py", "svc/payments/charge.py",
])
def test_default_deep_paths_match(path):
    assert crucial_paths(Config(), [path]) == [path]


@pytest.mark.parametrize("path", ["app/service.py", "README.md", "docs/guide.md", "tests/test_x.py"])
def test_ordinary_paths_are_not_crucial(path):
    assert crucial_paths(Config(), [path]) == []


@pytest.mark.parametrize("path", [
    "tests/fixtures/migrations/0001.py", "eval/seeded/case/db/migrations/0002.py",
    "eval/seeded/case/.github/workflows/ci.yml", "app/fixtures/schema.sql",
    "test/auth/test_login.py", "docs/deploy/runbook.sh", "pkg/tests/prompts/x.md",
    "app/auth/test_tokens.py", "spec/security/x.py",
])
def test_tests_eval_fixtures_and_docs_never_go_deep(path):
    assert not deep_eligible(path)
    assert crucial_paths(Config(), [path]) == []
    assert resolve_mode(Config(), [path, "app/service.py"]) == ("standard", [])


def test_agent_instructions_outside_tests_still_go_deep():
    assert crucial_paths(Config(), ["CLAUDE.md", "app/prompts/system.md"]) == [
        "CLAUDE.md", "app/prompts/system.md"
    ]


def test_resolve_mode_auto_and_explicit():
    paths = ["app/service.py", "db/migrations/0015.py"]
    assert resolve_mode(Config(), paths) == ("deep", ["db/migrations/0015.py"])
    assert resolve_mode(Config(), ["app/service.py"]) == ("standard", [])
    for mode in ("quick", "standard", "deep"):
        assert resolve_mode(merge_overrides(Config(), mode=mode), paths) == (mode, [])


def test_config_auto_round_trip_and_validation():
    c = config_from_dict({"mode": "auto", "confirm_above_usd": None,
                          "auto": {"deep_paths": ["core/**"]}})
    assert c.confirm_above_usd is None and c.auto.deep_paths == ["core/**"]
    assert resolve_mode(c, ["core/x.py"])[0] == "deep"
    assert resolve_mode(c, ["db/migrations/1.py"])[0] == "standard"
    assert config_from_dict({"confirm_above_usd": 5}).confirm_above_usd == 5.0
    with pytest.raises(ConfigError, match="mode"):
        config_from_dict({"mode": "thorough"})


def test_estimate_scales_with_mode(tmp_path):
    r = _repo(tmp_path, "app/service.py")
    run, _, _ = _review(r, mode="quick")
    scope = run.scope
    est = {m: estimate_run(scope, Config(), m, 5, 12_000) for m in ("quick", "standard", "deep")}
    assert est["quick"].usd < est["standard"].usd < est["deep"].usd
    assert est["deep"].sweep_calls == 1 and est["standard"].sweep_calls == 0
    assert est["deep"].security_calls == 1 and est["deep"].finder_calls == 4
    assert "~" in est["deep"].calls_text() and "1 sweep" in est["deep"].calls_text()
    # Sonnet costs less than Opus for the same plan.
    opus = merge_overrides(Config(), models=dataclasses.replace(
        Config().models, finder="opus", finder_deep="opus", security="opus", sweep="opus",
    ))
    assert estimate_run(scope, opus, "deep", 5, 12_000).usd > est["deep"].usd
    assert est["quick"].minutes <= est["standard"].minutes <= est["deep"].minutes
    assert est["deep"].batches == 1 and est["deep"].files == 1


# -------------------------------------------------------------------- pipeline


def test_auto_runs_standard_without_crucial_paths(tmp_path):
    r = _repo(tmp_path, "app/service.py")
    run, paths, be = _review(r)
    # Depth is per packet: the run keeps `auto`, its one packet ran standard.
    assert run.config.mode == "auto" and run.outcome == "complete"
    assert set(run.batch_modes.values()) == {"standard"}
    assert not any(c.role.startswith("sweep") for c in be.calls)
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["mode_requested"] == "auto" and manifest["mode_resolved"] == "standard"
    assert manifest["crucial_paths"] == [] and manifest["estimate"]["files"] == 1
    assert (
        "Mode: auto -> standard for all 1 packet (no crucial paths changed)"
        in paths["md"].read_text()
    )


def test_auto_runs_deep_on_crucial_path(tmp_path):
    r = _repo(tmp_path, "db/migrations/0015.py")
    run, paths, be = _review(r)
    assert run.config.mode == "auto" and set(run.batch_modes.values()) == {"deep"}
    assert any(c.role.startswith("sweep") for c in be.calls)
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["mode_resolved"] == "deep"
    assert manifest["crucial_paths"] == ["db/migrations/0015.py"]
    assert (
        "Mode: auto -> deep for all 1 packet (crucial paths: db/migrations/0015.py)"
        in paths["md"].read_text()
    )


def test_explicit_mode_wins_over_auto(tmp_path):
    r = _repo(tmp_path, "db/migrations/0015.py")
    run, paths, _ = _review(r, mode="quick")
    assert run.config.mode == "quick" and run.crucial_paths == []
    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["mode_requested"] == "quick" and manifest["mode_resolved"] == "quick"
    assert "Mode: quick (set explicitly)" in paths["md"].read_text()


def test_config_mode_wins_over_auto(tmp_path):
    r = _repo(tmp_path, "app/service.py", {"version": 1, "mode": "deep"})
    run, _, _ = _review(r)
    assert run.config.mode == "deep"


def test_manifest_caps_crucial_paths(tmp_path):
    r = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(r, "feature/x")
    commit(r, {f"db/migrations/{i:04d}.py": CODE for i in range(12)}, "many")
    run, paths, _ = _review(r, yes=True)
    assert len(run.crucial_paths) == 12
    assert len(json.loads(paths["manifest"].read_text())["crucial_paths"]) == 10
    assert "+9 more" in paths["md"].read_text()


# ---------------------------------------------------------------- estimate gate

LOW = {"version": 1, "confirm_above_usd": 0.01}


def test_gate_blocks_without_terminal(tmp_path):
    r = _repo(tmp_path, "db/migrations/0015.py", LOW)
    be = FakeBackend(default=EMPTY_FINDER)
    run, paths, _ = _review(r, be, choose=lambda menu: None, include_worktree=False)
    assert be.calls == []
    assert run.outcome == "not_started"
    assert set(run.mode_estimates) == {*MODES, "auto"}
    md = paths["md"].read_text()
    assert "Result: not started" in md and "no terminal to choose" in md
    for m in (*MODES, "auto"):
        assert f"preflight-review review --base main --committed-only --mode {m} --yes" in md
        assert f"- {m}: ~$" in md
    assert "Mode: auto (deep for packets with crucial paths: db/migrations/0015.py" in md
    data = json.loads(paths["json"].read_text())
    assert data["outcome"] == "not_started" and data["manifest"]["estimate"]["usd"] > 0
    assert data["manifest"]["estimate"]["calls"] > 0
    line = summary_line(run, paths)
    assert line.startswith("preflight: not started:")
    assert "re-run with --mode quick|standard|deep|auto --yes" in line and "quick ~$" in line
    # No round is consumed: the next run writes round 01.
    assert load_state(r).branches.get("feature/x") is None or not load_state(r).branches[
        "feature/x"
    ].last_report
    run2, _, be2 = _review(r, yes=True)
    assert run2.outcome == "complete" and run2.round == 1 and be2.calls


def test_gate_passes_with_yes(tmp_path):
    r = _repo(tmp_path, "app/service.py", LOW)
    run, _, be = _review(r, choose=_never_ask, yes=True)
    assert run.outcome == "complete" and be.calls and run.config.mode == "auto"
    assert set(run.batch_modes.values()) == {"standard"}


def test_gate_off_when_threshold_null(tmp_path):
    r = _repo(tmp_path, "app/service.py", {"version": 1, "confirm_above_usd": None})
    run, _, be = _review(r, choose=_never_ask)
    assert run.outcome == "complete" and be.calls
    assert any(n.startswith("pre-run estimate: ~$") for n in run.notes)


def test_gate_not_asked_below_threshold(tmp_path):
    r = _repo(tmp_path, "app/service.py")
    run, _, be = _review(r, choose=_never_ask)
    assert run.outcome == "complete" and be.calls


def test_menu_enter_runs_the_auto_mode(tmp_path):
    r = _repo(tmp_path, "db/migrations/0015.py", LOW)
    menus: list[str] = []

    def enter(menu):
        menus.append(menu)
        return "\n"

    run, paths, be = _review(r, choose=enter)
    assert run.outcome == "complete" and run.config.mode == "auto" and be.calls
    assert set(run.batch_modes.values()) == {"deep"}
    menu = menus[0]
    assert menu.startswith("preflight: 1 files, 1 batch. Choose depth:")
    lines = menu.splitlines()
    assert lines[1].lstrip().startswith("1) quick") and "~$" in lines[1] and "calls" in lines[1]
    assert lines[3].lstrip().startswith("3) deep") and "sweep" in lines[3]
    assert "no sweep" in lines[2]
    # Under auto the default is option 4: per-packet depth.
    assert lines[4].lstrip().startswith("4) auto") and lines[4].endswith("(auto)")
    assert lines[5].strip() == "5) cancel" and lines[6] == "Choose [4]: "
    assert "Mode: auto -> deep for all 1 packet (crucial paths" in paths["md"].read_text()


def test_menu_pick_switches_mode(tmp_path):
    r = _repo(tmp_path, "db/migrations/0015.py", LOW)
    run, paths, be = _review(r, choose=lambda menu: "1")
    assert run.outcome == "complete" and run.config.mode == "quick"
    assert not any(c.role.startswith("sweep") for c in be.calls)
    assert any("depth chosen at the pre-run menu: quick (was auto)" in n for n in run.notes)
    assert set(run.batch_modes.values()) == {"quick"}
    assert "Mode: quick (chosen at the pre-run depth menu)" in paths["md"].read_text()
    assert json.loads(paths["manifest"].read_text())["mode_requested"] == "menu"


def test_menu_cancel_does_not_start(tmp_path):
    r = _repo(tmp_path, "app/service.py", LOW)
    run, paths, be = _review(r, choose=lambda menu: "5")  # 4 is auto under auto
    assert run.outcome == "not_started" and be.calls == []
    assert "cancelled at the menu" in paths["md"].read_text()


@pytest.mark.parametrize("answer,expected", [
    ("", "standard"), ("\n", "standard"), ("1", "quick"), ("3", "deep"), ("deep", "deep"),
    (" Quick ", "quick"), ("4", "cancel"), ("n", "cancel"), ("9", "cancel"), ("yes", "cancel"),
])
def test_parse_choice(answer, expected):
    assert parse_choice(answer, "standard") == expected


def test_depth_menu_marks_configured_default(tmp_path):
    r = _repo(tmp_path, "app/service.py")
    run, _, _ = _review(r, mode="quick")
    est = {m: estimate_run(run.scope, Config(), m, 5, 12_000) for m in MODES}
    menu = depth_menu(Config(), est, "standard", auto=False)
    assert "(configured)" in menu and "(auto)" not in menu and menu.endswith("Choose [2]: ")


def test_choose_tty_without_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert choose_tty("menu") is None


def test_cli_not_started_exits_1_without_model_calls(tmp_path, monkeypatch, capsys):
    r = _repo(tmp_path, "db/migrations/0015.py", LOW)
    monkeypatch.chdir(r)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(
        pipeline, "pr_label", lambda root, br, gh_runner=None: naming.pr_label(root, br, no_gh)
    )
    calls: list = []
    real = pipeline.select_backend

    def tracking(opts, config):
        be = real(opts, config)
        calls.append(be)
        return be

    monkeypatch.setattr(pipeline, "select_backend", tracking)
    assert cli.main(["review", "--backend", "fake", "--base", "main"]) == 1
    out = capsys.readouterr()
    assert "preflight: not started:" in out.out
    assert "incomplete:" not in out.err
    assert all(be.calls == [] for be in calls)
    assert cli.main(["review", "--backend", "fake", "--base", "main", "--yes"]) == 0


def test_legacy_note_reads_the_file_config_not_resolved_mode(tmp_path):
    r = _repo(tmp_path, "app/service.py", config={"version": 1})
    run, _, _ = _review(r, yes=True)
    assert run.config.mode == "auto" and set(run.batch_modes.values()) == {"standard"}
    assert not any("1.0 defaults" in n for n in run.notes)
    run, _, _ = _review(r, yes=True, full=True, mode="standard")  # CLI flag, not a file key
    assert not any("1.0 defaults" in n for n in run.notes)
    (tmp_path / "pin").mkdir()
    r2 = _repo(tmp_path / "pin", "app/service.py", config={"version": 1, "mode": "standard"})
    run, _, _ = _review(r2, yes=True)
    assert any("mode: standard" in n for n in run.notes)
