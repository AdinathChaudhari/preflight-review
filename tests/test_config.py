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
    assert c.nitpicks.per_file == 2 and c.learnings.cap == 20 and c.verifier.cap == 15
    assert c.guidelines.ancestors and not c.intent.issues and not c.triage.haiku
    assert not hasattr(c, "gate")


def test_default_yaml_round_trips():
    assert config_from_dict(yaml.safe_load(DEFAULT_CONFIG_YAML)) == Config()


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
