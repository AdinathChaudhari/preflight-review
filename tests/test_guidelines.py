"""Guideline discovery."""

from __future__ import annotations

from preflight_review.config import Config, PathInstruction
from preflight_review.context.guidelines import discover_guidelines

from tests.conftest import branch, make_repo, write


def test_ancestor_claude_md_up_to_home_inclusive(tmp_path):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("home rules")
    (home / "a" / "CLAUDE.md").write_text("a rules")
    (tmp_path / "CLAUDE.md").write_text("ABOVE HOME")
    repo = make_repo(home / "a" / "b", {"x.py": "x = 1\n"})
    gs = discover_guidelines(repo, ["x.py"], Config(), None, home=home)
    texts = [g.text for g in gs]
    assert texts == ["home rules", "a rules"]
    assert "ABOVE HOME" not in texts
    assert all(g.kind == "claude_md" for g in gs)
    assert gs[0].source_path == str(home / "CLAUDE.md")


def test_repo_outside_home_walks_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "CLAUDE.md").write_text("outside")
    (tmp_path / "other").mkdir()
    repo = make_repo(tmp_path / "other", {"x.py": "x\n"})
    assert discover_guidelines(repo, ["x.py"], Config(), None, home=home) == []


def test_nested_claude_md_applies_only_under_changed_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(
        tmp_path,
        {"CLAUDE.md": "root", "app/CLAUDE.md": "app", "lib/CLAUDE.md": "lib", "app/m.py": "1\n"},
    )
    under = discover_guidelines(repo, ["app/m.py"], Config(), None, home=home)
    assert [(g.source_path, g.scope_dir) for g in under] == [("CLAUDE.md", ""), ("app/CLAUDE.md", "app")]
    other = discover_guidelines(repo, ["lib/z.py"], Config(), None, home=home)
    assert [g.source_path for g in other] == ["CLAUDE.md", "lib/CLAUDE.md"]


def test_claude_rules_agents_cursorrules(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(
        tmp_path,
        {
            ".claude/rules/b.md": "B",
            ".claude/rules/a.md": "A",
            "AGENTS.md": "agents",
            ".cursorrules": "cursor",
            "x.py": "1\n",
        },
    )
    gs = discover_guidelines(repo, ["x.py"], Config(), None, home=home)
    assert [(g.source_path, g.kind) for g in gs] == [
        ("AGENTS.md", "agents_md"),
        (".claude/rules/a.md", "rules_dir"),
        (".claude/rules/b.md", "rules_dir"),
        (".cursorrules", "cursorrules"),
    ]


def test_rules_file_read_from_base_not_worktree(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path, {".preflight/RULES.md": "version A", "x.py": "1\n"})
    branch(repo, "feature")
    write(repo, ".preflight/RULES.md", "version B")
    gs = discover_guidelines(repo, ["x.py"], Config(), "main", home=home)
    rules = [g for g in gs if g.kind == "preflight_rules"]
    assert [g.text for g in rules] == ["version A"]


def test_base_ref_none_skips_rules_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path, {".preflight/RULES.md": "A", "x.py": "1\n"})
    gs = discover_guidelines(repo, ["x.py"], Config(), None, home=home)
    assert not [g for g in gs if g.kind == "preflight_rules"]


def test_path_instruction_only_when_glob_matches(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path, {"x.py": "1\n"})
    cfg = Config(path_instructions=[
        PathInstruction("migrations/**", "check reversibility"),
        PathInstruction("app/**", "app rule"),
    ])
    gs = discover_guidelines(repo, ["migrations/001.sql"], cfg, None, home=home)
    assert [(g.kind, g.scope_dir, g.text) for g in gs] == [
        ("path_instruction", "migrations/**", "check reversibility")
    ]
    assert gs[0].source_path == "config:path_instructions"


def test_truncation_marker(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path, {"CLAUDE.md": "word " * 10_000, "x.py": "1\n"})
    (g,) = discover_guidelines(repo, ["x.py"], Config(), None, home=home)
    assert g.text.endswith("\n[truncated]")
    assert len(g.text) < 25_000


def test_discover_disabled_and_stable_order(tmp_path):
    home = tmp_path / "home"
    (home / "p").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("home")
    repo = make_repo(home / "p", {"CLAUDE.md": "root", "b/CLAUDE.md": "b", "a/CLAUDE.md": "a",
                                  ".preflight/RULES.md": "rules"})
    paths = ["b/x.py", "a/y.py"]
    gs = discover_guidelines(repo, paths, Config(), "main", home=home)
    assert [g.text for g in gs] == ["home", "root", "a", "b", "rules"]
    assert gs == discover_guidelines(repo, paths, Config(), "main", home=home)
    cfg = Config()
    cfg.guidelines.discover = False
    assert [g.kind for g in discover_guidelines(repo, paths, cfg, "main", home=home)] == ["preflight_rules"]
