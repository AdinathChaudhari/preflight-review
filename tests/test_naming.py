from __future__ import annotations

import subprocess
from pathlib import Path

from preflight_review.naming import pr_label, report_basename, report_paths, slugify_branch


def _boom(*a, **k):
    raise FileNotFoundError("gh")


def _runner(returncode=0, stdout=""):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


def test_slugify_cases():
    assert slugify_branch("feature/Outbox_Claim") == "feature-outbox-claim"
    assert slugify_branch("--Fix//a  b!!") == "fix-a-b"
    assert slugify_branch("v1.2/x") == "v1-2-x"
    assert slugify_branch("///") == ""


def test_no_gh_falls_back_to_slug(tmp_path):
    label = pr_label(tmp_path, "feature/outbox-claim", gh_runner=_boom)
    name = report_basename("example-app", label, 1) + ".md"
    assert name == "example-app-PR-feature-outbox-claim-preflight-review-01.md"


def test_pr_number_padded(tmp_path):
    label = pr_label(tmp_path, "b", gh_runner=_runner(0, '{"number":7,"state":"OPEN"}'))
    assert report_basename("example-app", label, 1) + ".md" == (
        "example-app-PR07-preflight-review-01.md"
    )


def test_pr_number_wide(tmp_path):
    assert pr_label(tmp_path, "b", gh_runner=_runner(0, '{"number":128,"state":"OPEN"}')) == "PR128"
    assert pr_label(tmp_path, "b", gh_runner=_runner(0, '{"number":42,"state":"OPEN"}')) == "PR42"


def test_gh_nonzero_and_junk(tmp_path):
    assert pr_label(tmp_path, "Feat/X", gh_runner=_runner(1, "")) == "PR-feat-x"
    assert pr_label(tmp_path, "Feat/X", gh_runner=_runner(0, "not a number")) == "PR-feat-x"


def test_gh_called_with_expected_args(tmp_path):
    run = _runner(0, '{"number":3,"state":"OPEN"}')
    pr_label(tmp_path, "b", gh_runner=run)
    cmd, kw = run.calls[0]
    assert cmd == ["gh", "pr", "view", "--json", "number,state"]
    assert kw["cwd"] == tmp_path and kw["timeout"] == 10


def test_merged_pr_falls_back_to_slug(tmp_path):
    run = _runner(0, '{"number":7,"state":"MERGED"}')
    assert pr_label(tmp_path, "Feat/X", gh_runner=run) == "PR-feat-x"


def test_detached_skips_gh(tmp_path):
    run = _runner(0, "7")
    assert pr_label(tmp_path, None, gh_runner=run) == "PR-detached"
    assert run.calls == []


def test_round_padding():
    assert report_basename("r", "PR01", 12) == "r-PR01-preflight-review-12"


def test_report_paths(tmp_path):
    p = report_paths(tmp_path, ".preflight/reviews", "r-PR01-preflight-review-01")
    assert set(p) == {"md", "json", "manifest", "latest"}
    d = tmp_path / ".preflight/reviews"
    assert p["md"] == d / "r-PR01-preflight-review-01.md"
    assert p["json"] == d / "r-PR01-preflight-review-01.json"
    assert p["manifest"] == d / "r-PR01-preflight-review-01.manifest.json"
    assert p["latest"] == d / "latest.md"
    assert all(isinstance(v, Path) and v.is_absolute() for v in p.values())
