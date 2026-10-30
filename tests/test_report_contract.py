"""Report contract: what the .json beside every report promises to the fixing agent.

`check_report` is used on the fake-backend pipeline output (always) and on a real smoke-review
report when PREFLIGHT_REAL_REPORT_JSON points at one (see docs/measurements.md).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review
from preflight_review.render.agent_prompt import PREAMBLE

from .conftest import branch, commit, make_repo

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
STABLE_ID = re.compile(r"^[0-9a-f]{12}$")

BASE = '"""Order pricing."""\n\n\ndef apply_discount(subtotal, rate):\n    discounted = subtotal - subtotal * rate\n    return discounted\n'
BAD = BASE.replace("return discounted", "return discounted * (1 - rate)")


def _has_quote(items) -> bool:
    return any(isinstance(e, dict) and str(e.get("quote", "")).strip() for e in items or [])


def check_report(json_path: Path) -> None:
    data = json.loads(Path(json_path).read_text())
    for f in data["findings"]:
        sid = f.get("stable_id")
        where = f"finding {sid} ({f.get('file')}:{f.get('start_line')})"
        assert f["verdict"] in ("CONFIRMED", "SKIPPED"), f"{where}: verdict {f['verdict']}"
        if f["verdict"] == "CONFIRMED":
            # A confirmed finding needs the verifier's own quote, not just the finder's.
            assert _has_quote(f.get("verifier_evidence")), f"{where}: no verifier_evidence quote"
        else:
            # SKIPPED findings never reach a verifier (e.g. tool signals); finder evidence counts.
            assert _has_quote(f.get("verifier_evidence")) or _has_quote(f.get("evidence")), (
                f"{where}: no evidence quote"
            )
        assert PREAMBLE in f.get("agent_prompt", ""), f"{where}: agent_prompt lacks the preamble"
        assert isinstance(sid, str) and STABLE_ID.match(sid), f"{where}: bad stable_id {sid!r}"


@pytest.fixture
def report_json(tmp_path):
    repo = make_repo(tmp_path, {"app/service.py": BASE, "README.md": "hi\n"})
    branch(repo, "feature/x")
    commit(repo, {"app/service.py": BAD}, "change service")
    no_gh = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no gh")
    backend = FakeBackend.from_file(FIXTURES / "run1.json")
    _, paths = run_review(ReviewOptions(repo_root=repo, base="main"), backend=backend, gh_runner=no_gh)
    return paths["json"]


def test_fake_pipeline_report_meets_contract(report_json):
    check_report(report_json)
    data = json.loads(Path(report_json).read_text())
    assert len(data["findings"]) >= 1  # the check is not vacuous


@pytest.mark.skipif(
    not os.environ.get("PREFLIGHT_REAL_REPORT_JSON"),
    reason="set PREFLIGHT_REAL_REPORT_JSON to a real review's .json to check it",
)
def test_real_report_meets_contract():
    path = Path(os.environ["PREFLIGHT_REAL_REPORT_JSON"])
    check_report(path)
    assert json.loads(path.read_text())["findings"], "real report has no findings"


def _hand_built(tmp_path, **override):
    finding = {
        "file": "a.py", "start_line": 1, "verdict": "CONFIRMED", "stable_id": "0123456789ab",
        "evidence": [{"file": "a.py", "line": 1, "quote": "x = 1"}],
        "verifier_evidence": [{"file": "a.py", "line": 1, "quote": "x = 1"}],
        "agent_prompt": PREAMBLE + "\n\nbody",
    }
    finding.update(override)
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"findings": [finding]}))
    return p


def test_check_report_accepts_a_valid_hand_built_report(tmp_path):
    check_report(_hand_built(tmp_path))


@pytest.mark.parametrize(
    "override",
    [
        {"verdict": "REFUTED"},
        {"verdict": "UNKNOWN"},
        {"verifier_evidence": [{"file": "a.py", "line": 1, "quote": "  "}]},
        {"verifier_evidence": []},
        {"agent_prompt": "no preamble here"},
        {"stable_id": "not-a-hex-id"},
    ],
)
def test_check_report_rejects_violations(tmp_path, override):
    with pytest.raises(AssertionError):
        check_report(_hand_built(tmp_path, **override))
