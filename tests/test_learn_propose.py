"""learn --propose / --apply with the fake backend only."""

from __future__ import annotations

import json

import pytest
import yaml

from preflight_review import cli
from preflight_review.config import PathInstruction, load_config
from preflight_review.errors import PreflightError
from preflight_review.learn import (
    apply_proposals,
    format_proposals_yaml,
    learn,
    propose_path_instructions,
)
from preflight_review.model import FakeBackend

from .conftest import make_repo


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = make_repo(tmp_path, {"a.py": "x = 1\n"})
    monkeypatch.chdir(r)
    return r


def seed(repo):
    learn(repo, "Migrations must be reversible.", "migrations/**")
    learn(repo, "Always add a down migration.", "migrations/**")
    learn(repo, "Use parameterised SQL.", "app/db/**")


GOOD = {
    "proposals": [
        {"path": "migrations/**", "instructions": "Require a reversible down migration.",
         "from_learnings": ["L1", "L2"]},
        {"path": "x/**", "instructions": "Bogus.", "from_learnings": ["L99"]},
        {"path": "y/**", "instructions": "  ", "from_learnings": ["L3"]},
    ]
}


def test_proposals_parsed_unknown_ids_dropped(repo):
    seed(repo)
    fb = FakeBackend({"propose": GOOD})
    out = propose_path_instructions(repo, fb)
    assert out == [{"path": "migrations/**",
                    "instructions": "Require a reversible down migration.",
                    "from_learnings": ["L1", "L2"]}]
    req = fb.calls[0]
    assert req.tools == [] and req.model == "sonnet"
    assert "migrations/**" in req.prompt and "Use parameterised SQL." in req.prompt


def test_yaml_snippet_round_trips(repo):
    props = [{"path": "a: b/**", "instructions": "Do: this. Then 'that'.", "from_learnings": ["L1"]}]
    text = format_proposals_yaml(props)
    assert text.startswith("path_instructions:")
    assert "# from learnings: L1" in text
    data = yaml.safe_load(text)
    assert data["path_instructions"] == [
        {"path": "a: b/**", "instructions": "Do: this. Then 'that'."}
    ]


def test_apply_appends_and_loads(repo):
    (repo / ".preflight").mkdir()
    (repo / ".preflight" / "config.yaml").write_text("backend: cli\n")
    props = [{"path": "migrations/**", "instructions": "Be reversible.", "from_learnings": ["L1"]}]
    path = apply_proposals(repo, props)
    apply_proposals(repo, props)  # duplicate skipped
    assert path.name == "config.yaml"
    cfg = load_config(repo, None)
    assert cfg.path_instructions == [PathInstruction("migrations/**", "Be reversible.")]


def test_apply_without_config_errors(repo):
    with pytest.raises(PreflightError, match="init"):
        apply_proposals(repo, [])


def test_empty_learnings_no_backend_call(repo, capsys):
    fb = FakeBackend()
    assert propose_path_instructions(repo, fb) == []
    assert fb.calls == []
    assert cli.main(["learn", "--propose", "--backend", "fake"]) == 0
    assert "no learnings to propose from" in capsys.readouterr().out


def test_apply_without_propose_exits_1(repo, capsys):
    assert cli.main(["learn", "--apply"]) == 1


def test_failed_backend_raises(repo):
    seed(repo)
    from preflight_review.errors import BackendError

    fb = FakeBackend({"propose": BackendError("boom")})
    with pytest.raises(PreflightError, match="boom"):
        propose_path_instructions(repo, fb)


def test_cli_propose_apply(repo, tmp_path, monkeypatch, capsys):
    seed(repo)
    (repo / ".preflight" / "config.yaml").write_text("backend: cli\n")
    resp = tmp_path / "resp.json"
    resp.write_text(json.dumps({"propose": GOOD}))
    monkeypatch.setenv("PREFLIGHT_FAKE_RESPONSES", str(resp))
    assert cli.main(["learn", "--propose", "--backend", "fake"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("path_instructions:") and "paste this under path_instructions:" in out
    assert "path_instructions" not in (repo / ".preflight" / "config.yaml").read_text()
    assert cli.main(["learn", "--propose", "--apply", "--backend", "fake"]) == 0
    assert "appended 1 path_instructions to" in capsys.readouterr().out
    assert load_config(repo, None).path_instructions[0].path == "migrations/**"
