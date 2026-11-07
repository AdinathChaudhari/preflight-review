"""Prompt registry: loads packaged prompt files and renders {{slot}} templates."""

import hashlib
import re
from importlib import resources

from preflight_review.errors import SchemaError

CORE_PROMPT_NAMES = ("system", "lens_correctness", "lens_contracts_tests", "verifier",
                     "security", "verifier_security", "lens_data_concurrency",
                     "lens_rules_compliance", "triage", "walkthrough",
                     "rerank", "judge", "propose", "adjudicator", "sweep", "docs_review",
                     "lens_deploy_ci")


def _extension_prompt_names() -> tuple[str, ...]:
    # Imported here: extension modules may import from preflight_review at load time.
    from preflight_review import extensions

    return extensions.prompt_names()


# Core prompts plus those an extension module ships (see preflight_review.extensions).
PROMPT_NAMES = CORE_PROMPT_NAMES + tuple(
    n for n in _extension_prompt_names() if n not in CORE_PROMPT_NAMES
)

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
