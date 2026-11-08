"""Per-packet depth end to end: under `mode: auto` only packets holding a crucial path run
deep (finders, security, verification, sweep); the rest run standard (fake backend only)."""

from __future__ import annotations

import json
import subprocess

import pytest

from preflight_review import pipeline
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.stages.finders import MAX_TURNS
from preflight_review.stages.security import SECURITY_MAX_TURNS, SECURITY_MAX_TURNS_DEEP
from preflight_review.stages.verify import VERIFIER_MAX_TURNS, VERIFIER_MAX_TURNS_DEEP

from .conftest import branch, commit, make_repo

# A finder answer with coverage rows, so no lane counts as shallow (a shallow lane is
# retried with deep turns, which would blur the per-packet turn counts).
EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x1.py", "symbol_or_hunk": "changed hunks",
                  "inputs_considered": "the diff", "result": "checked"}],
}
CONFIG = (
    "version: 1\nwalkthrough: false\nconfirm_above_usd: null\ncache_warm_seconds: 0\n"
    "triage:\n  haiku: false\nlearnings:\n  rerank: false\n"
)
MIGRATION = "db/migrations/0001.py"
MIG_LINE = "    return db.run('ALTER TABLE t ADD c')  # MIGRATION_MARKER"
PLAIN_LINE = "    return v + 10  # PLAIN_MARKER"


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


@pytest.fixture(autouse=True)
def _no_tools(monkeypatch):
    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config, *a: ([], []))


def make(tmp_path, crucial: bool = True, config: str = CONFIG):
    base = {
        "app/x1.py": "def one(v):\n    return v + 1\n",
        "app/x2.py": "def two(v):\n    return v + 2\n",
        ".preflight/config.yaml": config,
    }
    new = {
        "app/x1.py": f"def one(v):\n{PLAIN_LINE}\n",
        "app/x2.py": "def two(v):\n    return v + 20  # PLAIN_MARKER\n",
    }
    if crucial:
        base[MIGRATION] = "def migrate(db):\n    return db.run('SELECT 1')\n"
        new[MIGRATION] = f"def migrate(db):\n{MIG_LINE}\n"
    r = make_repo(tmp_path, base)
    branch(r, "feature/depth")
    commit(r, new, "change")
    return r


def _finding(path: str, quote: str) -> dict:
    return {
        "file": path, "start_line": 2, "end_line": 2, "caused_by": None, "lane": "inline",
        "category": "correctness", "severity": "major", "effort": "quick_win",
        "title": f"wrong value in {path}", "claim": f"{path} returns the wrong value",
        "failure_scenario": "v=1 returns 11", "why_it_matters": "wrong totals",
        "suggested_fix": "fix it", "evidence": [{"file": path, "line": 2, "quote": quote}],
        "bug_class_instances": [], "rule_quote": None,
    }


