"""CI tool wrappers (zizmor, actionlint, shellcheck) and extra ast-grep rule directories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import Category, Lane, Severity
from preflight_review.scope import resolve_scope
from preflight_review.tools import (
    parse_actionlint,
    parse_shellcheck,
    parse_zizmor,
    run_actionlint,
    run_astgrep,
    run_shellcheck,
    run_tools,
    run_zizmor,
)
from preflight_review.tools.zizmor import is_workflow_file
from tests.conftest import branch, commit, make_repo

ROOT = Path("/REPO")

WORKFLOW = """\
name: deploy
on:
  workflow_run:
    workflows: [ci]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""


def zizmor_item(path=".github/workflows/deploy.yml", row=4, severity="Medium",
                ident="excessive-permissions", ignored=False):
    return {
        "ident": ident,
        "desc": "overly broad permissions",
        "url": f"https://docs.zizmor.sh/audits/#{ident}",
        "determinations": {"confidence": "High", "severity": severity, "persona": "Regular"},
        "locations": [
            {
                "symbolic": {
                    "key": {"Local": {"prefix": ".", "given_path": path}},
                    "annotation": "default permissions used due to no permissions: block",
                    "route": {"components": []},
                    "feature_kind": "Normal",
                    "kind": "Related",
                },
                "concrete": {"location": {"start_point": {"row": 0, "column": 0},
                                          "end_point": {"row": 0, "column": 4}}},
            },
            {
                "symbolic": {
                    "key": {"Local": {"prefix": ".", "given_path": path}},
                    "annotation": "default permissions used due to no permissions: block",
                    "route": {"components": []},
                    "feature_kind": "Normal",
                    "kind": "Primary",
                },
                "concrete": {"location": {"start_point": {"row": row, "column": 2},
                                          "end_point": {"row": row + 3, "column": 0}}},
            },
        ],
        "ignored": ignored,
    }


def _scope(tmp_path, files):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feature")
    commit(repo, files, "change")
    return repo, resolve_scope(repo, "main")


# ------------------------------------------------------------------- zizmor


def test_parse_zizmor_primary_location_and_severity():
    data = [zizmor_item(), zizmor_item(severity="High", ident="artipacked", row=8),
            zizmor_item(ident="ignored-one", ignored=True)]
    fs = parse_zizmor(json.dumps(data), ROOT)
    assert [f.rule_id for f in fs] == ["excessive-permissions", "artipacked"]
    f = fs[0]
    assert f.file == ".github/workflows/deploy.yml" and (f.start_line, f.end_line) == (5, 8)
    assert f.severity == Severity.minor and fs[1].severity == Severity.major
    assert f.category == Category.security and f.source == "zizmor"
    assert "no permissions: block" in f.claim and "docs.zizmor.sh" in f.claim


@pytest.mark.parametrize("text", ["{not json", '{"a": 1}', json.dumps([{"ident": "x"}])])
def test_parse_zizmor_malformed_raises(text):
    with pytest.raises(ToolError):
        parse_zizmor(text, ROOT)


@pytest.mark.parametrize("path,hit", [
    (".github/workflows/deploy.yml", True),
    (".github/workflows/ci.yaml", True),
    ("actions/setup/action.yml", True),
    (".github/dependabot.yml", False),
    ("deploy/job.yaml", False),
])
def test_is_workflow_file(path, hit):
    assert is_workflow_file(path) is hit


def test_zizmor_missing_is_degraded_only_with_workflow_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: None)
    _, scope = _scope(tmp_path, {".github/workflows/deploy.yml": WORKFLOW})
    _, st = run_zizmor(scope, Config())
    assert st.degraded and "uv tool install zizmor" in st.note


def test_zizmor_missing_not_degraded_without_workflows(tmp_path, monkeypatch):
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: None)
    _, scope = _scope(tmp_path, {"app.py": "x = 1\n"})
    _, st = run_zizmor(scope, Config())
    assert not st.degraded and st.installed is False


def test_zizmor_disabled(tmp_path):
    _, scope = _scope(tmp_path, {".github/workflows/deploy.yml": WORKFLOW})
    cfg = Config()
    cfg.tools.zizmor = False
    _, st = run_zizmor(scope, cfg)
    assert st.note == "disabled in config" and not st.degraded


