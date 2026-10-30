"""Prompt registry: loads packaged prompt files and renders {{slot}} templates."""

import hashlib
import re
from importlib import resources

from preflight_review.errors import SchemaError

PROMPT_NAMES = ("system", "lens_correctness", "lens_contracts_tests", "verifier",
                "security", "verifier_security", "lens_data_concurrency",
                "lens_rules_compliance", "triage", "walkthrough",
                "rerank", "judge", "propose")

_SLOT_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _read_bytes(name: str) -> bytes:
    """Reader seam: raw bytes of '<name>.md' (tests monkeypatch this)."""
    path = resources.files(__package__).joinpath(f"{name}.md")
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise SchemaError(f"unknown prompt: {name!r}") from exc


def _discover_names() -> list[str]:
    root = resources.files(__package__)
    return sorted(p.name[: -len(".md")] for p in root.iterdir() if p.name.endswith(".md"))


def load_prompt(name: str) -> str:
    if not re.fullmatch(r"\w+", name or ""):
        raise SchemaError(f"unknown prompt: {name!r}")
    return _read_bytes(name).decode("utf-8")


def prompt_sha(name: str) -> str:
    if not re.fullmatch(r"\w+", name or ""):
        raise SchemaError(f"unknown prompt: {name!r}")
    return hashlib.sha1(_read_bytes(name)).hexdigest()


def all_prompt_shas() -> dict[str, str]:
    return {name: prompt_sha(name) for name in _discover_names()}


def render(name: str, **slots) -> str:
    text = load_prompt(name)

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in slots:
            raise SchemaError(f"prompt {name!r} needs slot {key!r}")
        return str(slots[key])

    return _SLOT_RE.sub(_sub, text)
