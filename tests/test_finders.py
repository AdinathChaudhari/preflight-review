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

EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x.py", "symbol_or_hunk": "f", "inputs_considered": "none", "result": "no issue"}],
}


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
    cfg = Config(mode="standard")
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
    assert all(c.model == "sonnet" for c in b.calls)  # every lens: sonnet in every mode
    assert [f.source for f in out.findings] == ["llm:correctness", "llm:contracts_tests"]
    assert set(out.lane_results) == {"finder:correctness", "finder:contracts_tests"}
    assert out.incomplete == []


def test_rules_lens_runs_on_rules_tier(env):
    b = FakeBackend({
        "finder:correctness": EMPTY,
        "finder:rules_compliance": EMPTY,
    })
    go(env, b, ["correctness", "rules_compliance"])
    models = {c.role: c.model for c in b.calls}
    assert models == {"finder:correctness": "sonnet", "finder:rules_compliance": "sonnet"}


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


def test_deep_mode_uses_sonnet_18_turns_and_high_effort(env):
    scope, cfg, packet, index = env
    cfg.mode = "deep"
    b = FakeBackend({"finder:correctness": {"findings": []}})
    go(env, b, ["correctness"])
    c = b.calls[0]
    assert c.model == "sonnet" and c.max_turns == 18 and c.effort == "high"


def test_effort_follows_mode_and_config(env):
    _scope, cfg, _packet, _index = env
    b = FakeBackend({"finder:correctness": {"findings": []}})
    go(env, b, ["correctness"])
    assert b.calls[0].effort == "medium"
    cfg.mode, cfg.effort.quick = "quick", None
    b = FakeBackend({"finder:correctness": {"findings": []}})
    go(env, b, ["correctness"])
    assert b.calls[0].effort is None


def test_no_run_budget_means_no_per_call_cap(env):
    _scope, cfg, _packet, _index = env
    assert cfg.budget_usd is None
    b = FakeBackend(default=EMPTY)
    go(env, b)
    assert len(b.calls) == 2 and all(c.max_budget_usd is None for c in b.calls)


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


# --- shallow lens: an empty result must show its work -----------------------

SHALLOW = {"findings": [], "files_read_outside_packet": [], "coverage": []}


def code_env(env):
    scope, cfg, packet, index = env
    packet.files = ["app/service.py"]
    return scope, cfg, packet, index


class TurnsBackend(FakeBackend):
    """FakeBackend that reports a fixed num_turns on every result."""

    def __init__(self, responses, turns):
        super().__init__(responses)
        self.turns = turns

    def run(self, req):
        res = super().run(req)
        res.num_turns = self.turns
        return res


def test_shallow_lens_retried_on_deep_tier(env):
    b = FakeBackend({"finder:correctness": [SHALLOW, {"findings": [raw()], "coverage": []}]})
    out = go(code_env(env), b, ["correctness"])
    assert len(b.calls) == 2
    first, retry = b.calls
    assert first.model == "sonnet" and first.max_turns == 6 and "## Retry" not in first.prompt
    assert retry.role == "finder:correctness"
    assert retry.model == "sonnet" and retry.max_turns == 18 and "## Retry" in retry.prompt
    assert set(out.lane_results) == {"finder:correctness", "finder:correctness#retry"}
    assert [f.source for f in out.findings] == ["llm:correctness"]
    assert out.incomplete == []
    assert len(out.shallow) == 1 and "shallow lens: correctness" in out.shallow[0]
    assert "also shallow" not in out.shallow[0]


def test_shallow_retry_runs_once_and_notes_second_shallow(env):
    b = FakeBackend({"finder:correctness": SHALLOW})
    out = go(code_env(env), b, ["correctness"])
    assert len(b.calls) == 2 and out.incomplete == []
    assert "retry was also shallow" in out.shallow[0]


def test_shallow_rules_lens_retries_on_deep_tier(env):
    b = FakeBackend({"finder:rules_compliance": SHALLOW})
    go(code_env(env), b, ["rules_compliance"])
    assert [c.model for c in b.calls] == ["sonnet", "sonnet"]  # deep tier: more turns


def test_failed_shallow_retry_marks_lens_incomplete(env):
    b = FakeBackend({"finder:correctness": [SHALLOW, BackendError("boom")]})
    out = go(code_env(env), b, ["correctness"])
    assert out.incomplete == ["correctness"]
    assert "retry failed" in out.shallow[0]


def test_coverage_or_outside_reads_or_findings_mean_not_shallow(env):
    for resp in (
        EMPTY,
        {"findings": [], "files_read_outside_packet": ["app/other.py"], "coverage": []},
    ):
        b = FakeBackend({"finder:correctness": resp})
        out = go(code_env(env), b, ["correctness"])
        assert len(b.calls) == 1 and out.shallow == []


def test_findings_without_coverage_noted_not_retried(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw()], "files_read_outside_packet": []}})
    out = go(code_env(env), b, ["correctness"])
    assert len(b.calls) == 1 and len(out.findings) == 1
    assert out.shallow == [
        (
            "shallow lens: correctness returned 1 finding(s) but no coverage; nothing shows "
            "the rest of the batch was reviewed"
        )
    ]
    cov = [{"file": "app/service.py", "symbol_or_hunk": "f", "inputs_considered": "-",
            "result": "finding"}]
    b = FakeBackend({"finder:correctness": {"findings": [raw()], "coverage": cov}})
    assert go(code_env(env), b, ["correctness"]).shallow == []


def test_docs_only_packet_retries_only_on_early_stop(env):
    scope, cfg, packet, index = env
    packet.files = ["docs/guide.md"]
    b = TurnsBackend({"finder:correctness": SHALLOW}, turns=7)
    out = run_finders(packet, ["correctness"], cfg, b, scope, "SYS", index)
    assert len(b.calls) == 1 and out.shallow == []
    b = TurnsBackend({"finder:correctness": SHALLOW}, turns=2)
    out = run_finders(packet, ["correctness"], cfg, b, scope, "SYS", index)
    assert len(b.calls) == 2 and len(out.shallow) == 1


def test_code_packet_retries_empty_coverage_even_after_many_turns(env):
    scope, cfg, packet, index = code_env(env)
    b = TurnsBackend({"finder:correctness": SHALLOW}, turns=9)
    out = run_finders(packet, ["correctness"], cfg, b, scope, "SYS", index)
    assert len(b.calls) == 2 and len(out.shallow) == 1


def test_finder_confidence_kept_apart_from_security_confidence(env):
    b = FakeBackend({"finder:correctness": {"findings": [raw(finder_confidence=2)]}})
    f = go(env, b, ["correctness"]).findings[0]
    assert f.finder_confidence == 2 and f.confidence is None


def test_finder_schema_requires_coverage_and_allows_finder_confidence():
    from preflight_review.model.schemas import load_schema

    s = load_schema("finder")
    assert "coverage" in s["required"]
    item = s["properties"]["coverage"]["items"]
    assert set(item["required"]) == {"file", "symbol_or_hunk", "inputs_considered", "result"}
    props = s["properties"]["findings"]["items"]["properties"]
    assert props["finder_confidence"] == {"type": "integer", "minimum": 1, "maximum": 5}
    assert "confidence" not in props
