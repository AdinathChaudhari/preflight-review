"""`claude -p` subprocess backend."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from typing import Callable

from preflight_review.model.backend import (
    ModelRequest,
    ModelResult,
    claude_version,
)

__all__ = ["CliBackend", "claude_version"]


def _parse_stdout(stdout: str) -> dict | None:
    stdout = stdout or ""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


# A retry with the same budget cap hits the cap again and just spends it twice.
NO_RETRY_SUBTYPES = frozenset({"error_max_budget_usd"})


class CliBackend:
    name = "cli"

    def __init__(
        self,
        claude_bin: str = "claude",
        runner: Callable = subprocess.run,
        retries: int = 1,
        timeout_s: int = 900,
    ) -> None:
        self.claude_bin = claude_bin
        self.runner = runner
        self.retries = retries
        self.timeout_s = timeout_s

    def build_argv(self, req: ModelRequest) -> list[str]:
        return [
            self.claude_bin,
            "-p",
            "--model", req.model,
            "--output-format", "json",
            "--json-schema", json.dumps(req.schema, separators=(",", ":")),
            "--max-turns", str(req.max_turns),
            "--max-budget-usd", f"{req.max_budget_usd:.2f}",
            "--setting-sources", "",
            "--system-prompt", req.system_prompt,
            "--no-session-persistence",
            "--permission-mode", "dontAsk",
            # claude.ai connectors and skills add ~3k tokens of tool/skill listings per call.
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--tools", ",".join(req.tools),
        ]

    def _attempt(self, req: ModelRequest) -> ModelResult:
        argv = self.build_argv(req)
        env = os.environ | {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        try:
            proc = self.runner(
                argv,
                input=req.prompt,
                capture_output=True,
                text=True,
                cwd=req.cwd,
                timeout=self.timeout_s,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ModelResult(
                ok=False, structured=None, subtype="error_subprocess",
                error=str(exc)[:500],
            )
        except Exception as exc:  # run() must never raise
            return ModelResult(
                ok=False, structured=None, subtype="error_subprocess",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        data = _parse_stdout(getattr(proc, "stdout", "") or "")
        if data is None:
            stderr = getattr(proc, "stderr", "") or ""
            return ModelResult(
                ok=False, structured=None, subtype="error_subprocess",
                error=(stderr or f"unparseable output (exit {proc.returncode})")[:500],
            )
        subtype = str(data.get("subtype") or "error_unknown")
        structured = data.get("structured_output")
        ok = subtype == "success" and isinstance(structured, dict)
        error = None
        if not ok:
            if subtype == "success":
                subtype = "error_no_structured_output"
            errs = data.get("errors")
            err = data.get("result") or (
                "; ".join(map(str, errs)) if isinstance(errs, list) and errs else None
            )
            error = str(err or subtype)[:500]
        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        model = next(iter(model_usage), None) or req.model
        return ModelResult(
            ok=ok,
            structured=structured if ok else None,
            subtype=subtype,
            error=error,
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            duration_ms=int(data.get("duration_ms") or 0),
            num_turns=int(data.get("num_turns") or 0),
            model=model,
            raw=data,
        )

    def run(self, req: ModelRequest) -> ModelResult:
        attempts: list[ModelResult] = []
        for _ in range(max(0, self.retries) + 1):
            result = self._attempt(req)
            attempts.append(result)
            if result.ok or result.subtype in NO_RETRY_SUBTYPES:
                break
        last = attempts[-1]
        if len(attempts) == 1:
            return last
        # Report what every attempt spent so cost.calls matches the cost total.
        return replace(
            last,
            cost_usd=sum(a.cost_usd for a in attempts),
            input_tokens=sum(a.input_tokens for a in attempts),
            output_tokens=sum(a.output_tokens for a in attempts),
            cache_read_tokens=sum(a.cache_read_tokens for a in attempts),
            cache_write_tokens=sum(a.cache_write_tokens for a in attempts),
            duration_ms=sum(a.duration_ms for a in attempts),
            num_turns=sum(a.num_turns for a in attempts),
        )
