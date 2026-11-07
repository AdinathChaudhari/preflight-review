"""Optional repo-specific extensions: extra lenses and lens auto-enable detectors.

Every non-underscore module in this package is loaded and may define:

- ``lens_specs() -> dict[str, LensSpec]``: extra lenses, registered when
  ``stages.finders`` is imported.
- ``detect(repo_root: Path) -> AutoEnable | None``: lenses and ast-grep rule
  directories to switch on for this repo (only when config ``auto_lenses`` is true).
- ``PROMPTS: tuple[str, ...]``: names of the packaged prompt files (``prompts/<name>.md``)
  the module's lenses use; they join ``prompts.PROMPT_NAMES``.

Import ``stages.finders`` and other heavy modules inside those functions, never at
module level: this package is loaded while ``stages.finders`` itself is loading.
A package with no extension modules adds nothing.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger("preflight_review.extensions")


@dataclass(frozen=True)
class AutoEnable:
    """What one detector switches on for a repo."""

    name: str
    lenses: tuple[str, ...] = ()
    ast_rule_dirs: tuple[Path, ...] = ()
    reason: str = ""


@lru_cache(maxsize=1)
def modules() -> tuple[ModuleType, ...]:
    found = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        found.append(importlib.import_module(f"{__name__}.{info.name}"))
    return tuple(found)


def lens_specs() -> dict[str, Any]:
    """Extra LensSpecs from every extension module (first definition of a name wins)."""
    out: dict[str, Any] = {}
    for mod in modules():
        fn = getattr(mod, "lens_specs", None)
        if fn is None:
            continue
        for name, spec in fn().items():
            out.setdefault(name, spec)
    return out


def prompt_names() -> tuple[str, ...]:
    """Prompt names every extension module ships, in module order, without duplicates."""
    out: list[str] = []
    for mod in modules():
        for name in getattr(mod, "PROMPTS", ()):
            if name not in out:
                out.append(name)
    return tuple(out)


def detect(repo_root: Path) -> list[AutoEnable]:
    """Run every extension's detector. A detector that raises is logged and skipped."""
    out: list[AutoEnable] = []
    for mod in modules():
        fn = getattr(mod, "detect", None)
        if fn is None:
            continue
        try:
            hit = fn(Path(repo_root))
        except Exception as exc:  # noqa: BLE001 - a broken detector must not stop the review
            logger.warning("extension %s: detect failed: %s", mod.__name__, exc)
            continue
        if hit is not None:
            out.append(hit)
    return out
