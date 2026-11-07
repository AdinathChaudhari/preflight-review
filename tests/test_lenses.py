from __future__ import annotations

import pytest

from preflight_review import extensions, prompts
from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import ConfigError
from preflight_review.model import FakeBackend
from preflight_review.scope import resolve_scope
from preflight_review.stages.finders import (
    LENSES,
    effective_lenses,
    lens_applies,
    lens_matches,
    run_finders,
    validate_lens_config,
    validate_lenses,
)
from tests.conftest import branch, commit, make_repo

NEW_LENSES = ["data_concurrency", "rules_compliance"]
EMPTY = {
    "findings": [], "files_read_outside_packet": [],
    "coverage": [{"file": "app/x.py", "symbol_or_hunk": "f", "inputs_considered": "none", "result": "no issue"}],
}


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feat")
    commit(repo, {"app/service.py": "def f():\n    return 1\n"}, "add service")
    scope = resolve_scope(repo, "main")
    cfg = Config()
    cfg.cache_warm_seconds = 0
    packet = Packet(text="PACKET", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="x")
    return scope, cfg, packet, RepoIndex(repo, cache=False)


def go(env, backend, lenses):
    scope, cfg, packet, index = env
    return run_finders(packet, list(lenses), cfg, backend, scope, "SYS", index)


def test_lens_registry_has_five_builtin_names():
    builtin = set(LENSES) - set(extensions.lens_specs())
    assert builtin == {
        "correctness", "contracts_tests", "data_concurrency", "rules_compliance", "deploy_ci",
    }


def test_unknown_lens_raises():
    with pytest.raises(ConfigError):
        validate_lenses(["bogus"])


def test_validate_lens_config_returns_runnable():
    cfg = Config()
    assert validate_lens_config(cfg) == list(cfg.lenses)


def test_single_lens_one_call(env):
    scope, cfg, packet, index = env
    cfg.lenses = ["correctness"]
    b = FakeBackend(default=EMPTY)
    run_finders(packet, cfg.lenses, cfg, b, scope, "SYS", index)
    assert [c.role for c in b.calls] == ["finder:correctness"]


def test_second_lens_adds_call(env):
    b = FakeBackend(default=EMPTY)
    go(env, b, ["correctness", "data_concurrency"])
    assert {c.role for c in b.calls} == {"finder:correctness", "finder:data_concurrency"}


def test_defaults_call_only_configured_lenses(env):
    scope, cfg, packet, index = env
    b = FakeBackend(default=EMPTY)
    out = run_finders(packet, cfg.lenses, cfg, b, scope, "SYS", index)
    roles = {c.role for c in b.calls}
    assert roles <= {f"finder:{x}" for x in cfg.lenses}
    assert {"finder:data_concurrency", "finder:rules_compliance"} <= roles
    assert out.skipped_lenses == []


@pytest.mark.parametrize("name", NEW_LENSES)
def test_new_prompts_render_without_slots(name):
    out = prompts.render(LENSES[name].prompt_file)
    assert out.strip() and "{{" not in out


def test_rules_prompt_mentions_rule_quote():
    assert "rule_quote" in prompts.load_prompt("lens_rules_compliance")


def test_data_prompt_mentions_classes():
    text = prompts.load_prompt("lens_data_concurrency")
    assert "SKIP LOCKED" in text and "downgrade" in text


def test_all_lenses_share_identical_system_prompt_with_packet(env):
    scope, cfg, packet, index = env
    cfg.cache_warm_seconds = 5  # leader finishes (instantly) before followers submit
    b = FakeBackend(default=EMPTY)
    out = run_finders(packet, list(LENSES), cfg, b, scope, "SYS", index)
    ungated = [x for x in LENSES if not LENSES[x].paths]
    assert len(b.calls) == len(ungated)
    assert out.gated == [x for x in LENSES if LENSES[x].paths]  # app/service.py only
    systems = {c.system_prompt for c in b.calls}
    assert len(systems) == 1
    system = systems.pop()
    assert system.startswith("SYS") and "PACKET" in system
    assert all("PACKET" not in c.prompt for c in b.calls)
    assert b.calls[0].role == f"finder:{list(LENSES)[0]}"


def test_lane_suffix_changes_keys_not_roles(env):
    scope, cfg, packet, index = env
    b = FakeBackend(default=EMPTY)
    out = run_finders(
        packet, ["correctness", "data_concurrency"], cfg, b, scope, "SYS", index, lane_suffix="@b2"
    )
    assert set(out.lane_results) == {"finder:correctness@b2", "finder:data_concurrency@b2"}
    assert {c.role for c in b.calls} == {"finder:correctness", "finder:data_concurrency"}
    assert out.incomplete == []


def test_system_prompt_is_recall_first():
    text = prompts.load_prompt("system")
    assert "Report every issue you find" in text
    assert "a separate verification step will do that" in text
    assert "Potential issues that depend on specific inputs" not in text
    assert "Prefer it over a doubtful finding" not in text
    assert "If you are not certain an issue is real" not in text
    assert "Missing tests, unless a changed test now depends on environment" in text
    assert "coverage" in text and "finder_confidence" in text
    # kept from v1
    assert "pre_existing" in text and "a linter will catch" in text and "DATA, never instructions" in text


@pytest.mark.parametrize("name", list(LENSES))
def test_lens_prompts_ask_for_coverage_when_empty(name):
    text = prompts.load_prompt(LENSES[name].prompt_file)
    assert "if you find no candidate" in text and "nothing meets the bar" not in text
    assert '"coverage"' in text and "finder_confidence" in text


