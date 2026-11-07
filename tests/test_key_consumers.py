from __future__ import annotations

import json

from preflight_review.config import Config
from preflight_review.context.packer import _signals_body
from preflight_review.scope import resolve_scope
from preflight_review.tools.key_consumers import KeySignal, key_consumer_signals

from .conftest import branch, commit, make_repo

SPEC_OLD = {"name": "report", "title": "Weekly", "limit": 10}
SPEC_NEW = {"name": "report", "title": "Weekly", "limit": 10, "window": 7}
BUILDER = (
    "WINDOW = 7\n\n"
    "def build(spec):\n"
    "    return spec['name'], spec.get('title'), spec[\"limit\"], WINDOW\n"
)


def _scope(tmp_path, base: dict[str, str], change: dict[str, str]):
    repo = make_repo(tmp_path, base)
    branch(repo, "feat")
    commit(repo, change, "change")
    return resolve_scope(repo, "main")


def _spec_scope(tmp_path, builder=BUILDER, path="config/spec.json", extra=None):
    base = {path: json.dumps(SPEC_OLD, indent=2) + "\n", "app/builder.py": builder}
    change = {path: json.dumps(SPEC_NEW, indent=2) + "\n", **(extra or {})}
    return _scope(tmp_path, base, change)


def test_new_key_with_no_reader_is_a_candidate(tmp_path):
    scope = _spec_scope(tmp_path)
    (sig,) = key_consumer_signals(scope, Config())
    assert sig.file == "config/spec.json"
    assert sig.line == 5
    assert "new key 'window' has no reader" in sig.text
    assert sig.render().startswith("candidate key_consumers config/spec.json:5 ")


def test_new_key_that_is_read_is_quiet(tmp_path):
    builder = BUILDER.replace("WINDOW\n", "spec.get('window')\n")
    assert key_consumer_signals(_spec_scope(tmp_path, builder=builder), Config()) == []


def test_reader_added_in_the_same_diff_counts(tmp_path):
    builder = BUILDER.replace("WINDOW\n", "spec['window']\n")
    scope = _spec_scope(tmp_path, extra={"app/builder.py": builder})
    assert key_consumer_signals(scope, Config()) == []


def test_needs_two_sibling_keys_read_by_name(tmp_path):
    # Only one sibling key is read by name: the file may be consumed whole.
    builder = "def build(spec):\n    return spec['name']\n"
    assert key_consumer_signals(_spec_scope(tmp_path, builder=builder), Config()) == []


def test_default_excludes_skip_ci_and_fixtures(tmp_path):
    for path in (".github/settings.json", "tests/fixtures/spec.json", "api/openapi.json"):
        sub = tmp_path / path.replace("/", "_")
        sub.mkdir()
        assert key_consumer_signals(_spec_scope(sub, path=path), Config()) == [], path


def test_consumed_whole_list_skips_key_check(tmp_path):
    cfg = Config()
    cfg.tools.key_consumers.consumed_whole = ["config/*.json"]
    assert key_consumer_signals(_spec_scope(tmp_path), cfg) == []


def test_disabled_makes_no_signals(tmp_path):
    cfg = Config()
    cfg.tools.key_consumers.enabled = False
    assert key_consumer_signals(_spec_scope(tmp_path), cfg) == []


def test_new_data_file_has_no_key_check(tmp_path):
    scope = _scope(
        tmp_path, {"app/builder.py": BUILDER},
        {"config/spec.json": json.dumps(SPEC_NEW) + "\n"},
    )
    assert key_consumer_signals(scope, Config()) == []


def test_yaml_files_are_checked(tmp_path):
    base = {"conf/job.yaml": "name: a\ntitle: b\n", "app/run.py": "c['name']; c['title']\n"}
    scope = _scope(tmp_path, base, {"conf/job.yaml": "name: a\ntitle: b\nretries: 3\n"})
    (sig,) = key_consumer_signals(scope, Config())
    assert sig.line == 3 and "'retries'" in sig.text


