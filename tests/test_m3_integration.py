"""M3 exit-criteria integration tests (fake backend, no real model, no real gh)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from preflight_review import cli, prompts
from preflight_review.findings import Lane, Verdict
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.state import load_state

from .conftest import branch, commit, make_repo
from .test_pipeline_e2e import SERVICE_BAD, SERVICE_BASE

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x.py", "symbol_or_hunk": "f", "inputs_considered": "none", "result": "no issue"}],
}


def no_gh(*a, **k):
    return subprocess.CompletedProcess(a, 1, "", "no gh")


def review(repo, be, **kw):
    return run_review(ReviewOptions(repo_root=repo, base="main", **kw), backend=be, gh_runner=no_gh)


def test_companion_e2e(tmp_path):
    repo = make_repo(tmp_path, {"app/calc.py": "X = 0\n", "tests/test_calc.py": "Y = 0\n"})
    for i in range(1, 5):
        commit(repo, {"app/calc.py": f"X = {i}\n", "tests/test_calc.py": f"Y = {i}\n"}, f"both {i}")
    branch(repo, "feature/calc")
    commit(repo, {"app/calc.py": "X = 99\n"}, "calc only")
    run, paths = review(repo, FakeBackend(default=EMPTY))
    assert "tests/test_calc.py usually changes with app/calc.py" in paths["md"].read_text()
    found = [f for f in run.policy.report if f.source == "cochange"]
    assert len(found) == 1
    assert found[0].lane == Lane.nitpick and found[0].verdict == Verdict.SKIPPED


@pytest.fixture
def svc(tmp_path):
    def make(config_yaml):
        r = make_repo(tmp_path, {"app/service.py": SERVICE_BASE, ".preflight/config.yaml": config_yaml})
        branch(r, "feature/x")
        commit(r, {"app/service.py": SERVICE_BAD}, "change service")
        return r
    return make


def test_lens_selection_single(svc):
    repo = svc("version: 1\nlenses: [correctness]\nsecurity_lane: false\n")
    be = FakeBackend(default=EMPTY)
    review(repo, be)
    assert [c.role for c in be.calls if c.role.startswith("finder:")] == ["finder:correctness"]


def test_lens_selection_two_lenses(svc):
    repo = svc("version: 1\nlenses: [correctness, data_concurrency]\nsecurity_lane: false\n")
    be = FakeBackend(default=EMPTY)
    review(repo, be)
    roles = [c.role for c in be.calls]
    assert "finder:data_concurrency" in roles and "finder:correctness" in roles


def test_respond_learn_then_previously_reported(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, {"app/service.py": SERVICE_BASE})
    branch(repo, "feature/x")
    commit(repo, {"app/service.py": SERVICE_BAD}, "change service")
    data = json.loads((FIXTURES / "run1.json").read_text())
    data["__default__"] = EMPTY
    fx = tmp_path / "run1_default.json"
    fx.write_text(json.dumps(data))

    first, _ = review(repo, FakeBackend.from_file(fx))
    sid = first.policy.report[0].stable_id

    monkeypatch.chdir(repo)
    assert cli.main(["respond", "F1", "wrong", "by design", "--learn"]) == 0
    st = load_state(repo).branches["feature/x"]
    assert st.findings[sid].status == "dismissed"
    learnings = (repo / ".preflight" / "learnings.md").read_text()
    assert "- id: L1 · date: " in learnings
    assert "path: app/** · from: F1 (" in learnings

    run, paths = review(repo, FakeBackend.from_file(fx), full=True)
    assert (sid, "dismissed") in [(s, t) for s, t in run.policy.previously_reported]
    assert all(f.stable_id != sid for f in run.policy.report)
    md = paths["md"].read_text()
    assert "Previously reported" in md and f"- {sid}: dismissed" in md


def test_security_lane_report_has_trace(tmp_path):
    repo = make_repo(tmp_path, {"app/api.py": "def health():\n    return 'ok'\n"})
    branch(repo, "feature/sec")
    bad = (
        "import db\n\n\ndef get_customer(request):\n"
        "    customer_id = request.args[\"id\"]\n    cur = db.cursor()\n"
        "    cur.execute(f\"SELECT * FROM customers WHERE id = {customer_id}\")\n"
        "    return cur.fetchall()\n"
    )
    commit(repo, {"app/api.py": bad}, "add lookup")
    _, paths = review(repo, FakeBackend.from_file(FIXTURES / "security.json"))
    assert "Trace:" in paths["md"].read_text()


def test_all_prompt_shas_cover_prompt_names():
    shas = prompts.all_prompt_shas()
    assert set(shas) == set(prompts.PROMPT_NAMES)
    assert {"rerank", "triage", "walkthrough", "judge"} <= set(prompts.PROMPT_NAMES)