def test_zizmor_runs_on_changed_workflows(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {".github/workflows/deploy.yml": WORKFLOW, "app.py": "x = 1\n"})
    seen = {}

    def fake_run(cmd, cwd, timeout=120):
        seen["cmd"] = cmd
        return 13, json.dumps([zizmor_item(row=5)]), ""

    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: f"/bin/{n}")
    monkeypatch.setattr("preflight_review.tools.zizmor.tool_version", lambda *a, **k: "zizmor 1.0")
    monkeypatch.setattr("preflight_review.tools.zizmor.run", fake_run)
    fs, st = run_zizmor(scope, Config())
    assert seen["cmd"][1:] == ["--format", "json", "--min-severity", "medium", "--offline",
                               "--", ".github/workflows/deploy.yml"]
    assert st.ran and st.finding_count == 1 and fs[0].start_line == 6


def test_zizmor_error_exit_is_degraded(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {".github/workflows/deploy.yml": WORKFLOW})
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: f"/bin/{n}")
    monkeypatch.setattr("preflight_review.tools.zizmor.tool_version", lambda *a, **k: "1")
    monkeypatch.setattr("preflight_review.tools.zizmor.run", lambda *a, **k: (2, "", "bad flag"))
    fs, st = run_zizmor(scope, Config())
    assert fs == [] and st.degraded and "exit 2" in st.note


def test_zizmor_finding_off_diff_line_is_pre_existing(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, {".github/workflows/deploy.yml": WORKFLOW})
    branch(repo, "feature")
    commit(repo, {".github/workflows/deploy.yml": WORKFLOW + "      - run: echo hi\n"}, "c")
    scope = resolve_scope(repo, "main")
    out = json.dumps([zizmor_item(row=0), zizmor_item(row=9, ident="template-injection")])
    monkeypatch.setattr("preflight_review.tools.base.which",
                        lambda n: "/bin/zizmor" if n == "zizmor" else None)
    monkeypatch.setattr("preflight_review.tools.zizmor.tool_version", lambda *a, **k: "1")
    monkeypatch.setattr("preflight_review.tools.zizmor.run", lambda *a, **k: (13, out, ""))
    findings, _ = run_tools(scope, Config())
    lanes = {f.rule_id: f.lane for f in findings if f.source == "zizmor"}
    assert lanes == {"excessive-permissions": Lane.pre_existing, "template-injection": Lane.inline}


# --------------------------------------------------------------- actionlint


def test_parse_actionlint():
    data = [{"message": "property \"foo\" is not defined", "filepath": ".github/workflows/ci.yml",
             "line": 7, "column": 3, "kind": "expression", "snippet": "x"}]
    fs = parse_actionlint(json.dumps(data), ROOT)
    assert len(fs) == 1 and fs[0].rule_id == "expression" and fs[0].start_line == 7
    assert fs[0].severity == Severity.minor and fs[0].category == Category.correctness
    assert parse_actionlint("null", ROOT) == [] and parse_actionlint("", ROOT) == []
    with pytest.raises(ToolError):
        parse_actionlint('[{"line": 1}]', ROOT)


def test_actionlint_opt_in_and_degraded_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: None)
    _, scope = _scope(tmp_path, {".github/workflows/ci.yml": WORKFLOW})
    _, st = run_actionlint(scope, Config())
    assert st.note == "disabled in config" and not st.degraded
    cfg = Config()
    cfg.tools.actionlint = True
    _, st = run_actionlint(scope, cfg)
    assert st.degraded and "brew install actionlint" in st.note


# --------------------------------------------------------------- shellcheck


def test_parse_shellcheck_severity_map():
    data = {"comments": [
        {"file": "scripts/a.sh", "line": 2, "endLine": 2, "column": 1, "endColumn": 3,
         "level": "error", "code": 2068, "message": "Double quote array expansions."},
        {"file": "scripts/a.sh", "line": 4, "level": "warning", "code": 2034, "message": "unused"},
        {"file": "scripts/a.sh", "line": 5, "level": "style", "code": 2086, "message": "quote"},
    ]}
    fs = parse_shellcheck(json.dumps(data), ROOT)
    assert [f.severity for f in fs] == [Severity.major, Severity.minor, Severity.trivial]
    assert fs[0].rule_id == "SC2068" and fs[0].source == "shellcheck"
    with pytest.raises(ToolError):
        parse_shellcheck("[]", ROOT)


def test_shellcheck_runs_on_changed_scripts(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"scripts/deploy.sh": "#!/bin/bash\necho $1\n", "app.py": "x=1\n"})
    cfg = Config()
    cfg.tools.shellcheck = True
    seen = {}

    def fake_run(cmd, cwd, timeout=120):
        seen["cmd"] = cmd
        return 1, json.dumps({"comments": [{"file": "scripts/deploy.sh", "line": 2,
                                            "level": "info", "code": 2086, "message": "q"}]}), ""

    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: f"/bin/{n}")
    monkeypatch.setattr("preflight_review.tools.shellcheck.tool_version", lambda *a, **k: "0.10")
    monkeypatch.setattr("preflight_review.tools.shellcheck.run", fake_run)
    fs, st = run_shellcheck(scope, cfg)
    assert seen["cmd"][1:] == ["-f", "json1", "--", "scripts/deploy.sh"]
    assert st.ran and fs[0].severity == Severity.trivial