def respond(req):
    deep_packet = "MIGRATION_MARKER" in req.system_prompt
    if req.role.startswith("finder:correctness"):
        if deep_packet:
            return {**EMPTY, "findings": [_finding(MIGRATION, MIG_LINE)]}
        if "PLAIN_MARKER" in req.system_prompt:
            return {**EMPTY, "findings": [_finding("app/x1.py", PLAIN_LINE)]}
    if req.role.startswith("verifier"):
        mig = MIGRATION in req.prompt
        path, quote = (MIGRATION, MIG_LINE) if mig else ("app/x1.py", PLAIN_LINE)
        return {
            "verdict": "CONFIRMED", "evidence": [{"file": path, "line": 2, "quote": quote}],
            "reason": "the value is wrong", "severity_adjusted": None, "confidence": 4,
            "checks_performed": [f"read {path}"],
        }
    return EMPTY


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def test_auto_runs_only_the_crucial_packet_deep(tmp_path):
    r = make(tmp_path)
    be = FakeBackend(default=respond)
    run, paths = review(r, be)
    assert run.outcome == "complete", run.incomplete_reasons
    assert run.config.mode == "auto"
    assert sorted(run.batch_modes.values()) == ["deep", "standard"]
    assert run.crucial_paths == [MIGRATION]

    finders = [c for c in be.calls if c.role.startswith("finder:")]
    deep = [c for c in finders if "MIGRATION_MARKER" in c.system_prompt]
    plain = [c for c in finders if "MIGRATION_MARKER" not in c.system_prompt]
    assert deep and plain
    assert all("PLAIN_MARKER" not in c.system_prompt for c in deep)
    assert {(c.max_turns, c.effort) for c in deep} == {(MAX_TURNS["deep"], "high")}
    assert {(c.max_turns, c.effort) for c in plain} == {(MAX_TURNS["standard"], "medium")}

    sec = [c for c in be.calls if c.role.startswith("security")]
    by_packet = {"MIGRATION_MARKER" in c.system_prompt: (c.max_turns, c.effort) for c in sec}
    assert by_packet == {
        True: (SECURITY_MAX_TURNS_DEEP, "high"), False: (SECURITY_MAX_TURNS, "medium"),
    }

    # Each packet's findings are verified at that packet's depth.
    votes = [c for c in be.calls if c.role.startswith("verifier")]
    mig_votes = {(c.max_turns, c.effort) for c in votes if MIGRATION in c.prompt}
    plain_votes = {(c.max_turns, c.effort) for c in votes if MIGRATION not in c.prompt}
    assert mig_votes == {(VERIFIER_MAX_TURNS_DEEP, "high")}
    assert plain_votes == {(VERIFIER_MAX_TURNS, "medium")}

    # One sweep, over the deep packet only (and with its packet, being the only one).
    sweeps = [c for c in be.calls if c.role.startswith("sweep")]
    assert len(sweeps) == 1
    # Changed-file lines are "- <path>"; known-finding lines are "- <path>:<line> ...".
    assert f"- {MIGRATION}\n" in sweeps[0].prompt
    assert "- app/x1.py\n" not in sweeps[0].prompt and "- app/x2.py\n" not in sweeps[0].prompt
    assert "MIGRATION_MARKER" in sweeps[0].system_prompt
    assert sweeps[0].max_turns == MAX_TURNS["deep"]

    manifest = json.loads(paths["manifest"].read_text())
    assert manifest["mode_requested"] == "auto" and manifest["mode_resolved"] == "mixed"
    md = paths["md"].read_text()
    assert f"Mode: auto -> deep for 1 packet(s) (1 file(s): {MIGRATION}), standard for 1" in md
    assert len(run.policy.report) == 2


def test_no_sweep_when_no_packet_is_deep(tmp_path):
    r = make(tmp_path, crucial=False)
    be = FakeBackend(default=respond)
    run, _ = review(r, be)
    assert set(run.batch_modes.values()) == {"standard"}
    assert not any(c.role.startswith("sweep") for c in be.calls)
    assert "sweep" not in run.models_used


def test_explicit_deep_makes_every_packet_deep(tmp_path):
    r = make(tmp_path)
    be = FakeBackend(default=respond)
    run, paths = review(r, be, mode="deep")
    assert set(run.batch_modes.values()) == {"deep"} and run.crucial_paths == []
    finders = [c for c in be.calls if c.role.startswith("finder:")]
    assert {c.max_turns for c in finders} == {MAX_TURNS["deep"]}
    sweeps = [c for c in be.calls if c.role.startswith("sweep")]
    assert len(sweeps) == 1
    for p in (MIGRATION, "app/x1.py", "app/x2.py"):
        assert f"- {p}\n" in sweeps[0].prompt
    assert json.loads(paths["manifest"].read_text())["mode_resolved"] == "deep"
    assert "Mode: deep (set explicitly)" in paths["md"].read_text()


def test_explicit_standard_makes_every_packet_standard(tmp_path):
    r = make(tmp_path)
    be = FakeBackend(default=respond)
    run, _ = review(r, be, mode="standard")
    assert set(run.batch_modes.values()) == {"standard"}
    assert not any(c.role.startswith("sweep") for c in be.calls)


def test_config_drift_note_when_branch_edits_path_filters(tmp_path, capsys):
    r = make(tmp_path, crucial=False)
    commit(r, {".preflight/config.yaml": CONFIG + "path_filters: ['!vendor/**']\n"}, "cfg")
    run, paths = review(r, FakeBackend(default=EMPTY))
    note = (
        ".preflight/config.yaml on this branch differs from main in: path_filters; "
        "this review uses the main version until it is merged"
    )
    assert note in run.notes
    assert note in capsys.readouterr().err
    assert note in paths["md"].read_text()
    assert "!vendor/**" not in run.config.path_filters  # still the base config


def test_no_drift_note_when_config_matches_base(tmp_path):
    r = make(tmp_path, crucial=False)
    run, _ = review(r, FakeBackend(default=EMPTY))
    assert not any("differs from main" in n for n in run.notes)