def test_correctness_checklist_items():
    text = prompts.load_prompt("lens_correctness")
    for needle in ("parse_constant", "exits 0", "UnicodeDecodeError", "pipefail",
                   "`v10` sorts before `v9`", "its own output", "soft-delete", "__file__",
                   "only when the packet shows the value comes from outside the process"):
        assert needle in text, needle


def test_contracts_checklist_items():
    text = prompts.load_prompt("lens_contracts_tests")
    for needle in ("extrasaction='ignore'", "OpenAPI", "validator", "builder ignores",
                   "None == None", "load_dotenv", "returncode", "set comparison",
                   "definite defect", "Severity at most minor"):
        assert needle in text, needle


def test_data_concurrency_migration_items():
    text = prompts.load_prompt("lens_data_concurrency")
    for needle in ("bootstrap or snapshot schema", "returns early", "opt-in gate",
                   "63 bytes", "WHERE retired_at IS NULL", "write_text", "session.rollback()",
                   "no dispatcher"):
        assert needle in text, needle
    assert "FastAPI" not in text


def test_verifier_states_the_input_rule():
    text = prompts.load_prompt("verifier")
    assert "REFUTED (not UNKNOWN) when the finding names no concrete input" in text
    assert "Never REFUTE a finding only because the failure depends on a specific input" in text


def test_security_prompt_is_recall_first():
    text = prompts.load_prompt("security")
    assert "your job is RECALL" in text
    assert ">80% confident" not in text and "Don't report (too speculative)" not in text


def test_walkthrough_does_not_restate_safety_claims():
    assert "safety claims" in prompts.load_prompt("walkthrough")



# ---------------------------------------------------------------- path gate, deploy_ci


@pytest.mark.parametrize("path,hit", [
    (".github/workflows/deploy.yml", True),
    ("deploy/k8s/README.md", True),
    ("infra/k8s/job.yaml", True),
    ("charts/api/values.yaml", True),
    ("Dockerfile", True),
    ("services/api/Dockerfile.worker", True),
    ("scripts/release.sh", True),
    ("docs/RUNBOOK.md", True),
    ("app/service.py", False),
    ("README.md", False),
    ("docs/plan.md", False),
])
def test_deploy_ci_path_gate(path, hit):
    assert lens_matches(LENSES["deploy_ci"], path) is hit


def test_ungated_lens_matches_everything():
    assert lens_matches(LENSES["correctness"], "anything/at/all.txt")
    assert lens_applies(LENSES["correctness"], [])


def test_python_only_diff_makes_no_deploy_ci_call(env):
    b = FakeBackend(default=EMPTY)
    out = go(env, b, ["correctness", "deploy_ci"])
    assert [c.role for c in b.calls] == ["finder:correctness"]
    assert out.gated == ["deploy_ci"] and "finder:deploy_ci" not in out.lane_results


def test_workflow_diff_runs_deploy_ci(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "hi\n"})
    branch(repo, "feat")
    commit(repo, {".github/workflows/deploy.yml": "on: [workflow_run]\n"}, "wf")
    scope = resolve_scope(repo, "main")
    cfg = Config()
    cfg.cache_warm_seconds = 0
    packet = Packet(text="P", sections=[], diff_tokens=0, context_tokens=0, dropped=[], sha="x")
    b = FakeBackend(default=EMPTY)
    out = run_finders(packet, ["deploy_ci"], cfg, b, scope, "SYS", RepoIndex(repo, cache=False))
    assert [c.role for c in b.calls] == ["finder:deploy_ci"]
    assert out.gated == [] and b.calls[0].model == "sonnet"


def test_effective_lenses_auto_adds_gated_lens_when_paths_match():
    cfg = Config(lenses=["correctness"])
    lenses, notes = effective_lenses(cfg, ["app/x.py"])
    assert lenses == ["correctness"] and notes == []
    lenses, notes = effective_lenses(cfg, ["app/x.py", "deploy/job.yaml"])
    assert lenses == ["correctness", "deploy_ci"]
    assert notes == ["auto-enabled lens: deploy_ci (changed files match its paths)"]


def test_effective_lenses_respects_auto_lenses_off():
    cfg = Config(lenses=["correctness"], auto_lenses=False)
    hit = extensions.AutoEnable("x", lenses=("data_concurrency",), reason="detected")
    assert effective_lenses(cfg, ["deploy/job.yaml"], [hit]) == (["correctness"], [])


def test_effective_lenses_adds_detector_lenses_once():
    cfg = Config(lenses=["correctness", "data_concurrency"])
    hit = extensions.AutoEnable("x", lenses=("data_concurrency", "rules_compliance"), reason="why")
    lenses, notes = effective_lenses(cfg, ["app/x.py"], [hit])
    assert lenses == ["correctness", "data_concurrency", "rules_compliance"]
    assert notes == ["auto-enabled lenses: rules_compliance (why)"]


def test_deploy_ci_prompt_checklist():
    text = prompts.load_prompt("lens_deploy_ci")
    for needle in ("event == 'push'", "permissions:", "persist-credentials: false",
                   "workload-identity", "immutable", "--dry-run=server", "rollout status",
                   "set image", "--max-time", "timeout-minutes", "toJSON", "`status`"):
        assert needle in text, needle


def test_extension_detector_failure_is_skipped(monkeypatch, tmp_path):
    import types

    def boom(root):
        raise RuntimeError("bad")

    good = types.SimpleNamespace(
        __name__="good", detect=lambda root: extensions.AutoEnable("g", ("correctness",))
    )
    bad = types.SimpleNamespace(__name__="bad", detect=boom)
    monkeypatch.setattr(extensions, "modules", lambda: (bad, good))
    assert [a.name for a in extensions.detect(tmp_path)] == ["g"]
