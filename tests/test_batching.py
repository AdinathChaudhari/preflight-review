"""Batch planning: split a large change into packets that together cover it."""

from __future__ import annotations

import dataclasses

from preflight_review.config import Config
from preflight_review.context.cochange import Companion
from preflight_review.context.hunks import render_diff
from preflight_review.context.intent import Intent
from preflight_review.context.packer import batch_note, build_packet, plan_batches
from preflight_review.context.symbols import RepoIndex
from preflight_review.context.tokens import estimate_tokens
from preflight_review.scope import resolve_scope

from tests.conftest import branch, commit, make_repo


def _lines(prefix: str, n: int) -> str:
    return "".join(f"{prefix}_{i} = {i} + 1000000  # filler text to add bytes\n" for i in range(n))


def _env(tmp_path, base: dict[str, str], changed: dict[str, str]):
    repo = make_repo(tmp_path, base)
    branch(repo, "feature")
    commit(repo, changed, "change")
    scope = resolve_scope(repo, "main")
    return repo, scope, RepoIndex(repo, cache=False)


def _rendered_numbers(plan, scope, index, path):
    nums: set[int] = set()
    for b in plan.batches:
        text = render_diff(dataclasses.replace(scope, files=b.files), index)
        for ln in text.split("\n"):
            parts = ln.split(" ", 1)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].startswith("+"):
                nums.add(int(parts[0]))
    return nums


def _kw(scope, index):
    return dict(
        scope=scope, index=index, config=Config(), tool_findings=[], guidelines=[], learnings=[],
        intent=Intent("feature", [], None, []), companions=[], callers={}, callees={}, tests=[],
    )


def test_many_files_all_covered_and_grouped(tmp_path):
    base = {"README.md": "x\n"}
    changed = {}
    for d in ("alpha", "beta", "gamma"):
        for i in range(10):
            changed[f"{d}/m{i}.py"] = _lines(f"v{i}", 40)
    _, scope, index = _env(tmp_path, base, changed)
    plan = plan_batches(scope, index, diff_budget=1000)
    assert len(plan.batches) > 1 and not plan.oversized
    seen = [p for b in plan.batches for p in b.paths]
    assert sorted(seen) == sorted(changed) and len(seen) == len(set(seen))
    assert all(b.diff_tokens <= 1000 for b in plan.batches)
    assert [b.index for b in plan.batches] == list(range(1, len(plan.batches) + 1))
    dirs = [p.split("/")[0] for p in seen]
    collapsed = [d for i, d in enumerate(dirs) if i == 0 or dirs[i - 1] != d]
    assert len(collapsed) == len(set(collapsed))


def test_large_multi_hunk_file_is_split(tmp_path):
    base_text = "".join(f"line_{i} = {i}\n" for i in range(1, 1201))
    lines = base_text.split("\n")
    changed_lines = list(lines)
    edited = list(range(50, 1200, 100))
    for n in edited:
        changed_lines[n - 1] = f"line_{n} = {n}  # changed" + "x" * 150
    _, scope, index = _env(tmp_path, {"big.py": base_text}, {"big.py": "\n".join(changed_lines)})
    plan = plan_batches(scope, index, diff_budget=1000)
    assert not plan.oversized
    assert len(plan.batches) >= 2
    splits = [b.split["big.py"] for b in plan.batches if "big.py" in b.split]
    assert splits and all(n >= 2 for _, n in splits)
    assert sorted(k for k, _ in splits) == list(range(1, splits[0][1] + 1))
    assert all(b.diff_tokens <= 1000 for b in plan.batches)
    assert set(edited) <= _rendered_numbers(plan, scope, index, "big.py")


def test_new_single_hunk_file_split_into_windows(tmp_path):
    text = _lines("w", 400)
    assert estimate_tokens(text) > 4000
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, {"gen.py": text})
    plan = plan_batches(scope, index, diff_budget=1000)
    assert not plan.oversized and len(plan.batches) >= 4
    assert all(b.diff_tokens <= 1000 for b in plan.batches)
    assert set(range(1, 401)) <= _rendered_numbers(plan, scope, index, "gen.py")


