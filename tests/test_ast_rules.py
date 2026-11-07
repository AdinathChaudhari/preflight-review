from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest
import yaml

from preflight_review.init_templates import init_repo
from tests.conftest import make_repo

ROOT = Path(str(resources.files("preflight_review").joinpath("ast_rules")))
RULES_DIR = ROOT / "rules"
TESTS = ROOT / "tests"
RULES = sorted(RULES_DIR.glob("*.yml"))


def test_generic_rules_packaged():
    assert [p.stem for p in RULES] == ["no-shell-true", "no-yaml-unsafe-load"]


@pytest.mark.parametrize("path", RULES, ids=lambda p: p.stem)
def test_rule_shape(path):
    d = yaml.safe_load(path.read_text())
    for key in ("id", "language", "severity", "message", "rule"):
        assert key in d, key
    assert d["language"] == "python"
    assert d["id"] == path.stem


@pytest.mark.parametrize("path", RULES, ids=lambda p: p.stem)
def test_rule_has_test_cases(path):
    t = yaml.safe_load((TESTS / f"{path.stem}-test.yml").read_text())
    assert t["id"] == path.stem
    assert t["valid"] and t["invalid"]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_ast_grep_rule_tests_pass(tmp_path):
    cfg = tmp_path / "sgconfig.yml"
    cfg.write_text(
        yaml.safe_dump({"ruleDirs": [str(RULES_DIR)], "testConfigs": [{"testDir": str(TESTS)}]})
    )
    r = subprocess.run(
        ["ast-grep", "test", "-c", str(cfg), "--skip-snapshot-tests"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_init_with_ast_rules_copies_the_bundled_rules(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    res = init_repo(repo, with_ast_rules=True)
    names = sorted(p.name for p in (repo / ".preflight" / "ast-rules").iterdir())
    assert names == [p.name for p in RULES] and len(names) == 2
    assert "installed 2 bundled ast-grep rule(s)" in res.message


def test_init_without_flag_copies_none(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    init_repo(repo)
    assert list((repo / ".preflight" / "ast-rules").iterdir()) == []


def test_init_ast_rules_respects_force(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    init_repo(repo, with_ast_rules=True)
    target = repo / ".preflight" / "ast-rules" / "no-shell-true.yml"
    target.write_text("custom")
    res = init_repo(repo, with_ast_rules=True)
    assert target.read_text() == "custom" and target in res.skipped
    init_repo(repo, force=True, with_ast_rules=True)
    assert target.read_text() != "custom"
