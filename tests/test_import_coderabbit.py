import json
from pathlib import Path

import pytest

from preflight_review.cli import main
from preflight_review.importers.coderabbit import (
    fetch, import_coderabbit, parse_comment, write_golden, write_history, _parse_paginated,
)

FIX = Path(__file__).parent / "fixtures" / "gh"


def _items():
    comments = json.loads((FIX / "pull_comments.json").read_text())
    pulls = {p["number"]: p for p in json.loads((FIX / "pulls.json").read_text())}
    return {c["id"]: parse_comment(c, "example-org/example-repo", pulls) for c in comments}


def test_severity_and_category():
    it = _items()
    assert it[1001].severity == "critical" and it[1001].category == "potential_issue"
    assert it[1002].severity == "major" and it[1002].category == "security_privacy"
    assert it[1003].severity == "major" and it[1003].category == "functional_correctness"
    assert it[1004].severity == "trivial"


def test_nitpick_flag():
    it = _items()
    assert it[1004].is_nitpick and it[1004].category == "nitpick"
    assert not it[1001].is_nitpick


def test_title_and_lines_and_shas():
    i = _items()[1001]
    assert i.title == "Deploy job runs on pull_request events with secrets."
    assert (i.pr, i.start_line, i.end_line) == (1, 3, 9)
    assert i.base_sha.endswith("1") and i.head_sha


def test_agent_prompt_extracted_and_excluded():
    i = _items()[1001]
    assert i.agent_prompt.startswith("In .github/workflows/deploy.yml")
    assert "```" not in i.agent_prompt
    assert "Prompt for AI Agents" not in i.body_excerpt
    assert "restrict the trigger" not in i.body_excerpt
    assert len(i.body_excerpt) <= 600
    assert _items()[1002].agent_prompt is None


def test_outcome_addressed():
    it = _items()
    assert it[1003].outcome == "fixed" and it[1002].outcome == "unknown"


def test_fetch_filters_and_paths():
    calls = []
    data = {
        "pulls?": json.loads((FIX / "pulls.json").read_text()),
        "comments?": json.loads((FIX / "pull_comments.json").read_text()),
    }

    def gh(path):
        calls.append(path)
        return data["comments?"] if "pulls/comments" in path else data["pulls?"]

    dump = fetch("example-org/example-repo", gh)
    assert calls == [
        "repos/example-org/example-repo/pulls?state=all&per_page=100",
        "repos/example-org/example-repo/pulls/comments?per_page=100",
    ]
    assert sorted(c["id"] for c in dump.comments) == [1001, 1002, 1003, 1004]


def test_paginated_concat():
    assert _parse_paginated('[{"a":1}]\n[{"a":2}]') == [{"a": 1}, {"a": 2}]


def test_write_history_and_golden(tmp_path):
    res = import_coderabbit("example-org/example-repo", tmp_path, from_fixture=FIX)
    assert res.n_items == 4 and res.n_skipped == 0
    hist = [json.loads(l) for l in res.history_path.read_text().splitlines()]
    assert [h["comment_id"] for h in hist] == [1001, 1002, 1003, 1004]
    assert set(hist[0]) == {
        "repo", "pr", "comment_id", "created_at", "file", "start_line", "end_line", "severity",
        "category", "title", "body_excerpt", "is_nitpick", "outcome", "head_sha", "base_sha",
        "agent_prompt",
    }
    gold = [json.loads(l) for l in res.golden_path.read_text().splitlines()]
    assert set(gold[0]) == {
        "repo", "pr", "commit", "file", "line_start", "line_end", "bug_class",
        "expected_severity", "is_real", "in_scope", "author_verdict", "title",
    }
    assert gold[0]["is_real"] is None and gold[0]["expected_severity"] == "critical"


def test_write_helpers_create_parents(tmp_path):
    write_history([], tmp_path / "a" / "h.jsonl")
    write_golden([], tmp_path / "b" / "g.jsonl")
    assert (tmp_path / "a" / "h.jsonl").exists() and (tmp_path / "b" / "g.jsonl").exists()


def test_missing_fixture_raises(tmp_path):
    from preflight_review.importers.coderabbit import CodeRabbitImportError

    with pytest.raises(CodeRabbitImportError):
        import_coderabbit("example-org/example-repo", tmp_path, from_fixture=tmp_path / "nope")


def test_cli_ok(tmp_path, capsys):
    rc = main(["import-coderabbit", "--repo", "example-org/example-repo", "--from-fixture", str(FIX), "--out", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "history.jsonl").exists() and (tmp_path / "golden.jsonl").exists()
    assert "imported 4 CodeRabbit comments (0 skipped)" in capsys.readouterr().out


def test_cli_bad_repo(tmp_path):
    assert main(["import-coderabbit", "--repo", "nonsense", "--out", str(tmp_path)]) == 1
    assert main(["import-coderabbit", "--out", str(tmp_path)]) == 1



def test_cli_from_fixture_file(tmp_path):
    rc = main(["import-coderabbit", "--repo", "example-org/example-repo", "--from-fixture",
               str(FIX / "pull_comments.json"), "--out", str(tmp_path)])
    assert rc == 0
    assert len((tmp_path / "history.jsonl").read_text().splitlines()) == 4
    assert (tmp_path / "golden.jsonl").exists()
