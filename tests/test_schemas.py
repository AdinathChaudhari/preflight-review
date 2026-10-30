import pytest

from preflight_review.errors import SchemaError
from preflight_review.model.schemas import SCHEMA_NAMES, load_schema, schema_sha


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_loads_and_structure(name):
    s = load_schema(name)
    assert s["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert s["type"] == "object"
    for node in walk(s):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) <= set(node["properties"])
        if "enum" in node:
            assert node["enum"]


def test_finder_details():
    item = load_schema("finder")["properties"]["findings"]["items"]
    assert item["properties"]["title"]["maxLength"] == 80
    assert {"caused_by", "rule_quote", "bug_class_instances"}.isdisjoint(item["required"])
    assert item["properties"]["evidence"]["minItems"] == 1


def test_security_requires_extra():
    item = load_schema("security")["properties"]["findings"]["items"]
    assert {"trace", "exploit_scenario", "confidence"} <= set(item["required"])


def test_verifier_security_extra():
    s = load_schema("verifier_security")
    assert "confidence_10" in s["required"]
    assert "confidence_10" not in load_schema("verifier")["properties"]


def test_nullable_enum_form():
    sa = load_schema("verifier")["properties"]["severity_adjusted"]
    assert None in sa["enum"] and sa["type"] == ["string", "null"]


def test_sha():
    assert schema_sha("finder") == schema_sha("finder")
    assert schema_sha("finder") != schema_sha("security")


def test_unknown_name():
    with pytest.raises(SchemaError):
        load_schema("nope")
    with pytest.raises(SchemaError):
        schema_sha("nope")
