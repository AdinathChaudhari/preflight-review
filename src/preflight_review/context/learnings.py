"""Learnings file layer: parse, format, append, read from base, filter by path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from preflight_review import gitutil
from preflight_review.context.retrieval import Doc, bm25_rank, rerank_with_model
from preflight_review.errors import ConfigError
from preflight_review.scope import glob_to_regex

LEARNINGS_PATH = ".preflight/learnings.md"
_SEP = " · "
_HEADING = "# Learnings"
_FROM_RE = re.compile(r"^from: (\S+)(?: \((.+)\))?$")


@dataclass
class Learning:
    id: str
    date: str
    path_glob: str
    text: str
    source_finding: str | None
    uses: int


def format_learning(l: Learning) -> str:
    parts = [f"id: {l.id}", f"date: {l.date}", f"path: {l.path_glob}"]
    if l.source_finding:
        parts.append(f"from: {l.source_finding}")
    parts.append(f"uses: {l.uses}")
    header = "- " + _SEP.join(parts)
    body = "\n".join(("  " + ln) if ln else "  " for ln in l.text.split("\n"))
    return header + "\n" + body


def _parse_header(line: str, lineno: int) -> Learning:
    def bad(why: str) -> ConfigError:
        return ConfigError(f"{LEARNINGS_PATH}:{lineno}: malformed learning header ({why}): {line!r}")

    fields = line[2:].split(_SEP)
    if len(fields) not in (4, 5):
        raise bad("expected id, date, path, optional from, uses")
    keys = ["id", "date", "path"] + (["from"] if len(fields) == 5 else []) + ["uses"]
    vals: dict[str, str] = {}
    for key, raw in zip(keys, fields):
        prefix = key + ": "
        if not raw.startswith(prefix):
            raise bad(f"expected '{key}:'")
        vals[key] = raw[len(prefix):]
    if not vals["id"] or not vals["path"]:
        raise bad("empty id or path")
    if not vals["uses"].isdigit():
        raise bad("uses must be an integer")
    return Learning(
        vals["id"], vals["date"], vals["path"], "", vals.get("from"), int(vals["uses"])
    )


def parse_learnings(text: str) -> list[Learning]:
    out: list[Learning] = []
    cur: Learning | None = None
    body: list[str] = []

    def close() -> None:
        nonlocal cur, body
        if cur is not None:
            cur.text = "\n".join(body)
            out.append(cur)
        cur, body = None, []

    for i, line in enumerate(text.split("\n"), 1):
        if line.startswith("- "):
            close()
            cur = _parse_header(line, i)
        elif line.startswith("  ") and cur is not None:
            body.append(line[2:])
        elif not line.strip():
            continue
        else:
            close()  # heading or stray prose
    close()
    return out


def next_id(learnings: list[Learning]) -> str:
    nums = [int(m.group(1)) for l in learnings if (m := re.fullmatch(r"L(\d+)", l.id))]
    return f"L{max(nums) + 1}" if nums else "L1"


def append_learning(repo_root: Path, learning: Learning) -> None:
    path = Path(repo_root) / LEARNINGS_PATH
    entry = format_learning(learning) + "\n"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + "\n" + entry, encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{_HEADING}\n\n{entry}", encoding="utf-8")


def read_learnings(repo_root: Path, base_ref: str | None) -> list[Learning]:
    if base_ref is not None:
        text = gitutil.show_file(base_ref, LEARNINGS_PATH, Path(repo_root))
    else:
        p = Path(repo_root) / LEARNINGS_PATH
        text = p.read_text(encoding="utf-8") if p.is_file() else None
    return parse_learnings(text) if text else []


def filter_by_path(learnings: list[Learning], changed_paths: list[str]) -> list[Learning]:
    out = []
    for l in learnings:
        rx = glob_to_regex(l.path_glob)
        if any(rx.match(p) for p in changed_paths):
            out.append(l)
    return out


def retrieve_learnings(
    learnings: list[Learning],
    changed_paths: list[str],
    config,
    backend=None,
    query: str = "",
    cwd=".",
) -> list[Learning]:
    """Select the learnings relevant to this change, capped at config.learnings.cap.

    Path glob filter, then BM25 (top 2*cap), then an optional model rerank.
    Never raises on backend failure: falls back to BM25 order.
    """
    cap = config.learnings.cap
    matched = filter_by_path(learnings, changed_paths)
    if len(matched) <= cap:
        return matched
    by_id = {l.id: l for l in matched}
    docs = [Doc(l.id, l.text, l.path_glob, "learning") for l in matched]
    ranked = [d for d, _ in bm25_rank(docs, query, 2 * cap)]
    if config.learnings.rerank and backend is not None:
        ranked = rerank_with_model(ranked, query, backend, config.models.rerank, cap, cwd)
    return [by_id[d.id] for d in ranked[:cap]]
