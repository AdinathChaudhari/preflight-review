"""Model backend interface, a scriptable fake for tests, and claude version probe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from preflight_review.errors import BackendError


@dataclass
class ModelRequest:
    role: str  # "finder:correctness" | "security" | "verifier" | "triage" | ...
    model: str
    system_prompt: str
    prompt: str
    schema: dict
    cwd: Path
    tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])
    max_turns: int = 6
    max_budget_usd: float = 0.5


@dataclass
class ModelResult:
    ok: bool
    structured: dict | None
    subtype: str
    error: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    num_turns: int = 0
    model: str = ""
    raw: dict = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def run(self, req: ModelRequest) -> ModelResult: ...


Response = "dict | list | Callable[[ModelRequest], dict] | BackendError"


class FakeBackend:
    """Deterministic backend keyed by request role; records every call."""

    name = "fake"

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        default: dict | None = None,
    ) -> None:
        self.responses = {
            k: (list(v) if isinstance(v, list) else v)
            for k, v in (responses or {}).items()
        }
        self.default = default
        self.calls: list[ModelRequest] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "FakeBackend":
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise BackendError(f"{path}: expected a JSON object")
        default = data.pop("__default__", None)

        def conv(v: Any) -> Any:
            if isinstance(v, dict) and set(v) == {"__error__"}:
                return BackendError(str(v["__error__"]))
            return v

        responses = {
            role: ([conv(i) for i in v] if isinstance(v, list) else conv(v))
            for role, v in data.items()
        }
        return cls(responses, default=conv(default))

    def run(self, req: ModelRequest) -> ModelResult:
        self.calls.append(req)
        resp: Any = None
        found = False
        if req.role in self.responses:
            entry = self.responses[req.role]
            if isinstance(entry, list):
                if entry:
                    resp, found = entry.pop(0), True
            else:
                resp, found = entry, True
        if not found and self.default is not None:
            resp, found = self.default, True
        if not found:
            return ModelResult(
                ok=False,
                structured=None,
                subtype="error_fake_missing",
                error=f"no fake response for role {req.role}",
            )
        if callable(resp):
            resp = resp(req)
        if isinstance(resp, BackendError):
            return ModelResult(
                ok=False, structured=None, subtype="error_fake", error=str(resp)
            )
        return ModelResult(
            ok=True,
            structured=resp,
            subtype="success",
            cost_usd=0.01,
            duration_ms=5,
            num_turns=1,
            model=req.model,
        )


def claude_version(claude_bin: str = "claude", runner=subprocess.run) -> str | None:
    try:
        proc = runner(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").split()
    return parts[0] if parts else None
