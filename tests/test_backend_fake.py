import json
import subprocess
from pathlib import Path

from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend, ModelRequest, claude_version


def req(role="verifier"):
    return ModelRequest(
        role=role, model="sonnet", system_prompt="s", prompt="p", schema={}, cwd=Path(".")
    )


def test_dict_response():
    r = FakeBackend({"verifier": {"a": 1}}).run(req())
    assert r.ok and r.structured == {"a": 1} and r.subtype == "success"
    assert r.model == "sonnet" and r.cost_usd == 0.01


def test_list_then_default():
    b = FakeBackend({"verifier": [{"n": 1}, {"n": 2}]}, default={"n": 0})
    assert [b.run(req()).structured["n"] for _ in range(3)] == [1, 2, 0]


def test_callable_sees_request():
    b = FakeBackend({"verifier": lambda r: {"role": r.role, "p": r.prompt}})
    assert b.run(req()).structured == {"role": "verifier", "p": "p"}


def test_backend_error():
    r = FakeBackend({"verifier": BackendError("boom")}).run(req())
    assert not r.ok and r.structured is None and r.subtype == "error_fake"
    assert r.error == "boom"


def test_missing_role():
    r = FakeBackend().run(req("security"))
    assert not r.ok and r.subtype == "error_fake_missing"
    assert r.error == "no fake response for role security"


def test_calls_recorded():
    b = FakeBackend(default={})
    b.run(req("a"))
    b.run(req("b"))
    assert [c.role for c in b.calls] == ["a", "b"]


def test_from_file(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(
        json.dumps(
            {
                "verifier": [{"x": 1}, {"__error__": "bad"}],
                "security": {"__error__": "down"},
                "__default__": {"d": 1},
            }
        )
    )
    b = FakeBackend.from_file(p)
    assert b.run(req()).structured == {"x": 1}
    assert b.run(req()).error == "bad"
    assert b.run(req()).structured == {"d": 1}
    assert b.run(req("security")).error == "down"


def test_claude_version_parses():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="2.1.314 (Claude Code)\n", stderr="")

    assert claude_version(runner=runner) == "2.1.314"


def test_claude_version_missing_binary():
    def runner(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    assert claude_version(runner=runner) is None


def test_claude_version_nonzero():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x")

    assert claude_version(runner=runner) is None
