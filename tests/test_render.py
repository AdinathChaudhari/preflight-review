from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from preflight_review import render as render_pkg
from preflight_review.config import Config
from preflight_review.findings import (
    Category, Effort, Evidence, Finding, Lane, Severity, Verdict,
)
from preflight_review.model.backend import ModelResult
from preflight_review.naming import report_paths
from preflight_review.prompts import all_prompt_shas
from preflight_review.render import RunResult, write_outputs
from preflight_review.render.agent_prompt import PREAMBLE, agent_prompt, aggregate_prompt
from preflight_review.render.json_out import render_json, write_ndjson
from preflight_review.render.manifest import build_manifest
from preflight_review.render.report import render_report
from preflight_review.scope import resolve_scope
from preflight_review.stages.policy import PolicyResult
from preflight_review.tools.base import ToolStatus

from .conftest import branch, commit, make_repo

SECTIONS = [
    "## Not reviewed (", "## Walkthrough", "## Findings (", "## Unverified (", "## Previously reported (still tracked)",
    "## Pre-existing (hidden,", "## Suppressed by learnings", "## Tool output", "## Run",
    "## Respond", "## Aggregate prompt",
]


@pytest.fixture
def scope(tmp_path):
    repo = make_repo(tmp_path, {"app/x.py": "a = 1\n"})
    branch(repo, "feature/x")
    commit(repo, {"app/x.py": "a = 1\nb = 2\n", "app/y.py": "c = 3\n"}, "change")
    return resolve_scope(repo, "main")


def _f(**kw):
    base = dict(
        file="app/x.py", start_line=149, end_line=158, title="Job claim not atomic",
        claim="SELECT then UPDATE races.", severity=Severity.major, category=Category.data_integrity,
        effort=Effort.quick_win, why_it_matters="Double processing.", suggested_fix="Use FOR UPDATE SKIP LOCKED.",
        stable_id="aaa111", verdict=Verdict.CONFIRMED, verifier_reason="read the code",
        verifier_checks=["read x.py", "grep claim"],
        evidence=[Evidence("app/x.py", 151, "job = session.get(...)")],
        verifier_evidence=[Evidence("app/x.py", 156, "job.status = 'claimed'")],
    )
    base.update(kw)
    return Finding(**base)


def _run(scope, policy=None, **kw):
    if policy is None:
        confirmed = _f()
        secret = _f(file="app/y.py", start_line=3, end_line=3, title="Secret", claim="key leaked",
                    severity=Severity.critical, category=Category.secrets, source="gitleaks",
                    verdict=Verdict.SKIPPED, stable_id="bbb222", lane=Lane.outside_diff,
                    evidence=[], verifier_evidence=[])
        unv = _f(title="Maybe", stable_id="ccc333", verdict=Verdict.UNKNOWN, severity=Severity.minor,
                 file="app/z.py", start_line=2, end_line=2)
        pre = _f(title="Old", stable_id="ddd444", lane=Lane.pre_existing)
        policy = PolicyResult(report=[confirmed, secret], unverified=[unv], pre_existing=[pre],
                              previously_reported=[("eee555", "dismissed")])
    args = dict(
        scope=scope, config=Config(), packet=None, policy=policy,
        tool_statuses=[ToolStatus("ruff", False, None, False, note="uv tool install ruff")],
        walkthrough={"summary": "adds x"},
        lane_results={"finder:correctness": ModelResult(True, {}, "success", cost_usd=0.1, model="sonnet"),
                      "verifier:aaa111": ModelResult(True, {}, "success", cost_usd=0.02, model="opus")},
        cost_usd=0.0123, duration_s=4.2, round=1, pr_label="PR-feature-x", basename="repo-PR-feature-x-preflight-review-01",
        outcome="complete", incomplete_reasons=[], models_used={"finder": "sonnet"},
        previous_report=None, backend_name="fake", test_cmd="pytest -q",
        suppressed=[(_f(title="Sup", stable_id="fff666"), "L4")],
    )
    args.update(kw)
    return RunResult(**args)


