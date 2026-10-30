"""Shared test helpers: throwaway git repos and a fake backend fixture."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from preflight_review.model import FakeBackend

_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout


def write(repo: Path, path: str, text: str) -> None:
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for path, text in files.items():
        write(repo, path, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", "-b", name)


def make_repo(tmp_path: Path, files: dict[str, str], base_branch: str = "main") -> Path:
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", base_branch)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    commit(repo, files, "base")
    return repo


@pytest.fixture(autouse=True)
def _isolated_git_env(monkeypatch):
    """Library code shells out to git: keep it independent of global config."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()
