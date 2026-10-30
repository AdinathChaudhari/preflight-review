"""Deterministic context packer."""

from __future__ import annotations

import pytest

from preflight_review.config import Config
from preflight_review.context.callers import CallSite
from preflight_review.context.cochange import Companion
from preflight_review.context.guidelines import Guideline
from preflight_review.context.intent import Intent
from preflight_review.context.learnings import Learning
from preflight_review.context.packer import build_packet
from preflight_review.context.symbols import RepoIndex, Symbol
from preflight_review.errors import PacketBudgetError
from preflight_review.findings import Category, Finding, Lane, Severity
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit, make_repo

BODY = "\n".join(f"    x{i} = {i}" for i in range(30))
SRC = f"def helper(a):\n    return a\n\n\ndef main(a):\n{BODY}\n    return helper(a)\n"


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"app.py": SRC, "other.py": "y = 1\n", "test_app.py": "from app import main\nmain(1)\n"})
    branch(repo, "feature")
    commit(repo, {"app.py": SRC.replace("x3 = 3", "x3 = 33"), "other.py": "y = 2\nz = 3\n"}, "change")
    scope = resolve_scope(repo, "main")
    return repo, scope, RepoIndex(repo, cache=False)


def _finding(lane, rid):
    return Finding("app.py", 3, 3, f"title {rid}", "claim", Severity.minor, Category.correctness,
                   lane=lane, source="ruff", rule_id=rid)


def _inputs(env, n_callers=1):
    repo, scope, index = env
    helper = Symbol("helper", "helper", "function", "app.py", 1, 2, "def helper(a):")
    sites = [CallSite("app.py", 36 + i, "main", "36 return helper(a)") for i in range(n_callers)]
    return dict(
        scope=scope, index=index, config=Config(),
        tool_findings=[_finding(Lane.inline, "B101"), _finding(Lane.nitpick, "E501")],
        guidelines=[Guideline("CLAUDE.md", "", "be nice", "claude_md")],
        learnings=[Learning("L1", "2026-11-03", "**", "close sessions", None, 0)],
        intent=Intent("feature", ["change"], None, []),
        companions=[Companion("app.py", "other.py", 4, 5, 0.8)],
        callers={"main": sites}, callees={"main": [helper]}, tests=["test_app.py"],
    )


def test_section_order(env):
    p = build_packet(**_inputs(env))
    heads = [ln for ln in p.text.split("\n") if ln.startswith("# ")]
    assert heads == ["# Intent", "# Guidelines", "# Learnings",
                     "# Tool signals (do not restate)", "# Diff", "# Context"]
    assert "source: CLAUDE.md\napplies to: (repo)" in p.text
    assert "[L1] (**) close sessions" in p.text
    assert "other.py — changed together in 4 of 5 commits" in p.text
    assert "app.py:1\n" in p.text  # callee header


def test_deterministic(env):
    a = build_packet(**_inputs(env))
    b = build_packet(**_inputs(env))
    assert a.text == b.text and a.sha == b.sha and len(a.sha) == 40


def test_only_inline_tool_signals(env):
    p = build_packet(**_inputs(env))
    assert "B101 app.py:3 title B101" in p.text
    assert "E501" not in p.text


def test_context_within_budget(env):
    p = build_packet(**_inputs(env), ratio=1.0)
    assert p.context_tokens <= p.diff_tokens * 1.0
    assert p.context_tokens <= 3 * p.diff_tokens
    big = build_packet(**_inputs(env), ratio=99.0)
    assert big.context_tokens <= 3 * big.diff_tokens


def test_tiny_budget_drops_items(env):
    p = build_packet(**_inputs(env, n_callers=60), ratio=0.5)
    assert p.dropped
    assert p.context_tokens <= int(p.diff_tokens * 0.5)
    assert any(d.startswith("caller:app.py:") for d in p.dropped)


def test_diff_over_cap_lists_remaining_files(env):
    p = build_packet(**_inputs(env), diff_cap=50)
    assert "# Not included (diff budget)" in p.text
    tail = p.text.split("# Not included (diff budget)")[1].split("# Context")[0]
    assert "app.py" in tail or "other.py" in tail
    assert p.omitted and not set(p.omitted) & set(p.files)


def test_mandatory_overflow_raises(env):
    kw = _inputs(env)
    kw["guidelines"] = [Guideline("CLAUDE.md", "", "word " * 2000, "claude_md")]
    with pytest.raises(PacketBudgetError):
        build_packet(**kw, diff_cap=100)


def test_empty_sections_emit_none(env):
    kw = _inputs(env)
    kw.update(guidelines=[], learnings=[], tool_findings=[], companions=[], callers={}, callees={}, tests=[])
    p = build_packet(**kw)
    assert p.text.count("(none)") >= 3