def test_sections_in_order(scope):
    md = render_report(_run(scope))
    pos = [md.index(s) for s in SECTIONS]
    assert pos == sorted(pos)
    assert md.startswith("# Preflight review — repo PR-feature-x round 01 — feature/x @ ")
    assert "Result: complete" in md and "Merge risk: high" in md


def test_finding_block_lines(scope):
    md = render_report(_run(scope))
    assert "### F1 · Major · Data integrity · quick win · app/x.py:149-158" in md
    assert "### F2 · Critical · Secrets · quick win · app/y.py:3\n" in md
    for line in ["Title: Job claim not atomic", "Claim: SELECT then UPDATE races.", "Evidence:",
                 "- `app/x.py:151` `job = session.get(...)`",
                 "- Verifier: CONFIRMED — read the code; checked: read x.py; grep claim",
                 "- `app/x.py:156` `job.status = 'claimed'`", "Why it matters: Double processing.",
                 "Suggested fix: Use FOR UPDATE SKIP LOCKED.", "Stable id: aaa111",
                 "- Verifier: SKIPPED — deterministic tool finding"]:
        assert line in md
    assert "caused_by:" not in md


def test_prompt_blocks_counted(scope):
    run = _run(scope)
    md = render_report(run)
    assert md.count("**Prompt for AI agent**") == 3
    assert md.count(PREAMBLE) == 4  # 3 blocks + aggregate
    assert "Review comment at @app/x.py around lines 149 - 158:" in md
    assert "After applying the fix, run `pytest -q`." in md


def test_agent_prompt_shape():
    p = agent_prompt(_f())
    assert p == f"{PREAMBLE}\n\nReview comment at @app/x.py around lines 149 - 158:\nUse FOR UPDATE SKIP LOCKED."
    assert "Use the claim" in agent_prompt(_f(suggested_fix="", claim="Use the claim"))


def test_aggregate_prompt_groups():
    a = _f()
    b = _f(file="app/y.py", lane=Lane.nitpick, suggested_fix="")
    c = _f(lane=Lane.pre_existing)
    out = aggregate_prompt([a, b, c])
    assert out.count(PREAMBLE) == 1
    assert out.index("Inline comments:") < out.index("Nitpick comments:")
    assert "Outside diff comments:" not in out
    assert "In @app/x.py:\n- Around lines 149 - 158: Use FOR UPDATE SKIP LOCKED." in out
    assert aggregate_prompt([]) == "none"


def test_empty_sections_none(scope):
    run = _run(scope, policy=PolicyResult(), tool_statuses=[], walkthrough=None, suppressed=[],
               lane_results={})
    md = render_report(run)
    pos = [md.index(s) for s in SECTIONS]
    assert pos == sorted(pos)
    assert "## Findings (0 confirmed" in md and "Merge risk: low" in md
    for header in ["## Not reviewed (0)", "## Walkthrough", "## Unverified (0)", "## Previously reported (still tracked)",
                   "## Suppressed by learnings", "## Tool output"]:
        assert re.search(re.escape(header) + r"\n\nnone\n", md), header
    assert re.search(r"## Findings \(.*\)\n\nDropped: 0 \(see .*\.dropped\.jsonl\)\n\nnone\n", md)
    assert "Aggregate prompt\n\n```text\nnone\n```" in md


def test_tool_status_and_reused(scope):
    md = render_report(_run(scope, reused=True, previous_report="x-01.md"))
    assert "| ruff | - | not installed (uv tool install ruff) | 0 |" in md
    assert "Result: reused (diff unchanged since x-01.md)" in md


def test_show_pre_existing(scope):
    hidden = render_report(_run(scope))
    shown = render_report(_run(scope, show_pre_existing=True))
    assert "Title: Old" not in hidden and "Title: Old" in shown


