"""Fake-backend fixtures stay schema-valid, so fake runs exercise the normal finder path.

A finder or sweep response without the required `coverage` reads as a shallow lens: every
fake run would retry it and note "shallow lens" instead of taking the normal path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from preflight_review.model.schemas import load_schema

FAKE = Path(__file__).parent / "fixtures" / "fake"
TYPES = {
    "object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "null": type(None),
}


def errors(value, schema: dict, at: str = "$") -> list[str]:
    """The draft-07 subset the model schemas use: type, enum, required, properties,
    additionalProperties: false, items, minItems, minimum/maximum, maxLength."""
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        ok = any(
            isinstance(value, TYPES[n])
            and not (n in ("integer", "number") and isinstance(value, bool))
            for n in names
        )
        if not ok:
            return [f"{at}: expected {types}, got {type(value).__name__}"]
    out: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{at}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        out += [f"{at}: missing {k!r}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            out += [f"{at}: unexpected {k!r}" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                out += errors(v, props[k], f"{at}.{k}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            out.append(f"{at}: fewer than {schema['minItems']} items")
        for i, v in enumerate(value):
            out += errors(v, schema.get("items", {}), f"{at}[{i}]")
    if (
        isinstance(value, int) and not isinstance(value, bool)
        and not schema.get("minimum", value) <= value <= schema.get("maximum", value)
    ):
        out.append(f"{at}: {value} out of range")
    if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
        out.append(f"{at}: longer than {schema['maxLength']}")
    return out


def _responses():
    """(where, role, response) for every fixture entry, per-case entries included."""
    for path in sorted(FAKE.glob("*.json")):
        data = json.loads(path.read_text())
        groups = data["__per_case__"].items() if "__per_case__" in data else [("", data)]
        for case, roles in groups:
            for role, resp in roles.items():
                yield f"{path.name}:{case}".rstrip(":"), role, resp


def _finder_shaped(role: str) -> bool:
    # __default__ answers every finder lens a fixture does not name, and the sweep. The
    # security lane may also fall back to it; that parser ignores the extra `coverage` key.
    return role.startswith("finder:") or role in ("sweep", "__default__")


FINDER_RESPONSES = [r for r in _responses() if _finder_shaped(r[1])]
SECURITY_RESPONSES = [r for r in _responses() if r[1] == "security"]


def test_fixtures_hold_finder_and_security_responses():
    assert len(FINDER_RESPONSES) > 30 and SECURITY_RESPONSES


@pytest.mark.parametrize("where,role,resp", FINDER_RESPONSES,
                         ids=[f"{w}:{r}" for w, r, _ in FINDER_RESPONSES])
def test_fake_finder_and_sweep_responses_match_finder_schema(where, role, resp):
    assert errors(resp, load_schema("finder")) == []


@pytest.mark.parametrize("where,role,resp", SECURITY_RESPONSES,
                         ids=[f"{w}:{r}" for w, r, _ in SECURITY_RESPONSES])
def test_fake_security_responses_match_security_schema(where, role, resp):
    assert errors(resp, load_schema("security")) == []


def test_validator_catches_missing_coverage():
    resp = {"findings": [], "files_read_outside_packet": []}
    assert errors(resp, load_schema("finder")) == ["$: missing 'coverage'"]
