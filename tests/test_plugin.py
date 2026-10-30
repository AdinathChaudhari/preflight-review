"""Plugin manifest, skill and README checks."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"
SKILL = ROOT / "skills" / "preflight" / "SKILL.md"
README = ROOT / "README.md"


def _skill_parts() -> tuple[dict, str]:
    parts = SKILL.read_text().split("---\n", 2)
    return yaml.safe_load(parts[1]), parts[2]


def test_json_files_parse():
    json.loads(PLUGIN.read_text())
    json.loads(MARKET.read_text())


def test_plugin_json_fields():
    d = json.loads(PLUGIN.read_text())
    assert d["name"] == "preflight"
    assert d["version"] and d["description"] and d["skills"]


def test_marketplace_json_fields():
    d = json.loads(MARKET.read_text())
    assert d["name"] and d["owner"]["name"]
    assert d["plugins"][0]["name"] == "preflight"
    assert d["plugins"][0]["source"]


def test_skill_frontmatter():
    fm, _ = _skill_parts()
    assert fm["name"] == "preflight"
    assert fm["disable-model-invocation"] is True


def test_skill_argument_hint_has_no_fake_flag():
    fm, body = _skill_parts()
    assert "--deep" not in fm["argument-hint"]
    assert "--mode" in fm["argument-hint"]
    assert "--deep" not in body


def test_skill_body_commands():
    _, body = _skill_parts()
    assert "preflight-review review" in body
    assert "respond" in body


def test_skill_body_has_no_fix_loop():
    _, body = _skill_parts()
    assert "Edit(" not in body
    assert "fix loop" not in body.lower()
    assert "Edit(" not in SKILL.read_text()


def test_readme_report_filename_contract():
    # README states the contract; docs/reference.md carries the worked examples.
    assert "<repo>-PR<##>-preflight-review-<##>.md" in README.read_text()
    reference = (ROOT / "docs" / "reference.md").read_text()
    assert "## Report filename" in reference
    assert "example-app-PR07-preflight-review-01.md" in reference