def test_binary_is_not_reviewable(tmp_path):
    repo = make_repo(tmp_path, {"README.md": "x\n"})
    branch(repo, "feature")
    (repo / "img.bin").write_bytes(b"\x00\x01\x02\xff" * 50)
    commit(repo, {"a.py": "x = 1\n"}, "c")
    scope = resolve_scope(repo, "main")
    plan = plan_batches(scope, RepoIndex(repo, cache=False))
    assert ("img.bin", "binary file") in plan.not_reviewable
    assert all("img.bin" not in b.paths for b in plan.batches)
    assert plan.batches and "a.py" in plan.batches[0].paths


def test_cochange_companions_adjacent(tmp_path):
    changed = {f"{d}/f.py": _lines("c", 5) for d in ("a", "b", "c", "d")}
    changed["z/test_f.py"] = _lines("t", 5)
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, changed)
    comp = [Companion("a/f.py", "z/test_f.py", 4, 5, 0.8)]
    plan = plan_batches(scope, index, diff_budget=100_000, companions=comp)
    paths = plan.batches[0].paths
    assert paths.index("z/test_f.py") == paths.index("a/f.py") + 1


def test_plan_is_deterministic(tmp_path):
    changed = {f"d{i % 3}/m{i}.py": _lines(f"v{i}", 40) for i in range(12)}
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, changed)

    def sig():
        p = plan_batches(scope, index, diff_budget=1000)
        return [(b.index, b.paths, b.split, b.diff_tokens) for b in p.batches], p.oversized

    assert sig() == sig()


def test_batch_note_section_first_and_text(tmp_path):
    changed = {f"d/m{i}.py": _lines(f"v{i}", 40) for i in range(6)}
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, changed)
    plan = plan_batches(scope, index, diff_budget=1000)
    b = plan.batches[0]
    note = batch_note(b, len(plan.batches))
    assert note.startswith(f"Batch 1 of {len(plan.batches)}.") and f"- {b.paths[0]}" in note
    sc = dataclasses.replace(scope, files=b.files)
    p = build_packet(**{**_kw(sc, index), "scope": sc}, batch_note=note)
    heads = [ln for ln in p.text.split("\n") if ln.startswith("# ")]
    assert heads[0] == "# Batch" and heads[1] == "# Intent"
    assert set(p.files) == set(b.paths) and p.omitted == []
    plain = build_packet(**{**_kw(sc, index), "scope": sc})
    assert "# Batch" not in plain.text


def _risk_env(tmp_path):
    changed = {
        "docs/guide.md": "# g\n" + "text line here\n" * 60,
        "eval/data.json": '{"a": 1}\n' * 60,
        ".claude-plugin/plugin.json": '{"name": "x"}\n' * 60,
        "tests/test_m.py": _lines("t", 40),
        "src/app/m.py": _lines("m", 40),
    }
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, changed)
    return changed, scope, index


def test_batches_ordered_by_risk(tmp_path):
    changed, scope, index = _risk_env(tmp_path)
    plan = plan_batches(scope, index, diff_budget=700)
    assert len(plan.batches) > 2
    assert "src/app/m.py" in plan.batches[0].paths
    last = plan.batches[-1].paths
    assert "docs/guide.md" in last or "eval/data.json" in last
    flat = [p for b in plan.batches for p in b.paths]
    assert flat.index("src/app/m.py") < flat.index("tests/test_m.py") < flat.index(".claude-plugin/plugin.json")
    assert flat.index(".claude-plugin/plugin.json") < flat.index("docs/guide.md")
    assert flat.index(".claude-plugin/plugin.json") < flat.index("eval/data.json")


def test_risk_tiers_cover_scope(tmp_path):
    changed, scope, index = _risk_env(tmp_path)
    plan = plan_batches(scope, index, diff_budget=700)
    assert plan.risk_tiers == {
        "docs/guide.md": "docs", "eval/data.json": "docs",
        ".claude-plugin/plugin.json": "config", "tests/test_m.py": "test",
        "src/app/m.py": "source",
    }


def test_fan_in_raises_priority(tmp_path):
    changed = {"src/a/big.py": _lines("b", 60), "src/a/small.py": _lines("s", 5)}
    _, scope, index = _env(tmp_path, {"README.md": "x\n"}, changed)
    base = plan_batches(scope, index, diff_budget=100_000).batches[0].paths
    assert base == ["src/a/big.py", "src/a/small.py"]
    boosted = plan_batches(scope, index, diff_budget=100_000, fan_in={"src/a/small.py": 10}).batches[0].paths
    assert boosted == ["src/a/small.py", "src/a/big.py"]