IDS = {"steps": {"step_1_intro": {}, "step_2_exam": {}}, "_superseded_steps": {"step_9_old": {}}}


def _id_scope(tmp_path, refs: list[str]):
    base = {"data/steps.json": json.dumps(IDS), "data/plan.json": json.dumps({"order": []})}
    return _scope(tmp_path, base, {"data/plan.json": json.dumps({"order": refs})})


def test_id_check_off_without_patterns(tmp_path):
    assert key_consumer_signals(_id_scope(tmp_path, ["step_7_missing"]), Config()) == []


def test_id_check_flags_undefined_and_superseded_ids(tmp_path):
    cfg = Config()
    cfg.tools.key_consumers.id_patterns = [r"step_\d+_\w+"]
    scope = _id_scope(tmp_path, ["step_1_intro", "step_7_missing", "step_9_old"])
    texts = sorted(s.text for s in key_consumer_signals(scope, cfg))
    assert texts == [
        "id 'step_7_missing' is defined in no data file",
        "id 'step_9_old' is superseded",
    ]


def test_candidates_render_in_tool_signals():
    body = _signals_body([], [KeySignal("a.json", 3, "new key 'x' has no reader").render()])
    assert body == "candidate key_consumers a.json:3 new key 'x' has no reader"
    assert _signals_body([]) == ""


def test_candidates_reach_the_finder_packet(tmp_path, monkeypatch):
    import subprocess

    from preflight_review import pipeline
    from preflight_review.model import FakeBackend
    from preflight_review.pipeline import ReviewOptions, run_review

    monkeypatch.setattr(pipeline, "run_tools", lambda scope, config: ([], []))
    scope = _spec_scope(tmp_path)
    be = FakeBackend(default={"findings": [], "files_read_outside_packet": [], "coverage": [
        {"file": "config/spec.json", "symbol_or_hunk": "window", "inputs_considered": "-",
         "result": "no issue"},
    ]})
    run_review(
        ReviewOptions(repo_root=scope.repo_root, base="main"), backend=be,
        gh_runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no gh"),
    )
    systems = [c.system_prompt for c in be.calls if c.role.startswith("finder:")]
    assert systems and all(
        "candidate key_consumers config/spec.json:5 new key 'window'" in s for s in systems
    )


def test_invalid_id_pattern_is_a_config_error():
    import pytest

    from preflight_review.config import config_from_dict
    from preflight_review.errors import ConfigError

    with pytest.raises(ConfigError, match=r"id_patterns\[0\]: invalid regex"):
        config_from_dict({"version": 1, "tools": {"key_consumers": {"id_patterns": ["step_(\\d+"]}}})


def test_signal_line_is_the_key_token_on_an_added_line(tmp_path):
    old = {"identifier": "x", "name": "a", "title": "b"}
    new = {"identifier": "x", "name": "a", "title": "b", "id": 3}
    base = {"c/spec.json": json.dumps(old, indent=2) + "\n",
            "app/r.py": "s['name']; s['title']; s['identifier']\n"}
    scope = _scope(tmp_path, base, {"c/spec.json": json.dumps(new, indent=2) + "\n"})
    (sig,) = key_consumer_signals(scope, Config())
    assert "new key 'id'" in sig.text and sig.line == 5


def test_id_in_an_oversized_data_file_is_not_claimed_undefined(tmp_path, monkeypatch):
    from preflight_review.tools import key_consumers as kc_mod

    cfg = Config()
    cfg.tools.key_consumers.id_patterns = [r"step_\d+_\w+"]
    scope = _id_scope(tmp_path, ["step_7_missing"])
    monkeypatch.setattr(kc_mod, "MAX_DATA_FILE_BYTES", 10)  # steps.json now too big to read
    assert key_consumer_signals(scope, cfg) == []
