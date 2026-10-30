from __future__ import annotations

import pytest

from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import BackendError, ConfigError
from preflight_review.model import FakeBackend
from preflight_review.scope import resolve_scope
from preflight_review.stages.finders import run_finders, validate_lenses
from tests.conftest import branch, commit, make_repo

SERVICE = "\n".join(
    ["def helper(x):", "    return x + 1", ""]
    + ["class Service:", "    def handle(self, req):"]
    + [f"        v{i} = req.get({i})" for i in range(12)]
    + ["        return v0", ""]
)


def raw(file="app/service.py", s=8, e=9, title="Null deref", **kw):
    d = {
        "file": file, "start_line": s, "end_line": e, "caused_by": None,
        "lane": "inline", "category": "correctness", "severity": "major",
        "effort": "quick_win", "title": title, "claim": "claim",
        "failure_scenario": "fs", "why_it_matters": "w", "suggested_fix": "fix",
        "evidence": [{"file": file, "line": s, "quote": "q"}],
        "bug_class_instances": [], "rule_quote": None,
    }
    d.update(kw)
    return d


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feat")
    commit(repo, {"app/service.py": SERVICE}, "add service")
    scope = resolve_scope(repo, "main")
    cfg = Config()
    cfg.cache_warm_seconds = 0
    packet = Packet(text="PACKET", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="x")
    return scope, cfg, packet, RepoIndex(repo, cache=False)


def go(env, backend, lenses=("correctness", "contracts_tests"), system="SYS"):
    scope, cfg, packet, index = env
    return run_finders(packet, list(lenses), cfg, backend, scope, system, index)


def test_fanout_collects_both_lenses(env):
    b = FakeBackend({
        "finder:correctness": {"findings": [raw(title="A")]},
        "finder:contracts_tests": {"findings": [raw(title="B", s=3, e=3)]},
    })
    out = go(env, b)
    assert {c.role for c in b.calls} == {"finder:correctness", "finder:contracts_tests"}
    assert all(c.model == "sonnet" for c in b.calls)
    assert [f.source for f in out.findings] == ["llm:correctness", "llm:contracts_tests"]
    assert set(out.lane_results) == {"finder:correctness", "finder:contracts_tests"}
    assert out.incomplete == []


def test_failing_lane_is_incomplete(env):
    b = FakeBackend({
        "finder:correctness": BackendError("boom"),
        "finder:contracts_tests": {"findings": [raw()]},
    })
    out = go(env, b)
    assert out.incomplete == ["correctness"]
    assert out.lane_results["finder:correctness"].ok is False
    assert len(out.findings) == 1


def test_nonexistent_file_dropped(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw(file="nope.py")]}})
    out = go(env, b, ["correctness"])
    assert out.findings == [] and "does not exist" in out.dropped[0][1]


def test_start_line_zero_dropped(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw(s=0, e=3)]}})
    out = go(env, b, ["correctness"])
    assert out.findings == [] and len(out.dropped) == 1


def test_escaping_path_dropped(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw(file="../x.py"), raw(file="/etc/passwd")]}})
    out = go(env, b, ["correctness"])
    assert out.findings == [] and len(out.dropped) == 2


def test_stable_id_uses_enclosing_symbol(env):
    scope, cfg, packet, index = env
    from preflight_review.findings import stable_id
    b = FakeBackend({"finder:correctness": {"findings": [raw()]}})
    out = go(env, b, ["correctness"])
    f = out.findings[0]
    sym = index.enclosing_symbol("app/service.py", 8)
    assert sym is not None
    assert f.stable_id == stable_id(f, sym.qualname)
    assert f.stable_id != stable_id(f, None)


def test_rule_quote_folded_into_evidence(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw(rule_quote="no bare except")]}})
    f = go(env, b, ["correctness"]).findings[0]
    assert f.evidence[-1].quote == "rule: no bare except"


def test_deep_mode_uses_opus_and_12_turns(env):
    scope, cfg, packet, index = env
    cfg.mode = "deep"
    b = FakeBackend({"finder:correctness": {"findings": []}})
    go(env, b, ["correctness"])
    assert b.calls[0].model == "opus" and b.calls[0].max_turns == 12


def test_validate_lenses():
    assert validate_lenses(["correctness", "data_concurrency"]) == (
        ["correctness", "data_concurrency"],
        [],
    )
    with pytest.raises(ConfigError, match="unknown lens: bogus"):
        validate_lenses(["bogus"])


def test_prompt_and_system_passed_and_outside_files(env):
    b = FakeBackend({"finder:correctness": {"findings": [], "files_read_outside_packet": ["x.py"]}})
    out = go(env, b, ["correctness"], system="MY SYSTEM")
    assert "PACKET" not in b.calls[0].prompt
    assert b.calls[0].system_prompt.startswith("MY SYSTEM") and "PACKET" in b.calls[0].system_prompt
    assert out.files_read_outside_packet == {"correctness": ["x.py"]}


def test_per_call_budget_floor_not_a_tiny_split(env):
    # Real run: 1.50 / (lenses + 2) = 0.25 starved a 4-turn finder; the cap has a floor.
    scope, cfg, packet, index = env
    cfg.budget_usd = 1.5
    b = FakeBackend({"finder:correctness": {"findings": []}, "finder:contracts_tests": {"findings": []}})
    go(env, b)
    assert all(c.max_budget_usd == pytest.approx(0.5) for c in b.calls)
    cfg.budget_usd = 0.2  # never above the run budget
    b = FakeBackend({"finder:correctness": {"findings": []}})
    go(env, b, ["correctness"])
    assert b.calls[0].max_budget_usd == pytest.approx(0.2)
