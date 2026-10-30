"""Learnings file layer."""

from __future__ import annotations

import pytest

from preflight_review.context.learnings import (
    LEARNINGS_PATH,
    Learning,
    append_learning,
    filter_by_path,
    format_learning,
    next_id,
    parse_learnings,
    read_learnings,
)
from preflight_review.errors import ConfigError

from tests.conftest import branch, make_repo, write

FILE = (
    "# Learnings\n\n"
    "- id: L1 · date: 2026-11-03 · path: app/services/** · from: F3 (abc123def456) · uses: 2\n"
    "  Always close sessions.\n"
    "  Second line.\n\n"
    "- id: L12 · date: 2026-11-04 · path: ** · uses: 0\n"
    "  Prefer pathlib.\n"
)


def test_round_trip_exact():
    ls = parse_learnings(FILE)
    assert len(ls) == 2
    assert ls[0].source_finding == "F3 (abc123def456)"
    assert ls[0].text == "Always close sessions.\nSecond line."
    assert ls[1].source_finding is None and ls[1].uses == 0
    rebuilt = "# Learnings\n\n" + "\n\n".join(format_learning(l) for l in ls) + "\n"
    assert rebuilt == FILE


def test_next_id():
    assert next_id([]) == "L1"
    assert next_id(parse_learnings(FILE)) == "L13"


def test_append_creates_then_appends(tmp_path):
    l1 = Learning("L1", "2026-11-03", "**", "one", None, 0)
    l2 = Learning("L2", "2026-11-03", "a/**", "two\nlines", "F1", 1)
    append_learning(tmp_path, l1)
    p = tmp_path / LEARNINGS_PATH
    assert p.read_text().startswith("# Learnings\n")
    append_learning(tmp_path, l2)
    assert parse_learnings(p.read_text()) == [l1, l2]


def test_read_from_base_beats_worktree(tmp_path):
    repo = make_repo(tmp_path, {LEARNINGS_PATH: FILE})
    branch(repo, "feature")
    write(repo, LEARNINGS_PATH, "# Learnings\n")
    assert len(read_learnings(repo, "main")) == 2
    assert read_learnings(repo, None) == []
    assert read_learnings(tmp_path / "nope", None) == []


def test_filter_by_path():
    ls = parse_learnings(FILE)
    assert [l.id for l in filter_by_path(ls, ["app/services/x/y.py"])] == ["L1", "L12"]
    assert [l.id for l in filter_by_path(ls, ["other.py"])] == ["L12"]


def test_malformed_header_names_line():
    with pytest.raises(ConfigError, match=r":3:"):
        parse_learnings("# Learnings\n\n- id: L1 · bogus\n  text\n")
