from __future__ import annotations

import pytest

from preflight_review import prompts
from preflight_review.config import Config
from preflight_review.context.packer import Packet
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import ConfigError
from preflight_review.model import FakeBackend
from preflight_review.scope import resolve_scope
from preflight_review.stages.finders import (
    LENSES,
    run_finders,
    validate_lens_config,
    validate_lenses,
)
from tests.conftest import branch, commit, make_repo

NEW_LENSES = ["data_concurrency", "rules_compliance"]


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


def test_lens_registry_has_four_names():
    assert set(LENSES) == {
        "correctness", "contracts_tests", "data_concurrency", "rules_compliance",
    }
    assert len(LENSES) == 4


def test_unknown_lens_raises():
    with pytest.raises(ConfigError):
        validate_lenses(["bogus"])


def test_validate_lens_config_returns_runnable():
    cfg = Config()
    assert validate_lens_config(cfg) == list(cfg.lenses)


def test_single_lens_one_call(env):
    scope, cfg, packet, index = env
    cfg.lenses = ["correctness"]
    b = FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    run_finders(packet, cfg.lenses, cfg, b, scope, "SYS", index)
    assert [c.role for c in b.calls] == ["finder:correctness"]


def test_extra_lens_adds_call(env):
    b = FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    go(env, b, ["correctness", "data_concurrency"])
    assert {c.role for c in b.calls} == {"finder:correctness", "finder:data_concurrency"}


def test_defaults_run_four_lenses(env):
    scope, cfg, packet, index = env
    b = FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    out = run_finders(packet, cfg.lenses, cfg, b, scope, "SYS", index)
    roles = {c.role for c in b.calls}
    assert roles == {
        "finder:correctness", "finder:contracts_tests",
        "finder:data_concurrency", "finder:rules_compliance",
    }
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
    b = FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    run_finders(packet, list(LENSES), cfg, b, scope, "SYS", index)
    assert len(b.calls) == len(LENSES)
    systems = {c.system_prompt for c in b.calls}
    assert len(systems) == 1
    system = systems.pop()
    assert system.startswith("SYS") and "PACKET" in system
    assert all("PACKET" not in c.prompt for c in b.calls)
    assert b.calls[0].role == f"finder:{list(LENSES)[0]}"


def test_lane_suffix_changes_keys_not_roles(env):
    scope, cfg, packet, index = env
    b = FakeBackend(default={"findings": [], "files_read_outside_packet": []})
    out = run_finders(packet, ["correctness", "data_concurrency"], cfg, b, scope, "SYS", index, lane_suffix="@b2")
    assert set(out.lane_results) == {"finder:correctness@b2", "finder:data_concurrency@b2"}
    assert {c.role for c in b.calls} == {"finder:correctness", "finder:data_concurrency"}
    assert out.incomplete == []
