import pytest
import yaml

from preflight_review.config import (
    DEFAULT_CONFIG_YAML,
    Config,
    config_from_dict,
    load_config,
    merge_overrides,
)
from preflight_review.errors import ConfigError
from tests.conftest import branch, make_repo, write


def test_defaults():
    c = Config()
    assert c.tools.ruff.select == ["B", "S", "F", "E9", "RUF"]
    assert c.models.verifier_major == "opus"
    assert c.nitpicks.per_file is None and c.nitpicks.per_run is None
    assert c.learnings.cap == 20 and c.verifier.cap is None
    assert c.mode == "auto" and c.profile == "assertive" and c.budget_usd is None
    assert c.confirm_above_usd == 5.0 and "**/migrations/**" in c.auto.deep_paths
    assert (c.effort.quick, c.effort.standard, c.effort.deep) == ("low", "medium", "high")
    assert c.call_timeout_s == 3600 and c.max_rounds == 3
    assert c.guidelines.ancestors and not c.intent.issues and c.triage.haiku
    assert "S101" in c.tools.ruff.test_ignore and "**/conftest.py" in c.tools.ruff.test_paths
    assert not hasattr(c, "gate")


def test_model_tiers_votes_and_sweep_defaults():
    m = Config().models
    # Cheap by default (decision 15): Sonnet finders, security and sweep; Opus verifies
    # critical findings only; Haiku for triage, rerank and walkthrough.
    assert (m.finder, m.finder_deep, m.finder_rules) == ("sonnet", "sonnet", "sonnet")
    assert (m.verifier_major, m.verifier, m.security) == ("opus", "sonnet", "sonnet")
    assert (m.adjudicator, m.adjudicator_fallback, m.sweep) == ("opus", "opus", "sonnet")
    assert (m.triage, m.rerank, m.walkthrough) == ("haiku", "haiku", "haiku")
    c = Config()
    assert c.verifier.votes == 3 and c.sweep is True
    c = config_from_dict({"verifier": {"votes": 1}, "sweep": False,
                          "models": {"adjudicator": "fable"}})
    assert c.verifier.votes == 1 and c.sweep is False and c.models.adjudicator == "fable"


def test_stage_effort_never_for_haiku():
    from preflight_review.config import stage_effort

    c = merge_overrides(Config(), mode="deep")
    assert stage_effort(c) == "high" and stage_effort(c, "sonnet") == "high"
    assert stage_effort(c, "haiku") is None and stage_effort(c, "claude-haiku-4-5") is None


def test_default_yaml_round_trips():
    assert config_from_dict(yaml.safe_load(DEFAULT_CONFIG_YAML)) == Config()


def test_null_caps_and_effort_load():
    c = config_from_dict({
        "budget_usd": None, "verifier": {"cap": None},
        "nitpicks": {"per_file": None, "per_run": 4},
        "effort": {"deep": "max", "quick": None},
    })
    assert c.budget_usd is None and c.verifier.cap is None
    assert c.nitpicks.per_file is None and c.nitpicks.per_run == 4
    assert c.effort.deep == "max" and c.effort.quick is None and c.effort.standard == "medium"
    with pytest.raises(ConfigError, match="effort.bogus"):
        config_from_dict({"effort": {"bogus": "high"}})


def test_unknown_top_level_key():
    with pytest.raises(ConfigError, match="bogus"):
        config_from_dict({"bogus": 1})


def test_unknown_nested_key_names_path():
    with pytest.raises(ConfigError, match=r"tools\.rufff"):
        config_from_dict({"tools": {"rufff": True}})


def test_gate_rejected():
    with pytest.raises(ConfigError, match="gate"):
        config_from_dict({"gate": "major"})


def test_version_2_rejected():
    with pytest.raises(ConfigError):
        config_from_dict({"version": 2})


def test_wrong_types():
    with pytest.raises(ConfigError):
        config_from_dict({"budget_usd": "lots"})
    with pytest.raises(ConfigError):
        config_from_dict({"concurrency": True})
    with pytest.raises(ConfigError):
        config_from_dict({"lenses": "correctness"})


def test_osv_and_path_instructions():
    c = config_from_dict(
        {
            "tools": {"osv": False},
            "path_instructions": [{"path": "a/**", "instructions": "be careful"}],
            "budget_usd": 2,
        }
    )
    assert c.tools.osv is False and c.budget_usd == 2.0
    assert c.path_instructions[0].path == "a/**"
    with pytest.raises(ConfigError, match="path_instructions"):
        config_from_dict({"path_instructions": [{"path": "a", "bogus": 1}]})


def test_base_ref_beats_working_tree(tmp_path):
    repo = make_repo(tmp_path, {".preflight/config.yaml": "budget_usd: 2.0\n"})
    branch(repo, "feature")
    write(repo, ".preflight/config.yaml", "budget_usd: 9.0\n")
    assert load_config(repo, "main").budget_usd == 2.0
    assert load_config(repo, None).budget_usd == 9.0


def test_missing_file_defaults(tmp_path):
    repo = make_repo(tmp_path, {"a.txt": "x"})
    assert load_config(repo, None) == Config()
    assert load_config(repo, "main") == Config()


def test_bad_yaml(tmp_path):
    repo = make_repo(tmp_path, {".preflight/config.yaml": "a: [unclosed\n"})
    with pytest.raises(ConfigError):
        load_config(repo, None)


def test_merge_overrides():
    c = merge_overrides(Config(), mode="deep", base=None, budget_usd=3.0)
    assert c.mode == "deep" and c.base == "origin/main" and c.budget_usd == 3.0
    with pytest.raises(ConfigError):
        merge_overrides(Config(), nope=1)


def test_base_fallback_origin_to_local(tmp_path):
    repo = make_repo(tmp_path, {".preflight/config.yaml": "budget_usd: 2.0\n"})
    assert load_config(repo, "origin/main").budget_usd == 2.0


def test_unresolvable_base_raises_config_error(tmp_path):
    repo = make_repo(tmp_path, {"a.txt": "x"})
    with pytest.raises(ConfigError, match="nope.*origin/nope"):
        load_config(repo, "nope")


def test_docs_review_values():
    assert Config().docs_review == "auto"
    assert config_from_dict({"version": 1, "docs_review": "off"}).docs_review == "off"
    with pytest.raises(ConfigError, match="docs_review"):
        config_from_dict({"version": 1, "docs_review": "sometimes"})


def test_legacy_defaults_flags_keys_pinned_at_1_0_values():
    from preflight_review.config import (
        config_from_dict,
        legacy_defaults,
        legacy_defaults_note,
    )

    assert legacy_defaults(Config()) == [] and legacy_defaults_note(Config()) is None
    cfg = config_from_dict({
        "version": 1, "profile": "chill", "mode": "standard", "budget_usd": 1.5,
        "nitpicks": {"per_file": 2, "per_run": 5}, "verifier": {"cap": 15},
        "models": {"finder": "sonnet"},  # the 1.1 default again: not flagged
    })
    found = legacy_defaults(cfg)
    assert len(found) == 6
    assert "profile: chill (1.1 default: assertive)" in found
    assert "budget_usd: 1.5 (1.1 default: None)" in found
    assert "verifier.cap: 15 (1.1 default: None)" in found
    assert legacy_defaults_note(cfg).startswith("config: keys still at their 1.0 defaults")
    assert legacy_defaults(config_from_dict({"version": 1, "mode": "quick"})) == []
