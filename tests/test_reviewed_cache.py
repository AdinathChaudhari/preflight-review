"""Reviewed-file cache, blob shas, and the report rendering for 'Reviewed earlier'."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from preflight_review import gitutil, prompts, reviewed_cache
from preflight_review.config import Config, PathInstruction
from preflight_review.depth import Estimate
from preflight_review.render.json_out import render_json
from preflight_review.render.manifest import build_manifest
from preflight_review.render.report import (
    coverage,
    coverage_of,
    mode_line,
    render_report,
)
from preflight_review.reviewed_cache import ReviewedCache, file_key, fingerprint
from preflight_review.scope import resolve_scope

from .conftest import branch, commit, make_repo
from .test_render import _run

LENSES = ["correctness", "contracts_tests"]


def fp(cfg=None, mode="standard", lenses=None, sec=True, guide=""):
    return fingerprint(cfg or Config(), mode, lenses or list(LENSES), sec, guide)


# --- keys ---

def test_key_stable_across_rebase_and_changes_with_old_blob():
    f = fp()
    a = file_key("a.py", "M", "o1", "n1", f)
    assert a == file_key("a.py", "M", "o1", "n1", f)
    assert a != file_key("a.py", "M", "o2", "n1", f)
    assert a != file_key("a.py", "M", "o1", "n2", f)
    assert file_key("a.py", "A", None, "n1", f) != file_key("a.py", "A", "-x", "n1", f)


# --- fingerprint ---

def test_fingerprint_changes(monkeypatch):
    base = fp()
    assert base == fp()
    real = prompts.prompt_sha
    monkeypatch.setattr(prompts, "prompt_sha", lambda n: "x" if n == "system" else real(n))
    assert fp() != base
    monkeypatch.setattr(prompts, "prompt_sha", lambda n: "y" if n == "lens_correctness" else real(n))
    assert fp() != base
    monkeypatch.setattr(prompts, "prompt_sha", real)
    assert fp(mode="deep") != base
    assert fp(lenses=["correctness"]) != base
    assert fp(sec=False) != base
    assert fp(guide="rules") != base
    cfg = Config()
    cfg.models.verifier = "opus"
    assert fp(cfg) != base
    cfg = Config()
    cfg.effort.standard = "max"
    assert fp(cfg) != base
    cfg = Config()
    cfg.path_instructions = [PathInstruction("a/**", "be careful")]
    assert fp(cfg) != base
    cfg = Config()
    cfg.verifier.votes = 1
    assert fp(cfg) != base
    assert fp(lenses=["correctness", "nonexistent", "contracts_tests"]) is not None


def test_fingerprint_ignores_learnings():
    cfg = Config()
    cfg.learnings.cap = 7
    assert fp(cfg) == fp()


# --- cache store ---

def test_put_get_save_roundtrip(tmp_path):
    c = ReviewedCache(tmp_path)
    c.put("k", run="r", round=2, branch="b", mode="deep", findings=["s1"])
    c.save()
    got = ReviewedCache(tmp_path).get("k")
    assert got["round"] == 2 and got["findings"] == ["s1"] and got["run"] == "r"
    assert ReviewedCache(tmp_path).get("nope") is None


def test_corrupt_or_non_dict_loads_empty(tmp_path):
    c = ReviewedCache(tmp_path)
    c.path.parent.mkdir(parents=True)
    c.path.write_text("{not json")
    assert c.get("k") is None
    c.path.write_text("[1,2]")
    assert ReviewedCache(tmp_path).get("k") is None
    c.path.write_text(json.dumps({"k": 5}))
    assert ReviewedCache(tmp_path).get("k") is None


def test_lru_cap_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewed_cache, "MAX_ENTRIES", 3)
    c = ReviewedCache(tmp_path)
    for i in range(6):
        c.put(f"k{i}", run="r", round=1, branch="b", mode="m", findings=[], at=f"2026-01-0{i + 1}T00:00:00")
    c.save()
    c2 = ReviewedCache(tmp_path)
    assert [c2.get(f"k{i}") is not None for i in range(6)] == [False, False, False, True, True, True]


def test_default_cap_is_5000():
    assert reviewed_cache.MAX_ENTRIES == 5000


def test_concurrent_puts(tmp_path):
    c = ReviewedCache(tmp_path)

    def work(n):
        for i in range(50):
            c.put(f"{n}-{i}", run="r", round=1, branch="b", mode="m", findings=[])

    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c.save()
    c2 = ReviewedCache(tmp_path)
    assert all(c2.get(f"{n}-{i}") for n in range(8) for i in range(50))


# --- git blobs ---

def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_blob_shas_match_git(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n", "dir/sp ace é.py": "y = 2\n"})
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"new.py": "z\n"}, "add")
    paths = ["a.py", "dir/sp ace é.py", "new.py", "missing.py"]
    got = gitutil.blob_shas(repo, base, paths)
    assert got["a.py"] == _git(repo, "rev-parse", "HEAD:a.py")
    assert got["dir/sp ace é.py"] == _git(repo, "rev-parse", f"{base}:dir/sp ace é.py")
    assert got["new.py"] is None and got["missing.py"] is None
    assert gitutil.blob_shas(repo, "HEAD", [])  == {}


def test_worktree_blob_sha(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    assert gitutil.worktree_blob_sha(repo, "a.py") == _git(repo, "hash-object", "a.py")
    assert gitutil.worktree_blob_sha(repo, "a.py") == _git(repo, "rev-parse", "HEAD:a.py")
    assert gitutil.worktree_blob_sha(repo, "gone.py") == "D"


# --- report rendering ---

@pytest.fixture
def scope(tmp_path):
    repo = make_repo(tmp_path, {"app/x.py": "a = 1\n"})
    branch(repo, "feature/x")
    commit(repo, {"app/x.py": "a = 1\nb = 2\n", "app/y.py": "c = 3\n"}, "change")
    return resolve_scope(repo, "main")


def test_coverage_adds_up_with_reviewed_earlier(scope):
    cov = coverage(scope, None, [], [], {}, [("app/x.py", "rep-01", 1)])
    assert cov.reviewed_earlier == [("app/x.py", "rep-01", 1)]
    assert "app/x.py" not in cov.reviewed and all(p != "app/x.py" for p, _ in cov.not_reviewed)
    assert len(cov.reviewed) + len(cov.reviewed_earlier) + len(cov.not_reviewed) == cov.total
    cov = coverage(scope, ["app/x.py", "app/y.py"], [("app/y.py", "r")], [], {}, [("app/x.py", "r", 1)])
    assert len(cov.reviewed) + len(cov.reviewed_earlier) + len(cov.not_reviewed) == cov.total


def test_section_only_when_non_empty(scope):
    plain = render_report(_run(scope))
    assert "Reviewed earlier" not in plain
    run = _run(scope, reviewed_earlier=[("app/x.py", "repo-PR-x-preflight-review-01", 1)])
    md = render_report(run)
    assert "1 reviewed earlier (see Reviewed earlier)" in md
    assert "## Reviewed earlier (1)" in md
    assert md.index("## Not reviewed (") < md.index("## Reviewed earlier (") < md.index("## Walkthrough")
    assert "- app/x.py — round 01 (repo-PR-x-preflight-review-01)" in md
    assert "--full" in md
    assert coverage_of(run).reviewed_earlier
    js = render_json(run)
    assert js["scope"]["reviewed_earlier"] == 1
    assert js["scope"]["reviewed_earlier_files"] == [
        {"path": "app/x.py", "report": "repo-PR-x-preflight-review-01", "round": 1}
    ]
    assert js["scope"]["reviewed"] + 1 + len(js["scope"]["not_reviewed"]) == js["scope"]["total"]


def test_mixed_mode_line_and_manifest(scope):
    cfg = Config()
    cfg.mode = "deep"
    run = _run(scope, config=cfg, mode_requested="auto", crucial_paths=["a.py", "b.py", "c.py", "d.py", "e.py"],
               batch_modes={0: "deep", 1: "standard", 2: "standard"})
    line = mode_line(run)
    assert line == "Mode: auto -> deep for 1 packet(s) (5 file(s): a.py, b.py, c.py, +2 more), standard for 2"
    assert build_manifest(run)["mode_resolved"] == "mixed"
    assert build_manifest(run)["batch_modes"] == {"0": "deep", "1": "standard", "2": "standard"}
    cfg2 = Config()
    cfg2.mode = "auto"
    one = _run(scope, config=cfg2, mode_requested="auto", batch_modes={0: "standard", 1: "standard"})
    assert mode_line(one) == "Mode: auto -> standard for all 2 packets (no crucial paths changed)"
    assert build_manifest(one)["mode_resolved"] == "standard"
    assert "batch_modes" not in build_manifest(_run(scope))


def test_estimate_vs_actual_line(scope):
    est = Estimate(mode="deep", usd=12.3, minutes=90, files=3, batches=2)
    md = render_report(_run(scope, estimate=est, duration_s=600.0, cost_usd=4.5))
    assert "Estimate vs actual: ~90 min est., 10 min actual; ~$12 est., $4.50 actual" in md
    assert "Estimate vs actual" not in render_report(_run(scope))
    assert "Estimate vs actual" not in render_report(
        _run(scope, estimate=est, outcome="not_started", incomplete_reasons=["x"])
    )
