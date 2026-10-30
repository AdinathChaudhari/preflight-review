"""Import CodeRabbit inline review comments from GitHub into history + golden JSONL.

Offline-testable: all network access goes through an injected ``gh_api`` callable
(or a fixture directory).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from preflight_review.errors import PreflightError, SchemaError

BOT_LOGINS = {"coderabbitai[bot]", "coderabbitai"}

GhApi = Callable[[str], "list | dict"]


class CodeRabbitImportError(PreflightError):
    """Fetching or reading CodeRabbit comment data failed."""


@dataclass
class RawDump:
    pulls: list[dict]
    comments: list[dict]


@dataclass
class HistoryItem:
    # Field set is a contract: retrieval code reads history.jsonl with these keys.
    repo: str
    pr: int
    comment_id: int
    created_at: str
    file: str
    start_line: int | None
    end_line: int | None
    severity: str | None
    category: str | None
    title: str
    body_excerpt: str
    is_nitpick: bool
    outcome: str
    head_sha: str | None
    base_sha: str | None
    agent_prompt: str | None


@dataclass
class ImportResult:
    history_path: Path
    golden_path: Path
    n_items: int
    n_skipped: int


# --- header / body patterns -------------------------------------------------

# CodeRabbit inline comments carry HTML machine markers (<!-- cr-comment:v1:... -->, cr-indicator-types, ...)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Severity emoji/word. Underscored/bold variants
# `_⚠️ Potential issue_ | _🔴 Critical_`; `🔵 Trivial` is the 4th tier in the newest format.
SEVERITY_EMOJI_RE = re.compile(r"(🔴|🟠|🟡|🔵)\s*[*_]*\s*(Critical|Major|Minor|Trivial)", re.I)
# Newest format: `<img ...major-16.svg>` + `**Major** · **Functional Correctness** · **Quick win**`
SEVERITY_SVG_RE = re.compile(r"(critical|major|minor|trivial)-\d+\.svg", re.I)
SEVERITY_BOLD_RE = re.compile(r"\*\*\s*(Critical|Major|Minor|Trivial)\s*\*\*", re.I)

# The first line of an inline comment is a header line:
#   `_⚠️ Potential issue_ | _🔴 Critical_`, `_📐 Maintainability & Code Quality_ | _🟡 Minor_ | _⚡ Quick win_`,
#   `<picture>...</picture> **Major** · **Functional Correctness** · **Quick win**`,
#   legacy `_:warning: Potential issue_` / `_:hammer_and_wrench: Refactor suggestion_` / `_:broom: Nitpick_`.
HEADER_LINE_RE = re.compile(
    r"^(?:<picture>|_|\*\*[^\w*]*\s*[A-Z]|[^\w\s]+\s*(?:Potential issue|Refactor suggestion|Nitpick)).*"
    r"(?:\||·|Potential issue|Refactor suggestion|Nitpick|\.svg)",
    re.I,
)
# Separators between header fields
HEADER_SEP_RE = re.compile(r"\s*[|·]\s*")
# Legacy `:warning:` shortcode badges
SHORTCODE_RE = re.compile(r":[a-z_]+:")
# 🧹 Nitpick marker (new header or legacy `:broom: Nitpick`)
NITPICK_RE = re.compile(r"🧹|:broom:|\bNitpick\b", re.I)
EFFORT_PARTS = re.compile(r"quick win|heavy lift|low value", re.I)

# `🤖 Prompt for AI Agents` block, normally inside <details><summary>
AGENT_DETAILS_RE = re.compile(
    r"<details>\s*<summary>[^<]*🤖\s*Prompt for AI Agents[^<]*</summary>(.*?)</details>",
    re.DOTALL | re.I,
)
AGENT_BARE_RE = re.compile(r"🤖\s*Prompt for AI Agents\s*(.*)\Z", re.DOTALL | re.I)
FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)
# CodeRabbit edits its own comment to append this when a later commit fixes it
ADDRESSED_RE = re.compile(r"✅\s*Addressed in commits?\b")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
DETAIL_TAG_RE = re.compile(r"</?(?:details|summary|picture|img|source)\b[^>]*>", re.I)

SEVERITY_BY_EMOJI = {"🔴": "critical", "🟠": "major", "🟡": "minor", "🔵": "trivial"}


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _strip_md(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)  # emphasis underscores, not snake_case
    return re.sub(r"\s+", " ", text).strip()


def _split_header(body: str) -> tuple[str | None, str]:
    """Return (header_line or None, remainder of body after the header)."""
    clean = HTML_COMMENT_RE.sub("", body).lstrip()
    lines = clean.split("\n")
    for i, line in enumerate(lines[:6]):
        if line.strip() and HEADER_LINE_RE.match(line.strip()):
            return line.strip(), "\n".join(lines[i + 1:])
    return None, clean


def _parse_severity(header: str | None) -> str | None:
    if not header:
        return None
    m = SEVERITY_EMOJI_RE.search(header)
    if m:
        return m.group(2).lower()
    m = SEVERITY_SVG_RE.search(header) or SEVERITY_BOLD_RE.search(header)
    return m.group(1).lower() if m else None


def _parse_category(header: str | None) -> str | None:
    if not header:
        return None
    text = DETAIL_TAG_RE.sub(" ", header)
    parts = [p for p in HEADER_SEP_RE.split(text) if p.strip()]
    for part in parts:
        if SEVERITY_EMOJI_RE.search(part) and not re.search(r"[A-Za-z]{4,}.*[A-Za-z]{4,}", SEVERITY_EMOJI_RE.sub("", part)):
            continue
        if SEVERITY_BOLD_RE.fullmatch(part.strip()) or EFFORT_PARTS.search(part):
            continue
        part = SHORTCODE_RE.sub("", part)
        part = re.sub(r"^[^\w]+", "", _strip_md(part).replace("️", ""))
        part = re.sub(r"^[^\w]+", "", part)
        if part:
            return _snake(part)
    return None


def _parse_agent_prompt(body: str) -> tuple[str | None, str]:
    """Return (agent prompt text or None, body with the block removed)."""
    m = AGENT_DETAILS_RE.search(body)
    if not m:
        m = AGENT_BARE_RE.search(body)
    if not m:
        return None, body
    inner = m.group(1).strip()
    fm = FENCE_RE.match(inner)
    if fm:
        inner = fm.group(1)
    inner = inner.strip()
    rest = body[: m.start()] + body[m.end():]
    return (inner or None), rest


def _parse_title(rest: str) -> str:
    m = BOLD_RE.search(rest)
    if m and _strip_md(m.group(1)):
        title = _strip_md(m.group(1))
    else:
        title = ""
        for line in rest.split("\n"):
            line = DETAIL_TAG_RE.sub("", line).strip()
            if line and not line.startswith("```"):
                title = _strip_md(line)
                break
    return title[:120]


def _pr_number(c: dict) -> int:
    url = c.get("pull_request_url") or ""
    m = re.search(r"/(\d+)/?$", url)
    if not m:
        raise SchemaError(f"comment {c.get('id')}: no PR number in pull_request_url")
    return int(m.group(1))


def parse_comment(c: dict, repo: str, pulls_by_number: dict[int, dict]) -> HistoryItem:
    try:
        body = c.get("body") or ""
        pr = _pr_number(c)
        end_line = c.get("line") or c.get("original_line")
        start_line = c.get("start_line") or c.get("original_start_line") or end_line
        header, rest = _split_header(body)
        prompt, rest = _parse_agent_prompt(rest)
        excerpt = DETAIL_TAG_RE.sub(" ", HTML_COMMENT_RE.sub("", rest))
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:600]
        base = (pulls_by_number.get(pr) or {}).get("base") or {}
        return HistoryItem(
            repo=repo,
            pr=pr,
            comment_id=int(c["id"]),
            created_at=c.get("created_at") or "",
            file=c.get("path") or "",
            start_line=start_line,
            end_line=end_line,
            severity=_parse_severity(header),
            category=_parse_category(header),
            title=_parse_title(rest),
            body_excerpt=excerpt,
            is_nitpick=bool(header and NITPICK_RE.search(header)),
            # Only the Addressed marker is a trustworthy signal;
            # absence is not evidence of rejection, so 'declined' is never inferred here.
            outcome="fixed" if ADDRESSED_RE.search(body) else "unknown",
            head_sha=c.get("original_commit_id") or c.get("commit_id"),
            base_sha=base.get("sha"),
            agent_prompt=prompt,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError(f"cannot parse comment {c.get('id')!r}: {exc}") from exc


# --- fetching ----------------------------------------------------------------


def default_gh_api(path: str) -> "list | dict":
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", path],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeRabbitImportError(f"gh api {path} failed: {exc}") from exc
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or ["unknown error"])[0]
        raise CodeRabbitImportError(f"gh api {path} failed: {first}")
    return _parse_paginated(proc.stdout)


def _parse_paginated(text: str) -> "list | dict":
    """Parse one JSON doc, or several concatenated ones (gh --paginate emits `][`)."""
    dec = json.JSONDecoder()
    pos, out, single = 0, [], None
    text = text.strip()
    while pos < len(text):
        try:
            obj, end = dec.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            raise CodeRabbitImportError(f"invalid JSON from gh: {exc}") from exc
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if isinstance(obj, list):
            out.extend(obj)
        else:
            single = obj
    return out if single is None else single


def _is_top_level_bot(c: dict) -> bool:
    return (c.get("user") or {}).get("login") in BOT_LOGINS and c.get("in_reply_to_id") is None


def fetch(repo: str, gh_api: GhApi) -> RawDump:
    pulls = gh_api(f"repos/{repo}/pulls?state=all&per_page=100")
    comments = gh_api(f"repos/{repo}/pulls/comments?per_page=100")
    if not isinstance(pulls, list) or not isinstance(comments, list):
        raise SchemaError("gh api returned a non-list for pulls or comments")
    return RawDump(pulls=pulls, comments=[c for c in comments if _is_top_level_bot(c)])


def _load_fixture(d: Path) -> RawDump:
    d = d if d.is_dir() else d.parent
    def load(name: str) -> list[dict]:
        p = d / name
        if not p.is_file():
            raise CodeRabbitImportError(f"fixture file missing: {p}")
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise SchemaError(f"{p}: invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise SchemaError(f"{p}: expected a JSON array")
        return data

    return RawDump(
        pulls=load("pulls.json"),
        comments=[c for c in load("pull_comments.json") if _is_top_level_bot(c)],
    )


# --- writing -----------------------------------------------------------------


def _write_jsonl(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def write_history(items: list[HistoryItem], path: Path) -> None:
    _write_jsonl([asdict(i) for i in items], path)


def write_golden(items: list[HistoryItem], path: Path) -> None:
    _write_jsonl(
        [
            {
                "repo": i.repo,
                "pr": i.pr,
                "commit": i.head_sha,
                "file": i.file,
                "line_start": i.start_line,
                "line_end": i.end_line,
                "bug_class": i.category,
                "expected_severity": i.severity,
                "is_real": None,  # hand-label
                "in_scope": None,  # hand-label
                "author_verdict": i.outcome,
                "title": i.title,
            }
            for i in items
        ],
        path,
    )


def import_coderabbit(
    repo: str,
    out_dir: Path,
    gh_api: GhApi = default_gh_api,
    from_fixture: Path | None = None,
) -> ImportResult:
    dump = _load_fixture(Path(from_fixture)) if from_fixture else fetch(repo, gh_api)
    pulls_by_number = {p["number"]: p for p in dump.pulls if isinstance(p, dict) and "number" in p}
    items: list[HistoryItem] = []
    skipped = 0
    for c in dump.comments:
        try:
            items.append(parse_comment(c, repo, pulls_by_number))
        except PreflightError:
            skipped += 1
    items.sort(key=lambda i: (i.pr, i.comment_id))
    out_dir = Path(out_dir)
    history, golden = out_dir / "history.jsonl", out_dir / "golden.jsonl"
    write_history(items, history)
    write_golden(items, golden)
    return ImportResult(history, golden, len(items), skipped)
