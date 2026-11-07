from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from preflight_review.config import Config, PathInstruction
from preflight_review.context.learnings import Learning
from preflight_review.findings import Category, Evidence, Lane, Severity
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


def _doc_finding(evidence):
    return mk(file="archive/_INDEX.md", s=12, e=15, cat=Category.maintainability,
              sev=Severity.trivial, lane=Lane.nitpick,
              title="Index describes a guard that the final commit removed",
              evidence=evidence)


def test_docs_finding_with_code_evidence_in_diff_kept():
    # A doc that makes a false claim about code in the same diff is a
    # consistency bug, not a prose nitpick: the finder cites the code line.
    f = _doc_finding([
        Evidence("archive/_INDEX.md", 12, "| a.py | gets a shape guard |"),
        Evidence("a.py", 5, "    speciality: str"),
    ])
    kept, _, dropped = run([f])
    assert kept == [f] and dropped == []


def test_docs_finding_citing_unchanged_line_of_changed_code_file_kept():
    # Real miss: the guard was added then removed, so the cited line itself is
    # unchanged context, but its file is in the diff.
    f = _doc_finding([Evidence("a.py", 500, "    speciality: str")])
    kept, _, dropped = run([f])
    assert kept == [f] and dropped == []


def test_docs_finding_with_prose_only_evidence_dropped():
    f = _doc_finding([
        Evidence("archive/_INDEX.md", 12, "| a.py | gets a shape guard |"),
        Evidence("README.md", 3, "see the index"),
    ])
    kept, _, dropped = run([f])
    assert kept == [] and dropped == [(f, "docs file")]


def test_docs_finding_citing_code_outside_diff_dropped():
    f = _doc_finding([Evidence("other.py", 5, "x = 1")])
    kept, _, dropped = run([f])
    assert kept == [] and dropped == [(f, "docs file")]


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


# ------------------------------------------------- doc scope (prompt and agent files)


def _fc(path, added=10, deleted=0):
    hunk = Hunk(0, 0, 1, added, added_ranges=[(1, max(1, added))])
    return FileChange(path, None, "M", False, [hunk], added, deleted, None, None)


def scope_of(*fcs, root=Path(".")):
    return Scope(root, "r", "b", "main", "x", "y", list(fcs), [], [], "h", False)


def test_prompt_md_finding_kept():
    f = mk(file="prompts/agent/system.md", s=3, e=3,
           evidence=[Evidence("prompts/agent/system.md", 3, "Always answer in JSON.")])
    kept, _, dropped = prefilter([f], scope_of(_fc("prompts/agent/system.md")), Config(), [])
    assert kept == [f] and dropped == []


def test_agent_instruction_files_kept():
    for path in ("SKILL.md", "pkg/CLAUDE.md", "AGENTS.md", ".claude/commands/ship.md"):
        f = mk(file=path, s=2, e=2, evidence=[Evidence(path, 2, "run the tests first")])
        kept, _, _dropped = prefilter([f], scope_of(_fc(path)), Config(), [])
        assert kept == [f], path


def test_doc_vs_doc_contradiction_kept():
    f = mk(file="PLAN.md", s=4, e=4, title="PLAN and PROGRESS disagree on the backup count",
           evidence=[Evidence("PLAN.md", 4, "8 backups"), Evidence("PROGRESS.md", 9, "7 backups")])
    sc = scope_of(_fc("PLAN.md"), _fc("PROGRESS.md"))
    kept, _, dropped = prefilter([f], sc, Config(), [])
    assert kept == [f] and dropped == []


def test_doc_citing_unchanged_doc_dropped(tmp_path):
    f = mk(file="PLAN.md", s=4, e=4,
           evidence=[Evidence("PLAN.md", 4, "8 backups"), Evidence("OLD.md", 9, "7 backups")])
    # repo_root is tmp_path (no OLD.md there), so the result does not depend on the CWD.
    kept, _, dropped = prefilter([f], scope_of(_fc("PLAN.md"), root=tmp_path), Config(), [])
    assert kept == [] and dropped == [(f, "docs file")]


def test_internal_contradiction_kept():
    f = mk(file="docs/runbook.md", s=4, e=20, cat=Category.contracts, sev=Severity.minor,
           evidence=[Evidence("docs/runbook.md", 4, "three steps"),
                     Evidence("docs/runbook.md", 20, "4. restart")])
    kept, _, _ = prefilter([f], scope_of(_fc("docs/runbook.md")), Config(), [])
    assert kept == [f]


def test_single_quote_or_style_in_same_doc_still_dropped():
    one = mk(file="docs/runbook.md", s=4, e=4,
             evidence=[Evidence("docs/runbook.md", 4, "three steps")])
    style = mk(file="docs/runbook.md", s=4, e=20, cat=Category.maintainability,
               evidence=[Evidence("docs/runbook.md", 4, "a"), Evidence("docs/runbook.md", 20, "b")])
    kept, _, dropped = prefilter([one, style], scope_of(_fc("docs/runbook.md")), Config(), [])
    assert kept == [] and [r for _, r in dropped] == ["docs file", "docs file"]


def test_fixture_json_finding_still_kept():
    f = mk(file="tests/fixtures/case.json", s=2, e=2)
    kept, _, _ = prefilter([f], scope_of(_fc("tests/fixtures/case.json")), Config(), [])
    assert kept == [f]