def test_json_keys_and_dumps(scope):
    d = render_json(_run(scope))
    assert set(d) == {
        "schema_version", "repo", "branch", "pr_label", "round", "base_sha", "head_sha", "diff_hash",
        "generated_at", "outcome", "incomplete_reasons", "scope", "findings", "unverified",
        "previously_reported", "pre_existing_count", "pre_existing", "suppressed", "dropped", "tools",
        "walkthrough", "cost", "manifest", "merge_risk", "merge_risk_detail", "degraded",
    }
    json.dumps(d)
    assert d["findings"][0]["agent_prompt"].startswith(PREAMBLE)
    assert d["pre_existing"] == [] and d["pre_existing_count"] == 1
    assert d["suppressed"] == [{"stable_id": "fff666", "learning_id": "L4"}]
    assert len(d["cost"]["calls"]) == 2


def test_ndjson_sequence(scope, tmp_path):
    path = tmp_path / "o.ndjson"
    write_ndjson(_run(scope), path)
    ev = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["type"] for e in ev] == ["review_context", "finding", "finding", "complete"]
    assert ev[1]["fileName"] == "app/x.py" and ev[1]["severity"] == "major"
    assert ev[1]["codegenInstructions"].startswith(PREAMBLE)
    assert ev[-1] == {"type": "complete", "findings": 2, "outcome": "complete"}


def test_manifest(scope):
    m = build_manifest(_run(scope))
    assert m["prompt_shas"] == all_prompt_shas()
    assert m["packet_sha"] is None and m["backend"] == "fake" and m["models"] == {"finder": "sonnet"}
    assert "finder" in m["schema_shas"]


def test_write_outputs_and_latest(scope, tmp_path):
    out = tmp_path / "reviews"
    paths = report_paths(tmp_path, "reviews", "r-01")
    res = write_outputs(_run(scope), paths)
    assert set(res) == {"md", "json", "manifest", "ndjson", "dropped", "latest"}
    assert all(p.exists() or p.is_symlink() for p in res.values())
    assert res["latest"].is_symlink() and not os.path.isabs(os.readlink(res["latest"]))
    json.loads(res["json"].read_text())
    paths2 = report_paths(tmp_path, "reviews", "r-02")
    res2 = write_outputs(_run(scope, round=2), paths2)
    assert res2["latest"].resolve() == (out / "r-02.md").resolve()
    assert "round 02" in res2["latest"].read_text()


