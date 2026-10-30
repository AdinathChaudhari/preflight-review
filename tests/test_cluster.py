from __future__ import annotations

from pathlib import Path

from preflight_review.config import Config, PathInstruction
from preflight_review.context.learnings import Learning
from preflight_review.findings import Category, Lane, Severity
from preflight_review.findings import Finding, stable_id
from preflight_review.scope import FileChange, Hunk, Scope
from preflight_review.stages.cluster import cluster, prefilter


def mk(file="a.py", s=10, e=10, cat=Category.correctness, sev=Severity.major,
       source="llm:correctness", title="t", claim="c", **kw):
    return Finding(file=file, start_line=s, end_line=e, title=title, claim=claim,
                   severity=sev, category=cat, source=source, **kw)


def mkscope():
    hunk = Hunk(0, 0, 1, 20, added_ranges=[(1, 20)])
    fc = FileChange("a.py", None, "M", False, [hunk], 20, 0, None, None)
    return Scope(Path("."), "r", "b", "main", "x", "y", [fc], [], [], "h", False)


def run(findings, config=None, learnings=None):
    return prefilter(findings, mkscope(), config or Config(), learnings or [])


def test_docs_file_dropped():
    kept, sup, dropped = run([mk(file="README.md")])
    assert kept == [] and dropped[0][1] == "docs file"


def test_docs_file_kept_with_path_instructions():
    cfg = Config(path_instructions=[PathInstruction("docs/**", "check docs")])
    kept, _, dropped = run([mk(file="docs/guide.md")], cfg)
    assert len(kept) == 1 and not dropped


def test_outside_diff_without_caused_by_demoted():
    f = mk(lane=Lane.outside_diff)
    kept, _, _ = run([f])
    assert kept[0].lane == Lane.pre_existing


def test_caused_by_not_in_diff_demoted():
    f = mk(lane=Lane.outside_diff, caused_by="a.py:500")
    assert run([f])[0][0].lane == Lane.pre_existing
    g = mk(lane=Lane.outside_diff, caused_by="garbage")
    assert run([g])[0][0].lane == Lane.pre_existing


def test_caused_by_in_diff_kept():
    f = mk(lane=Lane.outside_diff, caused_by="a.py:5")
    assert run([f])[0][0].lane == Lane.outside_diff


def test_learnings_suppression_hit_and_miss():
    l = Learning("3", "2026-01-01", "*.py", "Do not flag missing retry logic in handlers", None, 0)
    hit = mk(title="Missing retry", claim="handlers lack retry logic")
    miss = mk(title="Other", claim="retry is slow")  # shares only 'retry'
    kept, sup, _ = run([hit, miss], learnings=[l])
    assert kept == [miss]
    assert sup == [(hit, "3")] and hit.suppressed_by == "L3"
    l2 = Learning("L7", "d", "*.py", "do not report retry handlers", None, 0)
    h2 = mk(title="retry", claim="handlers")
    run([h2], learnings=[l2])
    assert h2.suppressed_by == "L7"


def test_prefilter_keeps_input_order():
    fs = [mk(s=5, e=5), mk(file="x.md"), mk(s=1, e=1)]
    orig = list(fs)
    run(fs)
    assert fs == orig


def test_tool_finding_clusters_with_llm():
    llm = mk(s=9, e=12, sev=Severity.minor, title="Mutable default", source="llm:correctness")
    tool = mk(s=10, e=10, sev=Severity.major, source="ruff", rule_id="B006", title="mutable arg")
    out = cluster([tool, llm])
    assert len(out) == 1
    p = out[0]
    assert p is llm and p.title == "Mutable default"
    assert p.severity == Severity.major
    assert p.evidence[0].quote == "ruff B006: mutable arg"
    assert p.cluster_members == [stable_id(tool), stable_id(llm)]


def test_different_coarse_groups_not_merged():
    a = mk(cat=Category.correctness, title="a")
    b = mk(cat=Category.security, title="b")
    assert len(cluster([a, b])) == 2


def test_transitive_chain_merge():
    a = mk(s=1, e=2, title="a")
    b = mk(s=5, e=6, title="b")
    c = mk(s=9, e=10, title="c")
    out = cluster([a, b, c])
    assert len(out) == 1 and (out[0].start_line, out[0].end_line) == (1, 10)


def test_far_apart_not_merged_and_order_stable():
    a = mk(s=1, e=1, title="a")
    b = mk(s=50, e=50, title="b")
    assert cluster([a, b]) == [a, b]