def _two_doc_evidence(sev, cat, lane=Lane.inline):
    return mk(file="PLAN.md", s=4, e=4, sev=sev, cat=cat, lane=lane,
              evidence=[Evidence("PLAN.md", 4, "8"), Evidence("PROGRESS.md", 9, "7")])


def test_docs_heavy_auto_keeps_only_signal():
    sc = scope_of(_fc("PLAN.md", 30), _fc("PROGRESS.md", 30), _fc("a.py", 10))
    keep_minor = _two_doc_evidence(Severity.minor, Category.correctness)
    keep_major = _two_doc_evidence(Severity.major, Category.maintainability)
    drop_trivial = _two_doc_evidence(Severity.trivial, Category.correctness)
    drop_style = _two_doc_evidence(Severity.minor, Category.maintainability)
    drop_nit = _two_doc_evidence(Severity.minor, Category.correctness, lane=Lane.nitpick)
    code = mk(file="a.py", sev=Severity.trivial, cat=Category.maintainability)
    fs = [keep_minor, keep_major, drop_trivial, drop_style, drop_nit, code]
    kept, _, dropped = prefilter(fs, sc, Config(), [])
    assert kept == [keep_minor, keep_major, code]
    assert {r for _, r in dropped} == {"docs file: low-signal finding in a docs-heavy diff"}
    # docs_review: on keeps every reviewable doc finding.
    kept_on, _, _ = prefilter(fs, sc, Config(docs_review="on"), [])
    assert len(kept_on) == 6


def test_docs_light_diff_keeps_low_signal_doc_finding():
    sc = scope_of(_fc("PLAN.md", 5), _fc("PROGRESS.md", 5), _fc("a.py", 90))
    f = _two_doc_evidence(Severity.trivial, Category.maintainability)
    kept, _, _ = prefilter([f], sc, Config(), [])
    assert kept == [f]


def test_secret_file_finding_never_merged():
    tool = mk(file=".env.local", s=1, e=1, cat=Category.secrets, sev=Severity.critical,
              source="secret-files")
    llm = mk(file=".env.local", s=1, e=2, cat=Category.security, sev=Severity.major,
             source="llm:security")
    out = cluster([tool, llm])
    assert out == [tool, llm] and tool.cluster_members == []


def test_gated_lens_finding_on_its_runbook_kept():
    # A deploy runbook is a prose doc, but reviewing it is deploy_ci's job: no docs drop.
    f = mk(file="deploy/k8s/README.md", source="llm:deploy_ci")
    f.evidence = [Evidence("deploy/k8s/README.md", 10, "kubectl apply -f job.yaml")]
    kept, _, dropped = run([f])
    assert kept == [f] and dropped == []


def test_gated_lens_finding_outside_its_paths_still_dropped():
    kept, _, dropped = run([mk(file="docs/plan.md", source="llm:deploy_ci")])
    assert kept == [] and dropped[0][1] == "docs file"


def test_ungated_lens_finding_on_runbook_still_dropped():
    kept, _, dropped = run([mk(file="deploy/k8s/README.md", source="llm:correctness")])
    assert kept == [] and dropped[0][1] == "docs file"


def test_changed_doc_line_citing_unchanged_repo_file_kept(tmp_path):
    # New README text contradicts an unchanged config file: doc-code drift introduced here.
    (tmp_path / "settings.toml").write_text("port = 8080\n")
    f = mk(file="README.md", s=3, e=3, title="README gives the wrong port",
           evidence=[Evidence("README.md", 3, "listens on 9000"),
                     Evidence("settings.toml", 1, "port = 8080")])
    sc = dataclasses.replace(scope_of(_fc("README.md")), repo_root=tmp_path)
    kept, _, dropped = prefilter([f], sc, Config(), [])
    assert kept == [f] and dropped == []
    # docs_review: off keeps the changed-files-only rule.
    kept, _, dropped = prefilter([f], sc, Config(docs_review="off"), [])
    assert kept == [] and dropped == [(f, "docs file")]


def test_unchanged_doc_line_or_missing_cited_file_still_dropped(tmp_path):
    (tmp_path / "settings.toml").write_text("port = 8080\n")
    sc = dataclasses.replace(scope_of(_fc("README.md", added=5)), repo_root=tmp_path)
    old_line = mk(file="README.md", s=40, e=40,
                  evidence=[Evidence("settings.toml", 1, "port = 8080")])
    ghost = mk(file="README.md", s=3, e=3, evidence=[Evidence("nowhere.toml", 1, "port")])
    kept, _, dropped = prefilter([old_line, ghost], sc, Config(), [])
    assert kept == [] and [r for _, r in dropped] == ["docs file", "docs file"]


@pytest.mark.parametrize("source,path", [
    ("secret-files", ".env.txt"), ("gitleaks", "README.md"), ("osv", "requirements.txt"),
    ("secret-files", "config/.env.local.md"),
])
def test_tool_findings_on_doc_files_survive_prefilter(source, path):
    f = mk(file=path, s=1, e=1, cat=Category.secrets, sev=Severity.critical, source=source,
           evidence=[Evidence(path, 1, "REDACTED")])
    kept, _, dropped = prefilter([f], scope_of(_fc(path)), Config(docs_review="off"), [])
    assert kept == [f] and dropped == []


def test_requirements_files_are_not_prose_docs():
    from preflight_review.context.risk import is_prose_doc

    assert not is_prose_doc("requirements.txt") and not is_prose_doc("svc/requirements-dev.txt")
    assert not is_prose_doc("constraints.txt")
    assert is_prose_doc("docs/notes.txt") and is_prose_doc("README.md")