def test_symlink_fallback_copies(scope, tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("no symlinks")
    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(os, "symlink", boom)
    res = write_outputs(_run(scope), report_paths(tmp_path, "reviews", "r-01"))
    assert not res["latest"].is_symlink()
    assert res["latest"].read_text() == res["md"].read_text()


def test_walkthrough_prose_and_triaged_line(scope):
    w = {"summary": "adds x", "changes_by_theme": [{"theme": "T", "files": ["a", "b"], "description": "d"}],
         "effort_1_to_5": 3, "merge_risk": "Moderate", "merge_risk_sentence": "wide."}
    md = render_report(_run(scope, walkthrough=w, triaged_out=[scope.files[0].path]))
    assert "adds x\n\n### Changes\n\n- **T** (a, b): d" in md
    assert "Effort: 3/5" in md and "Model merge risk: Moderate — wide." in md
    assert f"- {scope.files[0].path} — triaged as trivial" in md and "Merge risk:" in md


def _three_file_scope(scope, paths=("a.py", "b.py", "c.py")):
    from dataclasses import replace
    fcs = [replace(scope.files[0], path=p) for p in paths]
    return replace(scope, files=fcs, filtered_out=[])


def test_not_reviewed_scope_line_and_section(scope):
    sc = _three_file_scope(scope)
    run = _run(sc, reviewed_paths=["a.py"], triaged_out=["c.py"], triage_notes={"c.py": "docs only"},
               not_reviewed=[("b.py", "run budget exhausted (cap 1.00)")], batches=2,
               packet_shas=["s1", "s2"])
    md = render_report(run)
    assert "reviewed 1/3, not reviewed: 2 (see Not reviewed)" in md
    assert "## Not reviewed (2)\n\n2 source\n- b.py — run budget exhausted (cap 1.00) [risk: source]\n- c.py — triaged as trivial: docs only [risk: source]" in md
    assert md.index("Result:") < md.index("## Not reviewed") < md.index("## Walkthrough")
    assert "- Packet sha: s1 (2 packets)" in md and "- Batches: 2" in md
    js = render_json(run)["scope"]
    assert js["reviewed"] == 1 and js["total"] == 3 and js["batches"] == 2
    assert js["not_reviewed"] == [
        {"path": "b.py", "reason": "run budget exhausted (cap 1.00)", "risk": "source"},
        {"path": "c.py", "reason": "triaged as trivial: docs only", "risk": "source"},
    ]
    assert build_manifest(run)["packet_shas"] == ["s1", "s2"]


def test_legacy_reviewed_count_and_none(scope):
    sc = _three_file_scope(scope)
    md = render_report(_run(sc, triaged_out=["c.py"]))
    assert "reviewed 2/3, not reviewed: 1 (see Not reviewed)" in md
    md = render_report(_run(sc))
    assert "reviewed 3/3, not reviewed: none" in md and "## Not reviewed (0)\n\nnone" in md
    assert "- Batches:" not in md


def test_not_reviewed_dedupe_and_filter(scope):
    from dataclasses import replace
    sc = replace(_three_file_scope(scope), filtered_out=["uv.lock", "b.py"])
    md = render_report(_run(sc, not_reviewed=[("b.py", "oversized")]))
    assert "- b.py — oversized [risk: source]" in md
    assert "- uv.lock" not in md and "Excluded by path filters (2): uv.lock, b.py" in md
    assert md.count("- b.py —") == 1


def test_not_reviewed_truncates_at_300(scope):
    from dataclasses import replace
    nr = [(f"f{i}.py", "binary file") for i in range(305)]
    sc = replace(scope, files=[replace(scope.files[0], path=p) for p, _ in nr], filtered_out=[])
    scope_ = sc
    md = render_report(_run(scope_, not_reviewed=nr))
    assert "## Not reviewed (305)" in md
    assert "- f299.py — binary file [risk: source]" in md and "- f300.py" not in md
    assert "- ... and 5 more (full list in the JSON report)" in md
    assert len(render_json(_run(scope_, not_reviewed=nr))["scope"]["not_reviewed"]) == 305


def test_previously_reported_details(scope):
    pol = PolicyResult(previously_reported=[("k1", "open"), ("k2", "dismissed")],
                       previous_info={"k1": ("Race", "app/x.py", 12)})
    run = _run(scope, policy=pol)
    md = render_report(run)
    assert "- k1: open — Race (app/x.py:12)" in md
    assert "- k2: dismissed — (details not recorded)" in md
    pr = render_json(run)["previously_reported"]
    assert pr[0] == {"stable_id": "k1", "status": "open", "title": "Race", "file": "app/x.py", "line": 12}
    assert pr[1] == {"stable_id": "k2", "status": "dismissed", "title": None, "file": None, "line": None}


def test_degraded_banner_and_tool_row(scope):
    st = ToolStatus("ruff", False, None, False, note="not installed", degraded=True)
    run = _run(scope, tool_statuses=[st], degraded=["ruff: not installed"])
    md = render_report(run)
    assert "> **DEGRADED:** ruff: not installed." in md
    assert "Result: complete (DEGRADED: ruff)" in md
    assert "| ruff | - | DEGRADED: not installed | 0 |" in md
    assert render_json(run)["degraded"] == ["ruff: not installed"]
    clean = render_report(_run(scope))
    assert "DEGRADED" not in clean and "Result: complete\n" in clean


def test_unverified_section_collapsed(scope):
    md = render_report(_run(scope))
    sec = md[md.index("## Unverified (1)"):md.index("## Dropped (")]
    assert "<details>" in sec and "</details>" in sec
    assert sec.index("<summary>1 finding(s)") < sec.index("Title: Maybe") < sec.index("</details>")
    empty = render_report(_run(scope, policy=PolicyResult()))
    sec = empty[empty.index("## Unverified (0)"):empty.index("## Dropped (")]
    assert "<details>" not in sec and "none" in sec
