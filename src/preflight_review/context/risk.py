"""Pure risk ordering for review batches (no I/O, no git, stdlib only).

Files are ordered by risk tier (source, test, config, docs), then by risk score
(descending), then by parent directory and path as a deterministic tie-break.

Tier rules (path-only, first match wins):
  docs   -- lockfiles, generated files, fixtures/data, docs. A .md/.txt/.jinja2/.j2
            file under a `prompts` directory is "source" instead (prompts are behaviour).
  test   -- tests/test/__tests__/spec directories, test_*.py, *_test.py, *_test.go,
            conftest.py, *.test.* / *.spec.* JS/TS files.
  config -- CI/plugin/devcontainer directories, Dockerfile*, Makefile, pyproject etc.,
            .toml/.ini/.cfg/.yaml/.yml/.json/.env files.
  source -- known code extensions.
  anything else -> config.

Sensitivity rule (`is_sensitive`): lowercase the path, split on `[_\\-./]` into
tokens. A token matches if it equals a keyword (or the keyword plus a trailing
"s") from EXACT_KEYWORDS, or startswith one of PREFIX_KEYWORDS. So `db_utils.py`
and `migrations/0001.py` match, `feedback.py` does not match `db`.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

TIERS = ("source", "test", "config", "docs")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

EXACT_KEYWORDS = frozenset({
    "auth", "login", "session", "token", "secret", "credential", "password", "crypt",
    "perm", "acl", "db", "database", "sql", "migration", "schema", "subprocess", "shell",
    "exec", "command", "cmd", "config", "settings", "setting", "env", "prompt", "security",
    "sandbox",
})
PREFIX_KEYWORDS = (
    "auth", "migration", "credential", "password", "crypt", "session", "token", "secret",
    "prompt", "subprocess", "config", "setting",
)

_LOCK_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
               "Cargo.lock", "go.sum"}
_GENERATED_DIRS = {"dist", "build", "generated", "__generated__"}
_FIXTURE_DIRS = {"fixtures", "testdata", "snapshots"}
_CODE_EXTS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
              ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".scala",
              ".sh", ".bash", ".zsh", ".sql", ".jinja2", ".j2"}
_DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
_TEST_DIRS = {"tests", "test", "__tests__", "spec"}
_CONFIG_DIRS = {".github", ".gitlab", ".circleci", ".claude-plugin", ".devcontainer"}
_CONFIG_NAMES = {"Makefile", "pyproject.toml", "setup.cfg", "tox.ini",
                 ".pre-commit-config.yaml"}
_CONFIG_EXTS = {".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".env"}
_JS_TEST = re.compile(r"\.(test|spec)\.(js|ts|tsx|jsx)$")
_SPLIT = re.compile(r"[_\-./]+")


def risk_tier(path: str) -> str:
    p = PurePosixPath(path)
    name, parts, ext = p.name, set(p.parts[:-1]), p.suffix.lower()
    if (ext in {".md", ".txt", ".jinja2", ".j2"}) and "prompts" in parts:
        return "source"
    if (
        name.endswith(".lock") or name in _LOCK_NAMES
        or name.endswith("_pb2.py") or name.endswith(".min.js") or name.endswith(".snap")
        or parts & _GENERATED_DIRS
        or parts & _FIXTURE_DIRS
        or ("eval" in parts and ext not in _CODE_EXTS)
        or ext in {".jsonl", ".csv"}
        or ext in _DOC_EXTS or parts & {"docs", "doc"}
        or name.startswith(("LICENSE", "CHANGELOG"))
    ):
        return "docs"
    if (
        parts & _TEST_DIRS
        or (name.startswith("test_") and ext == ".py")
        or name.endswith(("_test.py", "_test.go")) or name == "conftest.py"
        or _JS_TEST.search(name)
    ):
        return "test"
    if (
        parts & _CONFIG_DIRS or name.startswith(("Dockerfile", "docker-compose"))
        and (name.startswith("Dockerfile") or ext in {".yml", ".yaml"})
        or name in _CONFIG_NAMES or ext in _CONFIG_EXTS
    ):
        return "config"
    if ext in _CODE_EXTS:
        return "source"
    return "config"


def is_sensitive(path: str) -> bool:
    for tok in _SPLIT.split(path.lower()):
        if not tok:
            continue
        if tok in EXACT_KEYWORDS or (tok.endswith("s") and tok[:-1] in EXACT_KEYWORDS):
            return True
        if tok.startswith(PREFIX_KEYWORDS):
            return True
    return False


def risk_score(path: str, lines_changed: int, fan_in: int = 0) -> int:
    return lines_changed * (3 if is_sensitive(path) else 1) + 20 * fan_in


def risk_sort_key(path: str, lines_changed: int, fan_in: int = 0) -> tuple:
    return (
        TIER_RANK[risk_tier(path)],
        -risk_score(path, lines_changed, fan_in),
        str(PurePosixPath(path).parent),
        path,
    )


def order_by_risk(
    files: list[tuple[str, int]], fan_in: dict[str, int] | None = None
) -> list[str]:
    fi = fan_in or {}
    return [
        p for p, _ in sorted(
            files, key=lambda f: risk_sort_key(f[0], f[1], fi.get(f[0], 0))
        )
    ]
