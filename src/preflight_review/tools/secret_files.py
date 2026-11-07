"""secret-files: committed env files, private keys and credential files in the diff.

Name-based and built in, so it runs whether or not gitleaks is installed, and it
catches files whose content no gitleaks rule matches (an `.env.local` holding an
internal URL and a test key is still a committed env file). Findings are always
inline, critical and skip the verifier, like gitleaks.

File content is never read, except that a `.json` file is parsed to recognise a
cloud service-account key (only its "type" field is inspected), and a `.pem` or `.key`
file is scanned for a PRIVATE KEY marker so public CA and certificate files are not
flagged. Content is the reviewed side of the diff: the working tree when the scope
includes it, otherwise the committed HEAD blob.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import PurePosixPath

from preflight_review.config import Config
from preflight_review.findings import (
    Category,
    Evidence,
    Finding,
    Lane,
    Severity,
    Verdict,
)
from preflight_review.gitutil import show_file
from preflight_review.scope import Scope
from preflight_review.tools.base import ToolStatus

NAME = "secret-files"

# Committed on purpose as templates (`.env.example`, `.env.local.example`): never flagged.
ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults"})
_PRIVATE_KEYS = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_KEY_EXTS = (".p12", ".pfx")
_SERVICE_ACCOUNT_RE = re.compile(r"service[-_]?account.*\.json$")
_MAX_JSON_BYTES = 64_000
# A key can sit after a long CA chain, so scan well past the head, but bounded.
_PEM_MAX_BYTES = 8_000_000
_PEM_PRIVATE_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")

Reader = Callable[[], "bytes | None"]


def _worktree_reader(repo_root, path: str) -> Reader:
    def read() -> bytes | None:
        try:
            with open(repo_root / path, "rb") as fh:
                return fh.read(_PEM_MAX_BYTES)
        except OSError:
            return None

    return read


def _pem_is_private(content: bytes | None) -> bool:
    """True unless the content is known and has no PRIVATE KEY block (a public cert or
    CA bundle). Unknown content is flagged: the name alone is the safer signal."""
    if content is None:
        return True
    return bool(_PEM_PRIVATE_RE.search(content[:_PEM_MAX_BYTES]))


def _json_type(content: bytes | None) -> str | None:
    """The top-level "type" of a small JSON object, "" for other JSON, None if unknown."""
    if content is None or len(content) > _MAX_JSON_BYTES:
        return None
    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return str(data.get("type")) if isinstance(data, dict) else ""


def _is_template(low: str) -> bool:
    """`service-account.example.json`: committed on purpose as a template."""
    return PurePosixPath(low).stem.rsplit(".", 1)[-1] in ENV_TEMPLATE_SUFFIXES


def classify(path: str, repo_root=None, read: Reader | None = None) -> str | None:
    """The kind of secret file `path` names, or None. `read` (or, without it, `repo_root`
    for a working-tree read) enables the content checks: service-account `.json` files,
    and PRIVATE KEY blocks in `.pem`/`.key` files. With neither, the name decides."""
    if read is None and repo_root is not None:
        read = _worktree_reader(repo_root, path)
    name = PurePosixPath(path).name
    low = name.lower()
    if low == ".env" or (
        low.startswith(".env.") and low.rsplit(".", 1)[-1] not in ENV_TEMPLATE_SUFFIXES
    ):
        return "env file"
    if low in _PRIVATE_KEYS:
        return "private SSH key"
    if low.endswith(_KEY_EXTS):
        return "private key or certificate bundle"
    if low.endswith((".pem", ".key")) and (read is None or _pem_is_private(read())):
        return "private key"
    if low == "credentials.json":
        return "credentials file"
    if _SERVICE_ACCOUNT_RE.search(low) and not _is_template(low):
        # The name decides unless the content is readable JSON of another type.
        kind = _json_type(read()) if read is not None else None
        if kind is None or kind == "service_account":
            return "service-account key file"
        return None
    if read is not None and low.endswith(".json") and _json_type(read()) == "service_account":
        return "service-account key file"
    return None


def _candidates(scope: Scope) -> list[tuple[str, Reader]]:
    """Changed, non-deleted paths, plus ones the path filters moved to filtered_out, each
    with a reader for the reviewed content (working tree, or HEAD for a committed range)."""
    root = scope.repo_root
    out: dict[str, Reader] = {}
    for f in scope.files:
        if f.status == "D":
            continue
        if f.new_text is not None:
            out[f.path] = lambda t=f.new_text: t.encode("utf-8", errors="replace")
        elif scope.include_worktree:
            out[f.path] = _worktree_reader(root, f.path)
        else:
            out[f.path] = lambda: None  # binary at HEAD: the name decides
    for p in scope.filtered_out:
        if p in out:
            continue
        if scope.include_worktree:
            if (root / p).is_file():
                out[p] = _worktree_reader(root, p)
            continue
        if classify(p) is None and not p.lower().endswith(".json"):
            continue  # no name or content rule can match: skip the git read
        try:
            text = show_file("HEAD", p, root)
        except UnicodeDecodeError:  # binary blob at HEAD: the name decides
            out[p] = lambda: None
            continue
        if text is not None:  # None: deleted in the range
            out[p] = lambda t=text: t.encode("utf-8", errors="replace")
    return list(out.items())


def find_secret_files(scope: Scope) -> list[Finding]:
    out: list[Finding] = []
    for path, read in _candidates(scope):
        kind = classify(path, scope.repo_root, read)
        if kind is None:
            continue
        slug = kind.split(" ")[0] if kind != "private SSH key" else "ssh-key"
        out.append(
            Finding(
                file=path,
                start_line=1,
                end_line=1,
                title=f"Committed secret file: {PurePosixPath(path).name}"[:80],
                claim=f"{path} is a {kind} committed in this diff.",
                severity=Severity.critical,
                category=Category.secrets,
                lane=Lane.inline,
                source=NAME,
                rule_id=f"secret-file:{slug}",
                failure_scenario=(
                    f"Anyone with read access to the repository or a clone reads {path}; "
                    "it stays in git history after a later delete."
                ),
                why_it_matters=(
                    "A committed env, key or credential file leaks whatever it holds to every "
                    "clone and fork, and test or local values often turn out to be live."
                ),
                suggested_fix=(
                    "Remove the file from the branch history (not only from HEAD), rotate any "
                    "credential it held, add it to .gitignore, and commit a template such as "
                    ".env.example instead."
                ),
                evidence=[Evidence(path, 1, f"REDACTED (file name: {kind})")],
                verdict=Verdict.SKIPPED,
            )
        )
    return out


def run_secret_files(scope: Scope, config: Config) -> tuple[list[Finding], ToolStatus]:
    """Always on: there is no config switch, so a missing gitleaks never hides these."""
    started = time.monotonic()
    found = find_secret_files(scope)
    ms = int((time.monotonic() - started) * 1000)
    return found, ToolStatus(NAME, True, "built-in", True, len(found), duration_ms=ms)
