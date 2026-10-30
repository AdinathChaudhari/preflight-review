import re

import pytest

from preflight_review import prompts
from preflight_review.errors import SchemaError
from preflight_review.prompts import (
    PROMPT_NAMES,
    all_prompt_shas,
    load_prompt,
    prompt_sha,
    render,
)

HEADER_RE = re.compile(
    r"^<!-- preflight-review prompt: (\w+) v[12] \| adapted from: .+ \| retrieved 2026-10-30 -->$"
)
DUMMY = {"finding_json": "{}"}


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_loads(name):
    assert load_prompt(name).strip()


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_header_line(name):
    m = HEADER_RE.match(load_prompt(name).splitlines()[0])
    assert m and m.group(1) == name


def test_system_has_no_slots():
    assert "{{" not in load_prompt("system")


@pytest.mark.parametrize("name", ["lens_correctness", "lens_contracts_tests", "verifier"])
def test_renders_without_leftovers(name):
    out = render(name, **DUMMY)
    assert "{{" not in out


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_no_packet_slot(name):
    assert "{{packet}}" not in load_prompt(name)


def test_missing_slot_raises():
    with pytest.raises(SchemaError, match="finding_json"):
        render("verifier")


def test_slot_value_with_braces_not_rescanned():
    out = render("verifier", finding_json="code: {{x}} here")
    assert "code: {{x}} here" in out


def test_unknown_prompt_raises():
    with pytest.raises(SchemaError):
        load_prompt("nope")
    with pytest.raises(SchemaError):
        prompt_sha("nope")


def test_shas_deterministic_and_complete():
    a, b = all_prompt_shas(), all_prompt_shas()
    assert a == b and set(PROMPT_NAMES) <= set(a)
    assert list(a) == sorted(a)


def test_shas_change_when_bytes_change(monkeypatch):
    before = all_prompt_shas()
    orig = prompts._read_bytes
    monkeypatch.setattr(
        prompts, "_read_bytes", lambda n: orig(n) + (b"x" if n == "system" else b"")
    )
    after = all_prompt_shas()
    assert after["system"] != before["system"]
    assert after["verifier"] == before["verifier"]


def test_adapted_from_in_system_and_verifier():
    for n in ("system", "verifier"):
        assert "adapted from: anthropics/claude-plugins-official plugins/code-review (Apache-2.0)" in load_prompt(n).splitlines()[0]


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_no_private_refs_in_prompts(name):
    text = load_prompt(name).lower()
    for bad in ["/Users/", "research/", "Licence check pending"]:
        assert bad.lower() not in text


def test_no_private_research_refs_in_repo():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    needle = "round" + "2/"
    hits = []
    for base in ("src", "docs", "eval", "README.md"):
        p = root / base
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                if needle in f.read_text(encoding="utf-8"):
                    hits.append(str(f.relative_to(root)))
            except (UnicodeDecodeError, OSError):
                pass
    assert not hits, hits
