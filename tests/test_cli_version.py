import json

import pytest

from preflight_review import __version__, cli

COMMANDS = [
    "review", "init", "doctor", "install-hook", "uninstall-hook",
    "respond", "learn", "import-coderabbit", "eval", "version",
]


def test_version_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "claude_version", lambda: "9.9.9")
    assert cli.main(["version", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert set(d) == {"version", "claude_code", "prompts_sha"}
    assert d["version"] == __version__ and d["claude_code"] == "9.9.9"


def test_version_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "claude_version", lambda: "9.9.9")
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"preflight-review {__version__} (claude 9.9.9, prompts ")


def test_import_coderabbit_requires_repo(capsys):
    assert cli.main(["import-coderabbit"]) == 1
    assert "--repo must look like OWNER/NAME" in capsys.readouterr().err


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for c in COMMANDS:
        assert c in out
    assert "--gate" not in out


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: preflight-review" in capsys.readouterr().out


def test_preflight_error_maps_to_exit_1(monkeypatch, capsys):
    from preflight_review.errors import GitError

    def boom(args):
        raise GitError("nope")

    monkeypatch.setattr(cli, "_not_implemented", boom)
    monkeypatch.setattr(cli, "prompts_sha", lambda: (_ for _ in ()).throw(GitError("nope")))
    monkeypatch.setattr(cli, "claude_version", lambda: None)
    assert cli.main(["version"]) == 1
    assert "preflight-review: GitError: nope" in capsys.readouterr().err
