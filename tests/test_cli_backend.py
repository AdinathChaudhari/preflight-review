import json
import subprocess
from pathlib import Path

import pytest

from preflight_review.model.backend import ModelRequest
from preflight_review.model.cli_backend import CliBackend, claude_version


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}


def make_req(tmp_path, **kw):
    base = dict(
        role="triage", model="sonnet", system_prompt="SYS", prompt="PROMPT",
        schema=SCHEMA, cwd=tmp_path,
    )
    base.update(kw)
    return ModelRequest(**base)


def success_json(**over):
    d = {
        "type": "result", "subtype": "success", "is_error": False,
        "structured_output": {"a": "x"}, "total_cost_usd": 0.0064,
        "duration_ms": 1234, "num_turns": 2,
        "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_input_tokens": 16000, "cache_creation_input_tokens": 31000,
        },
        "modelUsage": {"claude-sonnet-4-5": {"costUSD": 0.0064}},
    }
    d.update(over)
    return d


def exec_error_json(cost=0.25):
    d = max_turns_json(cost)
    d.update(subtype="error_during_execution", errors=["API Error: overloaded"])
    return d


def max_turns_json(cost=0.25):
    return {
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "result": None, "structured_output": None, "total_cost_usd": cost,
        "errors": ["Reached maximum number of turns (1)"],
        "usage": {"input_tokens": 3, "output_tokens": 7,
                  "cache_read_input_tokens": 11, "cache_creation_input_tokens": 13},
        "num_turns": 4, "duration_ms": 500,
    }


class Runner:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


def cp(obj_or_str, code=0, stderr=""):
    out = obj_or_str if isinstance(obj_or_str, str) else json.dumps(obj_or_str)
    return subprocess.CompletedProcess([], code, stdout=out, stderr=stderr)


def test_build_argv_exact(tmp_path):
    req = make_req(tmp_path, max_turns=4, max_budget_usd=0.5)
    argv = CliBackend().build_argv(req)
    assert argv == [
        "claude", "-p", "--model", "sonnet", "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA, separators=(",", ":")),
        "--max-turns", "4", "--max-budget-usd", "0.50",
        "--setting-sources", "", "--system-prompt", "SYS",
        "--no-session-persistence", "--permission-mode", "dontAsk",
        "--strict-mcp-config", "--disable-slash-commands",
        "--tools", "Read,Grep,Glob",
    ]


def test_no_budget_or_effort_flags_by_default(tmp_path):
    argv = CliBackend().build_argv(make_req(tmp_path))
    assert "--max-budget-usd" not in argv and "--effort" not in argv


def test_effort_flag_follows_max_turns(tmp_path):
    argv = CliBackend().build_argv(make_req(tmp_path, max_turns=3, max_budget_usd=1.0, effort="high"))
    i = argv.index("--max-turns")
    assert argv[i:i + 6] == ["--max-turns", "3", "--max-budget-usd", "1.00", "--effort", "high"]
    argv = CliBackend().build_argv(make_req(tmp_path, max_turns=3, effort="max"))
    i = argv.index("--max-turns")
    assert argv[i:i + 4] == ["--max-turns", "3", "--effort", "max"]


def test_timeout_default_and_override(tmp_path):
    runner = Runner(cp(success_json()))
    CliBackend(runner=runner).run(make_req(tmp_path))
    assert runner.calls[0][1]["timeout"] == 3600
    runner = Runner(cp(success_json()))
    CliBackend(runner=runner, timeout_s=120).run(make_req(tmp_path))
    assert runner.calls[0][1]["timeout"] == 120


def test_tools_last_and_forbidden_flags(tmp_path):
    argv = CliBackend().build_argv(make_req(tmp_path))
    assert argv[-2:] == ["--tools", "Read,Grep,Glob"]
    assert "--bare" not in argv
    assert "--system-prompt-file" not in argv
    assert '{"type":"object"' in argv[argv.index("--json-schema") + 1]


def test_empty_tools(tmp_path):
    argv = CliBackend().build_argv(make_req(tmp_path, tools=[]))
    assert argv[-2:] == ["--tools", ""]


