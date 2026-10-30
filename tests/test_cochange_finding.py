from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from preflight_review.context.cochange import Companion
from preflight_review.findings import Category, Lane, Severity, Verdict
from preflight_review.tools.cochange_finding import companion_findings


def make_scope(paths: list[str]):
    return SimpleNamespace(files=[SimpleNamespace(path=p) for p in paths], repo_root=Path("/tmp/x"))


def comp(companion, changed="app/x.py", support=4, total=5, conf=0.8):
    return Companion(changed, companion, support, total, conf)


def run(c, diff=("app/x.py",)):
    return companion_findings([c], make_scope(list(diff)))


def test_emitted_for_test_companion():
    (f,) = run(comp("tests/test_x.py"))
    assert f.file == "app/x.py" and f.start_line == f.end_line == 1
    assert f.severity == Severity.trivial and f.lane == Lane.nitpick
    assert f.category == Category.maintainability
    assert f.source == "cochange" and f.verdict == Verdict.SKIPPED
    assert f.stable_id
    assert "tests/test_x.py usually changes with app/x.py (4/5 commits)" in f.claim
    assert f.title == f.claim


def test_low_confidence_not_emitted():
    assert run(comp("tests/test_x.py", conf=0.59)) == []


def test_low_support_not_emitted():
    assert run(comp("tests/test_x.py", support=2)) == []


def test_already_in_diff_not_emitted():
    assert run(comp("tests/test_x.py"), diff=("app/x.py", "tests/test_x.py")) == []


def test_non_class_path_not_emitted():
    assert run(comp("app/other.py")) == []


def test_migration_and_test_prefix_emitted():
    assert len(run(comp("db/migrations/0002_x.py"))) == 1
    assert len(run(comp("pkg/test_y.py"))) == 1


def test_md_only_for_app_changes():
    assert len(run(comp("docs/guide.md"))) == 1
    assert run(comp("docs/guide.md", changed="scripts/x.py")) == []


def test_deduplicated_by_companion():
    cs = [comp("tests/test_x.py"), comp("tests/test_x.py", changed="app/y.py")]
    assert len(companion_findings(cs, make_scope(["app/x.py", "app/y.py"]))) == 1
