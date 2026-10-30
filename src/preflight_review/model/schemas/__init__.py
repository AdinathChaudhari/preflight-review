"""JSON output schemas (draft-07) for every model role."""

from __future__ import annotations

import hashlib
import json
from importlib import resources

from preflight_review.errors import SchemaError

SCHEMA_NAMES = (
    "finder",
    "security",
    "verifier",
    "verifier_security",
    "triage",
    "rerank",
    "walkthrough",
    "judge",
    "propose",
)


def _bytes(name: str) -> bytes:
    if name not in SCHEMA_NAMES:
        raise SchemaError(f"unknown schema: {name!r}")
    return resources.files(__package__).joinpath(f"{name}.json").read_bytes()


def load_schema(name: str) -> dict:
    return json.loads(_bytes(name))


def schema_sha(name: str) -> str:
    return hashlib.sha1(_bytes(name)).hexdigest()