def test_success_parsed(tmp_path):
    runner = Runner(cp(success_json()))
    req = make_req(tmp_path)
    res = CliBackend(runner=runner).run(req)
    assert res.ok and res.structured == {"a": "x"} and res.subtype == "success"
    assert res.cost_usd == pytest.approx(0.0064)
    assert (res.input_tokens, res.output_tokens) == (10, 20)
    assert (res.cache_read_tokens, res.cache_write_tokens) == (16000, 31000)
    assert res.model == "claude-sonnet-4-5"
    assert res.duration_ms == 1234 and res.num_turns == 2
    assert len(runner.calls) == 1
    _, kw = runner.calls[0]
    assert kw["input"] == "PROMPT"
    assert kw["cwd"] == req.cwd
    assert kw["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_error_retries_once_and_sums_cost(tmp_path):
    runner = Runner(cp(exec_error_json(0.25), 1), cp(exec_error_json(0.30), 1))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    assert not res.ok and res.subtype == "error_during_execution"
    assert len(runner.calls) == 2
    assert res.cost_usd == pytest.approx(0.55)
    assert "overloaded" in res.error


def test_max_turns_is_not_retried(tmp_path):
    # Same turn limit on retry hits the same wall and pays twice.
    runner = Runner(cp(max_turns_json(0.25), 1))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    assert not res.ok and res.subtype == "error_max_turns"
    assert len(runner.calls) == 1
    assert res.cost_usd == pytest.approx(0.25)
    assert "maximum number of turns" in res.error


def test_budget_exceeded_is_not_retried(tmp_path):
    budget = {
        "type": "result", "subtype": "error_max_budget_usd", "is_error": True,
        "errors": ["Reached maximum budget ($0.38)"], "total_cost_usd": 0.45,
        "usage": {"input_tokens": 4, "output_tokens": 100,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 200},
        "num_turns": 5, "duration_ms": 900,
    }
    runner = Runner(cp(budget, 1))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    assert not res.ok and res.subtype == "error_max_budget_usd"
    assert len(runner.calls) == 1
    assert res.cost_usd == pytest.approx(0.45)
    assert "Reached maximum budget" in res.error


def test_retry_sums_tokens_and_turns(tmp_path):
    runner = Runner(cp(exec_error_json(0.25), 1), cp(exec_error_json(0.30), 1))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    single = CliBackend(runner=Runner(cp(exec_error_json(0.25), 1))).run(make_req(tmp_path))
    assert res.num_turns == 2 * single.num_turns
    assert res.input_tokens == 2 * single.input_tokens
    assert res.cache_write_tokens == 2 * single.cache_write_tokens


def test_fail_then_success(tmp_path):
    runner = Runner(cp(exec_error_json(0.25), 1), cp(success_json(total_cost_usd=0.1)))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    assert res.ok and len(runner.calls) == 2
    assert res.cost_usd == pytest.approx(0.35)


def test_log_line_before_json(tmp_path):
    out = "some log line\n" + json.dumps(success_json())
    res = CliBackend(runner=Runner(cp(out))).run(make_req(tmp_path))
    assert res.ok


def test_timeout_no_raise(tmp_path):
    exc = subprocess.TimeoutExpired(["claude"], 1)
    runner = Runner(exc, exc)
    res = CliBackend(runner=runner, timeout_s=7).run(make_req(tmp_path))
    assert not res.ok and res.subtype == "error_timeout" and res.error == "timed out after 7s"
    assert len(runner.calls) == 1  # the same time cap would hit again: no retry


def test_file_not_found(tmp_path):
    res = CliBackend(runner=Runner(FileNotFoundError("nope"), FileNotFoundError("nope"))).run(
        make_req(tmp_path)
    )
    assert not res.ok and res.subtype == "error_subprocess" and "nope" in res.error


def test_garbage_stdout(tmp_path):
    res = CliBackend(runner=Runner(cp("garbage", 1, "boom"), cp("garbage", 1, "boom"))).run(
        make_req(tmp_path)
    )
    assert not res.ok and res.subtype == "error_subprocess" and res.error == "boom"


def test_success_without_structured_output(tmp_path):
    bad = success_json(structured_output=None)
    runner = Runner(cp(bad), cp(bad))
    res = CliBackend(runner=runner).run(make_req(tmp_path))
    assert not res.ok and res.subtype == "error_no_structured_output"


def test_claude_version_reexport():
    assert callable(claude_version)
