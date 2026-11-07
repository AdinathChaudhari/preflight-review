"""CLI-level tests for review / init / doctor with the fake backend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from preflight_review import cli, doctor, naming, pipeline
from preflight_review.doctor import Check

from .conftest import branch, commit, make_repo

RUN1 = Path(__file__).parent / "fixtures" / "fake" / "run1.json"

BASE = "\n".join(f"line{i} = {i}" for i in range(12)) + "\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = make_repo(tmp_path, {"app/service.py": BASE})
    branch(r, "feature/x")
    commit(r, {"app/service.py": BASE.replace("line3 = 3", "line3 = 33")}, "change")
    monkeypatch.chdir(r)
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(RUN1))
    no_gh = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(
        pipeline, "pr_label", lambda root, br, gh_runner=None: naming.pr_label(root, br, no_gh)
    )
    return r


def test_review_prints_report_path(repo, capsys):
    assert cli.main(["review", "--backend", "fake", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert ".preflight/reviews/" in out and "-preflight-review-01.md" in out


def test_review_json(repo, capsys):
    assert cli.main(["review", "--backend", "fake", "--base", "main", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert set(d) == {"md", "json", "outcome", "confirmed", "unverified", "round", "reused"}
    assert d["round"] == 1 and d["reused"] is False and d["outcome"] == "complete"


def test_init_then_review(repo, capsys):
    assert cli.main(["init"]) == 0
    assert (repo / ".preflight" / "config.yaml").exists()
    capsys.readouterr()
    assert cli.main(["review", "--backend", "fake", "--base", "main"]) == 0
    assert "-preflight-review-01.md" in capsys.readouterr().out


def test_review_reuse_then_full(repo, capsys):
    cli.main(["review", "--backend", "fake", "--base", "main"])
    capsys.readouterr()
    assert cli.main(["review", "--backend", "fake", "--base", "main"]) == 0
    assert "reusing" in capsys.readouterr().out
    assert cli.main(["review", "--backend", "fake", "--base", "main", "--full"]) == 0
    assert "-preflight-review-02.md" in capsys.readouterr().out


def test_review_json_reuse_counts_match_report(repo, capsys):
    cli.main(["review", "--backend", "fake", "--base", "main", "--json"])
    first = json.loads(capsys.readouterr().out)
    cli.main(["review", "--backend", "fake", "--base", "main", "--json"])
    second = json.loads(capsys.readouterr().out)
    assert second["reused"] is True
    assert second["confirmed"] == first["confirmed"]
    assert second["unverified"] == first["unverified"]
    data = json.loads(Path(second["json"]).read_text())
    assert second["confirmed"] == len(data["findings"])


def test_review_incomplete_exits_1(repo, tmp_path, monkeypatch, capsys):
    p = tmp_path / "err.json"
    p.write_text(json.dumps({"__default__": {"__error__": "down"}}))
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(p))
    assert cli.main(["review", "--backend", "fake", "--base", "main"]) == 1
    assert "incomplete" in capsys.readouterr().err


def test_review_json_stdout_pure_and_summary_on_stderr(repo, capsys):
    assert cli.main(["review", "--backend", "fake", "--base", "main", "--json"]) == 0
    cap = capsys.readouterr()
    json.loads(cap.out)
    assert "preflight:" not in cap.out
    assert "preflight: complete:" in cap.err


def test_review_json_incomplete(repo, tmp_path, monkeypatch, capsys):
    p = tmp_path / "err.json"
    p.write_text(json.dumps({"__default__": {"__error__": "down"}}))
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(p))
    assert cli.main(["review", "--backend", "fake", "--base", "main", "--json"]) == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out)["outcome"] == "incomplete"
    assert "incomplete" in cap.err and "merge risk unknown" in cap.err


def test_review_bad_base_maps_to_exit_1(repo, capsys):
    assert cli.main(["review", "--backend", "fake", "--base", "nope-branch"]) == 1
    assert "preflight-review:" in capsys.readouterr().err


def test_doctor_exit_codes(repo, monkeypatch, capsys):
    ok = [Check("git", True, "ok", True), Check("ruff", False, "not installed", False)]
    monkeypatch.setattr(doctor, "run_doctor", lambda root, **kw: ok)
    assert cli.main(["doctor"]) == 0
    assert "git" in capsys.readouterr().out
    bad = ok + [Check("claude", False, "missing", True)]
    monkeypatch.setattr(doctor, "run_doctor", lambda root, **kw: bad)
    assert cli.main(["doctor"]) == 1
