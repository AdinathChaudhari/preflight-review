"""Pure risk-tier / risk-order tests (no git)."""

import pytest

from preflight_review.context.risk import (
    TIER_RANK, TIERS, is_sensitive, order_by_risk, risk_score, risk_sort_key, risk_tier,
)


@pytest.mark.parametrize("path,tier", [
    ("src/preflight_review/pipeline.py", "source"),
    ("src/app/prompts/reviewer/system.md", "source"),
    ("tests/test_x.py", "test"),
    ("src/foo_test.go", "test"),
    (".github/workflows/ci.yml", "config"),
    (".claude-plugin/plugin.json", "config"),
    ("pyproject.toml", "config"),
    ("docs/guide.md", "docs"),
    ("README.md", "docs"),
    ("uv.lock", "docs"),
    ("tests/fixtures/case.json", "docs"),
    ("eval/seeded/cases/a.json", "docs"),
    ("web/app.test.ts", "test"),
    ("Dockerfile", "config"),
    ("proto/x_pb2.py", "docs"),
    ("weird.xyz", "config"),
])
def test_risk_tier(path, tier):
    assert risk_tier(path) == tier


def test_tiers_ranked():
    assert TIERS == ("source", "test", "config", "docs")
    assert TIER_RANK["source"] < TIER_RANK["docs"]


@pytest.mark.parametrize("path", [
    "app/auth/login.py", "app/db/migrations/0001.py", "app/db_utils.py", "app/run_subprocess.py",
])
def test_sensitive(path):
    assert is_sensitive(path)


@pytest.mark.parametrize("path", ["app/feedback.py", "app/util.py"])
def test_not_sensitive(path):
    assert not is_sensitive(path)


def test_score_and_key():
    assert risk_score("app/auth.py", 20) == 60
    assert risk_score("app/core.py", 10, 5) == 110
    assert risk_sort_key("a/b.py", 5)[0] == 0


def test_order_by_risk():
    files = [("docs/a.md", 500), ("eval/x.json", 900), (".claude-plugin/p.json", 10),
             ("tests/test_a.py", 300), ("src/app/util.py", 40), ("src/app/auth.py", 20),
             ("src/app/core.py", 10)]
    out = order_by_risk(files, {"src/app/core.py": 5})
    assert out == ["src/app/core.py", "src/app/auth.py", "src/app/util.py",
                   "tests/test_a.py", ".claude-plugin/p.json", "eval/x.json", "docs/a.md"]


def test_ties_sorted_by_parent_then_path():
    files = [("b/x.py", 10), ("a/z.py", 10), ("a/y.py", 10)]
    assert order_by_risk(files) == ["a/y.py", "a/z.py", "b/x.py"]
    assert order_by_risk(list(reversed(files))) == order_by_risk(files)
