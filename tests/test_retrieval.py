"""Learnings + history retrieval: glob -> BM25 -> optional rerank."""

from __future__ import annotations

import json

from preflight_review.config import Config
from preflight_review.context.intent import Intent
from preflight_review.context.learnings import Learning, retrieve_learnings
from preflight_review.context.packer import build_packet
from preflight_review.context.retrieval import (
    Doc,
    bm25_rank,
    query_from_scope,
    retrieve_history,
    split_identifier,
    tokenize,
)
from preflight_review.context.symbols import RepoIndex
from preflight_review.errors import BackendError
from preflight_review.model import FakeBackend
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit, make_repo


def _l(i, text="generic note", glob="src/**"):
    return Learning(i, "2026-10-30", glob, text, None, 0)


def _many(n=25):
    return [_l(f"L{i}", f"note number {i} about widgets") for i in range(n)]


def test_tokenize_and_split_identifier():
    assert tokenize("The Quick fox, a go!") == ["the", "quick", "fox"]
    assert tokenize("claim claim") == ["claim", "claim"]
    assert split_identifier("claimNextJob") == ["claim", "next", "job"]
    assert split_identifier("claim_next_job") == ["claim", "next", "job"]


def test_glob_filter_excludes_non_matching():
    out = retrieve_learnings([_l("L1"), _l("L2", glob="docs/**")], ["src/a.py"], Config())
    assert [l.id for l in out] == ["L1"]


def test_bm25_ranks_relevant_doc_first():
    docs = [
        Doc("a", "unrelated styling convention", "src/**", "learning"),
        Doc("b", "non-atomic job claim allows double processing", "src/**", "learning"),
        Doc("c", "prefer small functions", "src/**", "learning"),
    ]
    ranked = bm25_rank(docs, "claim job", 3)
    assert ranked[0][0].id == "b"
    assert bm25_rank(docs, "", 2) == [(docs[0], 0.0), (docs[1], 0.0)]
    assert bm25_rank([], "claim", 2) == []


def test_cap_without_rerank_call_when_disabled():
    cfg = Config()
    cfg.learnings.rerank = False
    fb = FakeBackend({"rerank": {"ranked_ids": ["L3"]}})
    out = retrieve_learnings(_many(), ["src/a.py"], cfg, fb)
    assert len(out) == 20
    assert fb.calls == []


def test_rerank_order_unknown_ids_and_missing_appended():
    cfg = Config()
    ls = _many()
    ls[7].text = "claim claim claim job"
    fb = FakeBackend({"rerank": {"ranked_ids": ["L5", "nope", "L5", "L2"]}})
    out = retrieve_learnings(ls, ["src/a.py"], cfg, fb, query="claim job")
    ids = [l.id for l in out]
    assert len(ids) == 20
    assert ids[:2] == ["L5", "L2"]
    assert ids[2] == "L7"  # BM25 top follows the model's picks
    assert len(fb.calls) == 1
    req = fb.calls[0]
    assert req.role == "rerank" and req.model == cfg.models.rerank and req.tools == []


def test_backend_failure_falls_back_to_bm25():
    ls = _many()
    ls[9].text = "claim claim job"
    base = retrieve_learnings(ls, ["src/a.py"], Config(), None, query="claim job")
    assert base[0].id == "L9"
    for fb in (FakeBackend({"rerank": BackendError("x")}), FakeBackend()):
        out = retrieve_learnings(ls, ["src/a.py"], Config(), fb, query="claim job")
        assert [l.id for l in out] == [l.id for l in base]
        assert len(fb.calls) == 1

    class Boom:
        name = "boom"

        def run(self, req):
            raise RuntimeError("down")

    out = retrieve_learnings(ls, ["src/a.py"], Config(), Boom(), query="claim job")
    assert [l.id for l in out] == [l.id for l in base]


def test_query_from_scope(tmp_path):
    repo = make_repo(tmp_path, {"job_queue.py": "x = 1\n"})
    branch(repo, "feature")
    commit(repo, {"job_queue.py": "x = 1\ndef claimNextJob(worker_id):\n    return 1\n"}, "c")
    scope = resolve_scope(repo, "main")
    q = query_from_scope(scope, ["fooBar"])
    terms = q.split()
    assert terms[:2] == ["foo", "bar"]
    for t in ("claim", "next", "job", "worker", "queue"):
        assert t in terms
    assert len(terms) == len(set(terms))
    assert q == query_from_scope(scope, ["fooBar"])


def _hist(pr, cid, file, title, body="body text"):
    return {"repo": "r", "pr": pr, "comment_id": cid, "created_at": "x", "file": file,
            "start_line": 3, "end_line": 4, "severity": "major", "category": "bug",
            "title": title, "body_excerpt": body, "is_nitpick": False,
            "outcome": "accepted", "head_sha": "h", "base_sha": "b", "agent_prompt": ""}


def test_retrieve_history(tmp_path):
    assert retrieve_history(tmp_path, ["a.py"], "claim") == []
    d = tmp_path / ".preflight"
    d.mkdir()
    lines = [
        json.dumps(_hist(1, 10, "a.py", "style nit")),
        json.dumps(_hist(2, 11, "a.py", "job claim race")),
        "not json",
        json.dumps(_hist(4, 13, "a.py", "naming convention")),
        json.dumps(_hist(5, 14, "a.py", "docstring wording")),
        json.dumps(_hist(3, 12, "b.py", "claim claim claim")),
    ]
    (d / "history.jsonl").write_text("\n".join(lines) + "\n")
    out = retrieve_history(tmp_path, ["a.py"], "claim job")
    assert out[0].id == "2#11" and len(out) == 4
    assert all(x.kind == "history" and x.path_glob == "a.py" for x in out)


def test_packer_history_section(tmp_path):
    from tests.test_packer import _inputs

    repo = make_repo(tmp_path, {"app.py": "a = 1\n"})
    branch(repo, "feature")
    commit(repo, {"app.py": "a = 2\n"}, "c")
    env = (repo, resolve_scope(repo, "main"), RepoIndex(repo, cache=False))
    kw = _inputs(env)
    plain = build_packet(**kw)
    assert build_packet(**kw, history=None).text == plain.text
    assert build_packet(**kw, history=[]).text == plain.text
    doc = Doc("2#11", "job claim race\nexcerpt", "app.py", "history", meta=_hist(2, 11, "app.py", "job claim race"))
    text = build_packet(**kw, history=[doc]).text
    h = text.index("# Prior review comments in these files")
    assert text.index("# Learnings") < h < text.index("# Tool signals (do not restate)")
    assert "- PR 2 · app.py:3 · major · accepted: job claim race" in text