# ------------------------------------------------------- ast-grep extra rule dirs


def _capture_sgconfig(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, timeout=120):
        cfg_path = Path(cmd[cmd.index("-c") + 1])
        seen["config"] = cfg_path.read_text()
        seen["extra"] = sorted(p.name for p in (cfg_path.parent / "extra-rules").glob("*.yml")) \
            if (cfg_path.parent / "extra-rules").is_dir() else []
        return 0, "[]", ""

    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: "/bin/ast-grep")
    monkeypatch.setattr("preflight_review.tools.astgrep.tool_version", lambda *a, **k: "0")
    monkeypatch.setattr("preflight_review.tools.astgrep.run", fake_run)
    return seen


def test_astgrep_loads_extra_rule_dir_without_repo_rules(tmp_path, monkeypatch):
    _repo, scope = _scope(tmp_path, {"app.py": "x = 1\n"})
    extra = tmp_path / "rules"
    extra.mkdir()
    (extra / "a.yml").write_text("id: a\n")
    (extra / "notes.md").write_text("not a rule\n")
    seen = _capture_sgconfig(monkeypatch)
    _, st = run_astgrep(scope, Config(), [extra])
    assert st.ran and seen["extra"] == ["a.yml"]
    assert ".preflight" not in seen["config"]
    _, st = run_astgrep(scope, Config())
    assert not st.ran and "no ast-grep rules" in st.note


def test_astgrep_extra_rules_skip_names_the_repo_already_has(tmp_path, monkeypatch):
    repo, scope = _scope(tmp_path, {"app.py": "x = 1\n"})
    own = repo / ".preflight" / "ast-rules"
    own.mkdir(parents=True)
    (own / "a.yml").write_text("id: a\n")
    extra = tmp_path / "rules"
    extra.mkdir()
    (extra / "a.yml").write_text("id: a\n")
    (extra / "b.yml").write_text("id: b\n")
    seen = _capture_sgconfig(monkeypatch)
    run_astgrep(scope, Config(), [extra, tmp_path / "missing"])
    assert seen["extra"] == ["b.yml"] and "ast-rules" in seen["config"]


def test_astgrep_extra_rules_missing_binary_is_degraded(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"app.py": "x = 1\n"})
    extra = tmp_path / "rules"
    extra.mkdir()
    (extra / "a.yml").write_text("id: a\n")
    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: None)
    _, st = run_astgrep(scope, Config(), [extra])
    assert st.degraded and "brew install ast-grep" in st.note


def test_changed_paths_follow_end_of_options_marker(tmp_path, monkeypatch):
    """A changed file named like a flag lands after `--`, so no tool parses it as one."""
    wf = "-shellcheck=x.github/workflows/a.yml"
    _, scope = _scope(tmp_path, {"-evil.sh": "#!/bin/sh\necho $1\n", "-evil.py": "x = 1\n",
                                 wf: WORKFLOW, ".github/workflows/b.yml": WORKFLOW})
    cfg = Config()
    cfg.tools.shellcheck = cfg.tools.actionlint = True
    seen = {}

    def fake_run(cmd, cwd, timeout=120):
        seen[Path(cmd[0]).name] = cmd
        return 0, "[]" if "shellcheck" not in cmd[0] else '{"comments": []}', ""

    monkeypatch.setattr("preflight_review.tools.base.which", lambda n: f"/bin/{n}")
    for mod in ("actionlint", "shellcheck", "astgrep", "zizmor"):
        monkeypatch.setattr(f"preflight_review.tools.{mod}.tool_version", lambda *a, **k: "1")
        monkeypatch.setattr(f"preflight_review.tools.{mod}.run", fake_run)
    run_actionlint(scope, cfg)
    run_shellcheck(scope, cfg)
    run_zizmor(scope, cfg)
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.yml").write_text("id: a\n")
    run_astgrep(scope, cfg, [rules])
    assert set(seen) == {"actionlint", "shellcheck", "zizmor", "ast-grep"}
    for name, cmd in seen.items():
        flagged = [a for a in cmd if a.startswith("-evil") or a == wf]
        assert flagged, name
        assert all(cmd.index(a) > cmd.index("--") for a in flagged), name


@pytest.mark.parametrize("entry", [None, "warning", 3])
def test_parse_zizmor_non_object_entry_is_tool_error(entry):
    with pytest.raises(ToolError, match="not an object"):
        parse_zizmor(json.dumps([entry]), ROOT)
