"""Deterministic tool layer: parsers on fixtures, gating, lanes, degradation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Lane, Severity, Verdict, to_dict
from preflight_review.scope import resolve_scope
from preflight_review.tools import (
    osv_should_run,
    parse_astgrep,
    parse_gitleaks,
    parse_osv,
    parse_ruff,
    run_astgrep,
    run_gitleaks,
    run_osv,
    run_ruff,
    run_tools,
)
from tests.conftest import branch, commit, make_repo

FIX = Path(__file__).parent / "fixtures" / "tools"
ROOT = Path("/REPO")
FAKE_SECRET = "FAKE-SECRET-VALUE-9f8e7d6c5b4a"


def _text(name: str) -> str:
    return (FIX / name).read_text()


def _scope(tmp_path, base_files, new_files):
    repo = make_repo(tmp_path, base_files)
    branch(repo, "feature")
    commit(repo, new_files, "change")
    return repo, resolve_scope(repo, "main")


# ---------------------------------------------------------------- ruff parser


def test_parse_ruff_fields():
    fs = parse_ruff(_text("ruff_output.json"), ROOT)
    assert len(fs) == 6
    by = {f.rule_id: f for f in fs}
    assert by["S608"].file == "app/x.py"
    assert by["S608"].start_line == 3 and by["S608"].end_line == 3
    assert by["S608"].severity == Severity.major and by["S608"].category == Category.security
    assert by["B006"].severity == Severity.minor and by["B006"].category == Category.correctness
    assert by["B006"].suggested_fix.startswith("Replace with")
    assert by["F401"].severity == Severity.trivial
    assert by["F821"].severity == Severity.minor
    assert by["E902"].severity == Severity.minor and by["E902"].category == Category.correctness
    assert by["RUF012"].file == "app/models.py"
    assert by["RUF012"].category == Category.maintainability
    assert all(f.source == "ruff" and len(f.title) <= 80 for f in fs)
    assert by["S608"].title.startswith("S608: ")


def test_parse_ruff_evidence_reads_disk(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("import os\n\nq = 'select ' + x\n")
    raw = json.loads(_text("ruff_output.json"))[:1]
    raw[0]["filename"] = str(tmp_path / "app" / "x.py")
    fs = parse_ruff(json.dumps(raw), tmp_path)
    assert fs[0].file == "app/x.py"
    assert fs[0].evidence[0].quote == "q = 'select ' + x"


def test_malformed_json_raises_tool_error():
    for fn in (parse_ruff, parse_gitleaks, parse_astgrep):
        with pytest.raises(ToolError):
            fn("{not json", ROOT)
    with pytest.raises(ToolError):
        parse_osv("{not json", ROOT)
    with pytest.raises(ToolError):
        parse_ruff('[{"code": "S1"}]', ROOT)


# ------------------------------------------------------------- gitleaks parser


def test_parse_gitleaks_dedupes_and_never_leaks_secret():
    fs = parse_gitleaks(_text("gitleaks_output.json"), ROOT)
    assert len(fs) == 2
    for f in fs:
        assert f.severity == Severity.critical and f.category == Category.secrets
        assert f.verdict == Verdict.SKIPPED and f.source == "gitleaks"
        assert f.evidence[0].quote == f"REDACTED ({f.rule_id})"
        assert FAKE_SECRET not in repr(to_dict(f))
    assert fs[0].title == "Secret detected: slack-bot-token"
    assert fs[0].file == "app/config.py" and fs[0].start_line == 4


def test_parse_gitleaks_dedupe_fallback_without_fingerprint():
    item = {"RuleID": "r", "File": "a.py", "StartLine": 2, "EndLine": 2}
    fs = parse_gitleaks(json.dumps([item, item]), ROOT)
    assert len(fs) == 1


# ------------------------------------------------------------ ast-grep parser


def test_parse_astgrep_zero_based_lines_and_severity():
    fs = parse_astgrep(_text("astgrep_output.json"), ROOT)
    assert [f.start_line for f in fs] == [3, 7, 10]
    assert fs[1].end_line == 8
    assert [f.severity for f in fs] == [Severity.major, Severity.minor, Severity.trivial]
    assert all(f.category == Category.domain and f.source == "ast-grep" for f in fs)
    assert fs[0].rule_id == "no-mutable-default"


# ----------------------------------------------------------------- osv parser


def test_parse_osv_new_vulns_and_base_subtraction(tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n[[package]]\nname = "requests"\n')
    fs = parse_osv(_text("osv_output.json"), tmp_path, "uv.lock")
    assert len(fs) == 3
    assert all(f.severity == Severity.major and f.category == Category.dependency for f in fs)
    assert all(f.verdict == Verdict.SKIPPED and f.source == "osv" for f in fs)
    req = [f for f in fs if "requests" in f.title]
    assert req[0].start_line == 3 and req[0].title == "GHSA-j8r2-6x86-q33q: requests@2.19.0"
    assert [f for f in fs if "urllib3" in f.title][0].start_line == 1
    base = json.loads(_text("osv_output.json"))
    base["results"][0]["packages"].pop()  # base lacked urllib3 only
    fs2 = parse_osv(_text("osv_output.json"), tmp_path, "uv.lock", json.dumps(base))
    assert [f.rule_id for f in fs2] == ["GHSA-www2-v7xj-xrc6"]


# --------------------------------------------------------------- degradation


def test_missing_binaries_degrade(tmp_path, monkeypatch):
    repo, scope = _scope(
        tmp_path,
        {"a.py": "x = 1\n", "uv.lock": "a\n"},
        {"a.py": "x = 1\ny = 2\n", "uv.lock": "a\nb\n"},
    )
    (repo / ".preflight" / "ast-rules").mkdir(parents=True)
    (repo / ".preflight" / "ast-rules" / "r.yml").write_text("id: r\n")
    monkeypatch.setattr("preflight_review.tools.base.which", lambda name: None)
    findings, statuses = run_tools(scope, Config())
    assert findings == []
    by = {s.name: s for s in statuses}
    assert set(by) == {"ruff", "gitleaks", "ast-grep", "osv-scanner"}
    for name, hint in [
        ("ruff", "uv tool install ruff"),
        ("gitleaks", "brew install gitleaks"),
        ("ast-grep", "brew install ast-grep"),
        ("osv-scanner", "brew install osv-scanner"),
    ]:
        assert by[name].installed is False and by[name].ran is False
        assert hint in by[name].note


def test_ruff_no_python_files(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"README.md": "a\n"}, {"README.md": "a\nb\n"})
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: "/bin/true")
    monkeypatch.setattr("preflight_review.tools.ruff.tool_version", lambda *a, **k: "0")
    findings, status = run_ruff(scope, Config())
    assert findings == [] and status.ran is False
    assert status.note == "no python files changed"


def test_astgrep_skipped_without_rules(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: "/bin/true")
    monkeypatch.setattr("preflight_review.tools.astgrep.tool_version", lambda *a, **k: "0")
    findings, status = run_astgrep(scope, Config())
    assert findings == [] and status.ran is False and "no ast-grep rules" in status.note


# ------------------------------------------------------------------- osv gate


def test_osv_gating(tmp_path):
    _, s_lock = _scope(tmp_path / "a", {"uv.lock": "a\n"}, {"uv.lock": "a\nb\n"}) if (
        (tmp_path / "a").mkdir() or True
    ) else (None, None)
    assert osv_should_run(s_lock, Config()) is True
    cfg = Config()
    cfg.tools.osv = False
    assert osv_should_run(s_lock, cfg) is False
    (tmp_path / "b").mkdir()
    _, s_py = _scope(tmp_path / "b", {"a.py": "x\n"}, {"a.py": "y\n"})
    assert osv_should_run(s_py, Config()) is False


def test_osv_runs_on_lockfile_with_default_path_filters(tmp_path):
    repo = make_repo(tmp_path, {"uv.lock": "a\n"})
    branch(repo, "feature")
    commit(repo, {"uv.lock": "a\nb\n"}, "change")
    scope = resolve_scope(repo, "main", path_filters=Config().path_filters)
    assert scope.files == [] and "uv.lock" in scope.filtered_out
    assert osv_should_run(scope, Config()) is True


def test_failing_binaries_do_not_abort_tool_layer(tmp_path, monkeypatch):
    _, scope = _scope(
        tmp_path,
        {"a.py": "x = 1\n", "uv.lock": "a\n"},
        {"a.py": "x = 2\n", "uv.lock": "a\nb\n"},
    )
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: None if n == "gitleaks" else "/bin/fake")
    for mod in ("ruff", "osv", "astgrep"):
        monkeypatch.setattr(
            f"preflight_review.tools.{mod}.tool_version", lambda *a, **k: "0", raising=False
        )
    fail = lambda *a, **k: (127, "", "could not determine extractor")  # noqa: E731
    for mod in ("ruff", "osv", "astgrep"):
        monkeypatch.setattr(f"preflight_review.tools.{mod}.run", fail)
    _, st = run_osv(scope, Config())
    assert st.note.startswith("failed") or "failed" in st.note
    _, st = run_ruff(scope, Config())
    assert st.note.startswith("failed")
    findings, statuses = run_tools(scope, Config())
    assert findings == []
    assert {s.name for s in statuses} == {"ruff", "gitleaks", "ast-grep", "osv-scanner"}


# ---------------------------------------------------------------------- lanes


def test_lane_assignment(tmp_path, monkeypatch):
    base = "".join(f"l{i} = {i}\n" for i in range(1, 11))
    new = base + "added = 1\n"
    _, scope = _scope(tmp_path, {"app.py": base}, {"app.py": new})
    raw = [
        {"code": "S101", "message": "m", "filename": str(scope.repo_root / "app.py"),
         "location": {"row": 11, "column": 1}, "end_location": {"row": 11, "column": 5}},
        {"code": "S101", "message": "m", "filename": str(scope.repo_root / "app.py"),
         "location": {"row": 2, "column": 1}, "end_location": {"row": 2, "column": 5}},
    ]
    monkeypatch.setattr(
        "preflight_review.tools.base.which",
        lambda n: "/bin/ruff" if n == "ruff" else None,
    )
    monkeypatch.setattr("preflight_review.tools.ruff.tool_version", lambda *a, **k: "0")
    monkeypatch.setattr(
        "preflight_review.tools.ruff.run", lambda *a, **k: (0, json.dumps(raw), "")
    )
    findings, _ = run_tools(scope, Config())
    by_line = {f.start_line: f for f in findings}
    assert by_line[11].lane == Lane.inline
    assert by_line[2].lane == Lane.pre_existing


# ------------------------------------------------------------- live gitleaks


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_live_gitleaks_finds_one_secret(tmp_path):
    token = "xoxb-" + "1234567890" + "12" + "-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    repo, scope = _scope(
        tmp_path,
        {"app.py": "x = 1\n"},
        {"config.py": f'SLACK_BOT_TOKEN = "{token}"\n'},
    )
    findings, status = run_gitleaks(scope, Config())
    assert status.installed and status.ran
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.severity == Severity.critical and f.category == Category.secrets
    assert f.verdict == Verdict.SKIPPED and f.file == "config.py"
    assert token not in repr(to_dict(f))
    assert "a1B2c3D4e5F6" not in repr(to_dict(f))
