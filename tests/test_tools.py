"""Deterministic tool layer: parsers on fixtures, gating, lanes, degradation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from preflight_review.config import Config
from preflight_review.errors import ToolError
from preflight_review.findings import (
    Category,
    Finding,
    Lane,
    Severity,
    Verdict,
    to_dict,
)
from preflight_review.scope import resolve_scope
from preflight_review.tools import (
    degraded_tools,
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
from preflight_review.tools.gitleaks import config_args
from preflight_review.tools.secret_files import classify, run_secret_files
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
    assert by["S608"].severity == Severity.minor and by["S608"].category == Category.security
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
    assert set(by) == {
        "ruff", "gitleaks", "ast-grep", "osv-scanner", "secret-files", "zizmor", "actionlint",
        "shellcheck",
    }
    assert by["secret-files"].ran is True and by["secret-files"].degraded is False
    # No workflow or shell file changed: a missing CI tool is not degraded.
    assert not by["zizmor"].degraded and by["zizmor"].installed is False
    assert by["actionlint"].note == "disabled in config" and not by["shellcheck"].degraded
    for name, hint in [
        ("ruff", "uv tool install ruff"),
        ("gitleaks", "brew install gitleaks"),
        ("ast-grep", "brew install ast-grep"),
        ("osv-scanner", "brew install osv-scanner"),
    ]:
        assert by[name].installed is False and by[name].ran is False
        assert hint in by[name].note
        assert by[name].degraded is True  # enabled and relevant (a.py and uv.lock changed)
    assert degraded_tools(statuses) == [
        f"{n}: {by[n].note}" for n in ("ruff", "gitleaks", "ast-grep", "osv-scanner")
    ]


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
    by = {s.name: s for s in statuses}
    assert {"ruff", "gitleaks", "ast-grep", "osv-scanner", "secret-files"} <= set(by)
    assert by["ruff"].degraded and by["osv-scanner"].degraded


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


def test_ruff_test_noise_dropped_only_on_test_paths():
    from preflight_review.tools.ruff import drop_test_noise

    def f(path, code):
        return Finding(file=path, start_line=1, end_line=1, title=code, claim="c",
                       severity=Severity.major, category=Category.security, source="ruff",
                       rule_id=code)

    keep = [f("app/x.py", "S101"), f("tests/test_x.py", "S603"), f("app/auth.py", "S105")]
    drop = [f("tests/test_x.py", "S101"), f("pkg/test_y.py", "S105"), f("conftest.py", "S311"),
            f("a/tests/helpers.py", "S108"), f("svc/z_test.py", "S106")]
    assert drop_test_noise(keep + drop, Config()) == keep
    cfg = Config()
    cfg.tools.ruff.test_ignore = []
    assert len(drop_test_noise(keep + drop, cfg)) == 8


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


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_live_gitleaks_repo_config_allowlists_redacted_fixture_only(tmp_path):
    # Built at runtime so this file holds no PEM header gitleaks would match.
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    fixture = f'key = "{header}\\nREDACTED\\n"\nother = "{header}\\nREDACTED\\n"\n'
    body = "\n".join(
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn" + c * 33 for c in ("ygWyF8Pb", "aNDu6Op8", "kNGlHRY0")
    )
    real = f'KEY = """{header}\n{body}\n-----END RSA {"PRIVATE KEY"}-----"""\n'
    config = (Path(__file__).parents[1] / ".gitleaks.toml").read_text()
    repo, scope = _scope(
        tmp_path,
        {"app.py": "x = 1\n", ".gitleaks.toml": config},
        {"tests/test_fixture.py": fixture, "tests/test_real.py": real},
    )
    assert config_args(repo) == ["--config", str(repo / ".gitleaks.toml")]
    findings, status = run_gitleaks(scope, Config())
    assert status.ran
    assert {f.file for f in findings} == {"tests/test_real.py"}, findings


def test_gitleaks_config_args_absent(tmp_path):
    assert config_args(tmp_path) == []


# ------------------------------------------------------------- degraded banner


def test_missing_tool_without_work_is_not_degraded(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"README.md": "a\n"}, {"README.md": "a\nb\n"})
    monkeypatch.setattr("preflight_review.tools.base.which", lambda name: None)
    _, statuses = run_tools(scope, Config())
    by = {s.name: s for s in statuses}
    assert by["ruff"].installed is False and by["ruff"].degraded is False  # no python changed
    assert by["ast-grep"].degraded is False  # no rules defined
    assert by["osv-scanner"].degraded is False  # no manifest changed
    assert by["gitleaks"].degraded is True  # enabled and always relevant
    cfg = Config()
    cfg.tools.gitleaks = False
    _, statuses = run_tools(scope, cfg)
    assert degraded_tools(statuses) == []  # disabled tools never degrade


def test_unparseable_tool_output_degrades_without_aborting(tmp_path, monkeypatch):
    _, scope = _scope(tmp_path, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    monkeypatch.setattr(
        "preflight_review.tools.base.which", lambda n: None if n == "gitleaks" else "/bin/fake"
    )
    monkeypatch.setattr("preflight_review.tools.ruff.tool_version", lambda *a, **k: "0")
    monkeypatch.setattr("preflight_review.tools.ruff.run", lambda *a, **k: (0, "not json", ""))
    _findings, statuses = run_tools(scope, Config())
    ruff = next(s for s in statuses if s.name == "ruff")
    assert ruff.degraded is True and ruff.ran is False
    assert "malformed JSON" in ruff.note
    assert any(d.startswith("ruff: failed: ") for d in degraded_tools(statuses))


# --------------------------------------------------------------- secret files


@pytest.mark.parametrize(
    "path,kind",
    [
        (".env", "env file"),
        ("services/api/.env.local", "env file"),
        (".env.production", "env file"),
        (".env.example", None),
        ("deploy/.env.sample", None),
        (".env.local.example", None),
        ("config/.env.production.sample", None),
        ("deploy/.env.staging.template", None),
        (".env.example.bak", "env file"),
        ("certs/server.pem", "private key"),
        ("keys/client.p12", "private key or certificate bundle"),
        ("home/.ssh/id_rsa", "private SSH key"),
        ("home/.ssh/id_ed25519", "private SSH key"),
        ("home/.ssh/id_rsa.pub", None),
        ("config/credentials.json", "credentials file"),
        ("infra/my-service-account.json", "service-account key file"),
        ("app/environment.py", None),
        ("docs/env.md", None),
    ],
)
def test_secret_file_classify(path, kind):
    assert classify(path) == kind


def test_secret_file_service_account_by_content(tmp_path):
    (tmp_path / "gcp.json").write_text(json.dumps({"type": "service_account", "project_id": "p"}))
    (tmp_path / "data.json").write_text(json.dumps({"type": "user"}))
    assert classify("gcp.json", tmp_path) == "service-account key file"
    assert classify("data.json", tmp_path) is None


def test_secret_file_pem_flagged_only_with_private_key(tmp_path):
    (tmp_path / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    (tmp_path / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nREDACTED\n")
    (tmp_path / "bundle.pem").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nREDACTED\n"
    )
    assert classify("ca.pem", tmp_path) is None
    assert classify("key.pem", tmp_path) == "private key"
    assert classify("bundle.pem", tmp_path) == "private key"
    assert classify("missing.pem", tmp_path) == "private key"  # unreadable: name decides


def test_secret_file_key_ext_pgp_and_late_key(tmp_path):
    (tmp_path / "server.key").write_text("-----BEGIN RSA PRIVATE KEY-----\nREDACTED\n")
    (tmp_path / "pub.key").write_text("-----BEGIN PUBLIC KEY-----\nMIIB\n")
    (tmp_path / "gpg.pem").write_text("-----BEGIN PGP PRIVATE KEY BLOCK-----\nREDACTED\n")
    chain = "-----BEGIN CERTIFICATE-----\n" + "A" * 64 + "\n-----END CERTIFICATE-----\n"
    (tmp_path / "late.pem").write_text(chain * 2000 + "-----BEGIN PRIVATE KEY-----\nX\n")
    assert (tmp_path / "late.pem").stat().st_size > 64_000
    assert classify("deploy/tls/server.key") == "private key"
    assert classify("server.key", tmp_path) == "private key"
    assert classify("pub.key", tmp_path) is None
    assert classify("gpg.pem", tmp_path) == "private key"
    assert classify("late.pem", tmp_path) == "private key"


def test_secret_file_service_account_template_and_content(tmp_path):
    sa = json.dumps({"type": "service_account", "private_key": "REDACTED"})
    (tmp_path / "service-account.json").write_text(sa)
    (tmp_path / "service_account_schema.json").write_text(json.dumps({"type": "object"}))
    assert classify("service-account.example.json") is None
    assert classify("infra/service_account.template.json") is None
    assert classify("service-account.json", tmp_path) == "service-account key file"
    assert classify("service_account_schema.json", tmp_path) is None
    assert classify("service-account-missing.json", tmp_path) == "service-account key file"


def test_secret_file_committed_range_reads_head_not_worktree(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x\n"})
    branch(repo, "feature")
    sa = json.dumps({"type": "service_account", "private_key": "REDACTED"})
    commit(repo, {"gcp.json": sa, "vendor/gcp2.json": sa}, "key")
    (repo / "gcp.json").unlink()  # deleted locally, still committed
    (repo / "vendor/gcp2.json").unlink()
    scope = resolve_scope(repo, "main", include_worktree=False, path_filters=["!vendor/**"])
    assert "vendor/gcp2.json" in scope.filtered_out
    findings, _ = run_secret_files(scope, Config())
    assert sorted(f.file for f in findings) == ["gcp.json", "vendor/gcp2.json"]


def test_secret_files_always_reported_inline_and_skip_verifier(tmp_path, monkeypatch):
    env_base = "".join(f"K{i}=v{i}\n" for i in range(1, 8))
    _, scope = _scope(
        tmp_path,
        {"app.py": "x = 1\n", "config/.env.staging": env_base, "old/.env": "A=1\n"},
        {
            ".env.local": "API_URL=http://localhost\nAPI_KEY=dummy\n",
            ".env.example": "API_KEY=\n",
            "certs/server.pem": "-----BEGIN PRIVATE KEY-----\nREDACTED\n",
            "config/.env.staging": env_base + "K8=v8\n",  # line 1 is not in the diff
        },
    )
    subprocess.run(["git", "rm", "-q", "old/.env"], cwd=scope.repo_root, check=True)
    subprocess.run(["git", "commit", "-qm", "drop env"], cwd=scope.repo_root, check=True)
    scope = resolve_scope(scope.repo_root, "main")
    monkeypatch.setattr("preflight_review.tools.base.which", lambda name: None)
    findings, statuses = run_tools(scope, Config())
    mine = sorted((f for f in findings if f.source == "secret-files"), key=lambda f: f.file)
    assert [f.file for f in mine] == [".env.local", "certs/server.pem", "config/.env.staging"]
    for f in mine:
        assert f.lane == Lane.inline and f.verdict == Verdict.SKIPPED
        assert f.severity == Severity.critical and f.category == Category.secrets
        assert f.evidence[0].quote.startswith("REDACTED")
    st = next(s for s in statuses if s.name == "secret-files")
    assert st.ran is True and st.finding_count == 3 and st.degraded is False


def test_secret_file_filtered_by_path_filters_still_flagged(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x\n"})
    branch(repo, "feature")
    commit(repo, {"local/.env": "A=1\n"}, "env")
    scope = resolve_scope(repo, "main", path_filters=["!local/**"])
    assert "local/.env" in scope.filtered_out
    findings, _ = run_secret_files(scope, Config())
    assert [f.file for f in findings] == ["local/.env"]


def test_partial_tool_is_degraded_but_not_did_not_run(tmp_path):
    from types import SimpleNamespace

    from preflight_review.pipeline import summary_line
    from preflight_review.tools import degraded_label
    from preflight_review.tools.base import ToolStatus

    statuses = [
        ToolStatus("osv-scanner", True, "1", True, 2, note="requirements.txt: failed", degraded=True),
        ToolStatus("ruff", False, None, False, note="not installed", degraded=True),
    ]
    entries = degraded_tools(statuses)
    assert entries == ["osv-scanner: partial: requirements.txt: failed", "ruff: not installed"]
    assert [degraded_label(d) for d in entries] == ["osv-scanner (partial)", "ruff"]
    run = SimpleNamespace(
        reused=False, outcome="complete", degraded=entries,
        policy=SimpleNamespace(report=[], unverified=[]),
    )
    line = summary_line(run, {"md": tmp_path / "r.md"})
    assert line.endswith("DEGRADED: ruff did not run; osv-scanner ran partially")
