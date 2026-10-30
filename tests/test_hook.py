"""Pre-push hook install/uninstall and a live (local bare remote) push."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from preflight_review import cli
from preflight_review.errors import PreflightError
from preflight_review.hookinstall import HOOK_MARKER, install_hook, uninstall_hook

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

FIXTURES = Path(__file__).parent / "fixtures" / "fake"


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, env=env or _env(), capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path, {"app/service.py": SERVICE_BASE})


def test_install_writes_executable_hook_with_marker(repo):
    result = install_hook(repo)
    hook = repo / ".git" / "hooks" / "pre-push"
    assert result.path == hook
    assert os.access(hook, os.X_OK)
    assert HOOK_MARKER in hook.read_text().splitlines()[:3]


def test_reinstall_upgrades_in_place(repo):
    install_hook(repo)
    result = install_hook(repo)
    assert "upgraded" in result.message
    assert not (repo / ".git" / "hooks" / "pre-push.pre-preflight").exists()


def test_foreign_hook_is_moved_and_chained(repo, tmp_path):
    sentinel = tmp_path / "sentinel"
    foreign = repo / ".git" / "hooks" / "pre-push"
    foreign.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 0\n")
    foreign.chmod(0o755)
    install_hook(repo)
    assert (repo / ".git" / "hooks" / "pre-push.pre-preflight").exists()
    bare = _remote(tmp_path, repo)
    _git(repo, "push", "-q", "origin", "main", env=_env(PREFLIGHT="0"))
    assert sentinel.exists()
    assert (bare / "HEAD").exists()


def test_foreign_hook_exit_code_blocks_push(repo, tmp_path):
    foreign = repo / ".git" / "hooks" / "pre-push"
    foreign.write_text("#!/bin/sh\nexit 3\n")
    foreign.chmod(0o755)
    install_hook(repo)
    bare = _remote(tmp_path, repo)
    proc = _git(repo, "push", "origin", "main", env=_env(PREFLIGHT="0"), check=False)
    assert proc.returncode != 0
    assert _git(bare, "rev-parse", "main", check=False).returncode != 0


def test_uninstall_restores_foreign_hook_byte_for_byte(repo):
    foreign = repo / ".git" / "hooks" / "pre-push"
    body = "#!/bin/sh\n# mine\nexit 0\n"
    foreign.write_text(body)
    foreign.chmod(0o755)
    install_hook(repo)
    msg = uninstall_hook(repo)
    assert "restored" in msg
    assert foreign.read_bytes() == body.encode()
    assert not (repo / ".git" / "hooks" / "pre-push.pre-preflight").exists()


def test_uninstall_edge_cases(repo):
    assert "nothing to uninstall" in uninstall_hook(repo)
    foreign = repo / ".git" / "hooks" / "pre-push"
    foreign.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(PreflightError):
        uninstall_hook(repo)


def test_chained_backup_collision_needs_force(repo):
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push.pre-preflight").write_text("#!/bin/sh\nexit 0\n")
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(PreflightError):
        install_hook(repo)
    install_hook(repo, force=True)
    assert HOOK_MARKER in (hooks / "pre-push").read_text()


def test_core_hooks_path_respected(repo):
    _git(repo, "config", "core.hooksPath", "myhooks")
    result = install_hook(repo)
    assert result.path == repo / "myhooks" / "pre-push"
    assert result.path.exists()
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_cli_install_and_uninstall_hook(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["install-hook"]) == 0
    assert "pre-push" in capsys.readouterr().out
    assert (repo / ".git" / "hooks" / "pre-push").exists()
    assert cli.main(["uninstall-hook"]) == 0
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_cli_init_with_ast_rules(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["init", "--with-ast-rules"]) == 0
    ymls = list((repo / ".preflight" / "ast-rules").glob("*.yml"))
    assert len(ymls) == 2, ymls


def _remote(tmp_path: Path, repo: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=_env())
    _git(repo, "remote", "add", "origin", str(bare))
    return bare


def _live_repo(tmp_path, repo):
    bare = _remote(tmp_path, repo)
    _git(repo, "push", "-q", "origin", "main", env=_env(PREFLIGHT="0"))
    _git(repo, "fetch", "-q", "origin")
    branch(repo, "feature")
    commit(repo, {"app/service.py": SERVICE_BAD}, "feature change")
    install_hook(repo)
    return bare


def _reports(repo: Path) -> list[Path]:
    return sorted((repo / ".preflight" / "reviews").glob("*-preflight-review-*.md"))


def test_live_push_writes_report_and_never_blocks(repo, tmp_path):
    bare = _live_repo(tmp_path, repo)
    env = _env(
        PREFLIGHT_REVIEW_CMD=f"{sys.executable} -m preflight_review",
        PREFLIGHT_REVIEW_ARGS="--backend fake",
        PREFLIGHT_FAKE_RESPONSES=str(FIXTURES / "critical.json"),
    )
    env.pop("PREFLIGHT", None)
    proc = _git(repo, "push", "origin", "feature", env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert _git(bare, "rev-parse", "feature").returncode == 0
    reports = _reports(repo)
    assert any(r.name.endswith("-preflight-review-01.md") for r in reports)
    assert "preflight:" in proc.stdout + proc.stderr


def test_preflight_zero_skips(repo, tmp_path):
    _live_repo(tmp_path, repo)
    before = _reports(repo)
    proc = _git(repo, "push", "origin", "feature", env=_env(PREFLIGHT="0"), check=False)
    assert proc.returncode == 0, proc.stderr
    assert "preflight: skipped (PREFLIGHT=0)" in proc.stdout + proc.stderr
    assert _reports(repo) == before
