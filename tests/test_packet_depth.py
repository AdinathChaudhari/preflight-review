"""Per-packet depth: file_mode/batch_mode, per-mode packet sizes, auto estimates, menu."""

from __future__ import annotations

import dataclasses
import math

from preflight_review.config import Config, merge_overrides
from preflight_review.context.cochange import Companion
from preflight_review.context.packer import (
    DEFAULT_BATCH_DIFF_TOKENS,
    PACKET_DIFF_TOKENS,
    context_bounds,
    plan_batches,
)
from preflight_review.context.symbols import RepoIndex
from preflight_review.depth import (
    TRIAGE_FILES_PER_CALL,
    batch_mode,
    depth_menu,
    diff_tokens,
    estimate_run,
    file_mode,
    parse_choice,
)
from preflight_review.scope import resolve_scope
from tests.conftest import branch, commit, make_repo

CRUCIAL = "db/migrations/0001_init.py"
CRUCIAL_TEST = "tests/test_migration.py"


def _lines(prefix: str, n: int) -> str:
    return "".join(f"{prefix}_{i} = {i} + 1000000  # filler text to add bytes\n" for i in range(n))


def _env(tmp_path, changed: dict[str, str]):
    repo = make_repo(tmp_path, {"README.md": "x\n"})
    branch(repo, "feature")
    commit(repo, changed, "change")
    scope = resolve_scope(repo, "main")
    return scope, RepoIndex(repo, cache=False)


def _mixed(n_std=30, lines=30):
    changed = {f"app/m{i:02d}.py": _lines(f"v{i}", lines) for i in range(n_std)}
    changed[CRUCIAL] = _lines("mig", lines)
    changed[CRUCIAL_TEST] = _lines("tm", lines)
    return changed


def _modes(scope, config=None):
    config = config or Config()
    return {fc.path: file_mode(config, fc.path) for fc in scope.files}


def test_file_mode_and_batch_mode():
    auto = Config()
    assert file_mode(auto, CRUCIAL) == "deep"
    assert file_mode(auto, "app/service.py") == "standard"
    assert file_mode(auto, CRUCIAL_TEST) == "standard"
    assert batch_mode(auto, ["app/a.py", CRUCIAL]) == "deep"
    assert batch_mode(auto, ["app/a.py", "app/b.py"]) == "standard"
    for m in ("quick", "standard", "deep"):
        cfg = merge_overrides(Config(), mode=m)
        assert file_mode(cfg, CRUCIAL) == m and file_mode(cfg, "app/a.py") == m
        assert batch_mode(cfg, ["app/a.py"]) == m


def test_one_crucial_file_gives_one_deep_packet(tmp_path):
    scope, index = _env(tmp_path, _mixed())
    modes = _modes(scope)
    companions = [Companion(changed=CRUCIAL, companion=CRUCIAL_TEST, support=3, total=3, confidence=1.0)]
    plan = plan_batches(
        scope, index, 3000, companions, file_modes=modes,
        budget_by_mode={"deep": 3000, "standard": 6000, "quick": 9000},
    )
    deep = [b for b in plan.batches if b.mode == "deep"]
    std = [b for b in plan.batches if b.mode == "standard"]
    assert len(deep) == 1 and len(std) >= 2 and len(deep) + len(std) == len(plan.batches)
    assert deep[0].paths == [CRUCIAL, CRUCIAL_TEST]
    assert plan.batches[0] is deep[0]  # deep first
    for b in plan.batches:  # homogeneous
        assert {modes[p] if p != CRUCIAL_TEST else "deep" for p in b.paths} == {b.mode}
    seen = [p for b in plan.batches for p in b.paths]
    assert sorted(seen) == sorted(_mixed())


def test_budgets_by_mode_set_packet_count(tmp_path):
    changed = {f"app/m{i:02d}.py": _lines(f"v{i}", 40) for i in range(24)}
    scope, index = _env(tmp_path, changed)
    total = diff_tokens(scope)
    budgets = {"deep": 2000, "standard": 4000, "quick": 8000}
    counts = {}
    for m in ("deep", "standard", "quick"):
        plan = plan_batches(
            scope, index, file_modes={p: m for p in changed}, budget_by_mode=budgets
        )
        counts[m] = len(plan.batches)
        assert all(b.mode == m and b.diff_tokens <= budgets[m] for b in plan.batches)
        assert counts[m] >= math.ceil(total / budgets[m])
    assert counts["deep"] > counts["standard"] > counts["quick"]


def test_legacy_plan_is_unchanged(tmp_path):
    changed = {f"app/m{i:02d}.py": _lines(f"v{i}", 40) for i in range(12)}
    scope, index = _env(tmp_path, changed)
    legacy = plan_batches(scope, index, 1000)
    explicit_none = plan_batches(scope, index, 1000, file_modes=None, budget_by_mode=None)
    assert [b.paths for b in legacy.batches] == [b.paths for b in explicit_none.batches]
    assert all(b.mode == "" for b in legacy.batches)
    assert DEFAULT_BATCH_DIFF_TOKENS == 12_000
    assert PACKET_DIFF_TOKENS == {"quick": 32_000, "standard": 24_000, "deep": 16_000}


def test_context_bounds():
    assert context_bounds(12_000) == (0.5, 3.0)
    assert context_bounds(16_000) == (0.5, 3.0)
    assert context_bounds(24_000) == (0.5, 1.5)
    assert context_bounds(32_000) == (0.5, 1.5)


def test_estimate_auto_is_cheaper_and_faster_than_deep(tmp_path):
    scope, _ = _env(tmp_path, _mixed(n_std=40, lines=200))
    cfg = Config()
    budgets = dict(PACKET_DIFF_TOKENS)
    auto = estimate_run(scope, cfg, "auto", 5, budgets)
    deep = estimate_run(scope, cfg, "deep", 5, budgets)
    assert auto.usd < deep.usd and auto.minutes < deep.minutes
    assert auto.batches_by_mode["deep"] == 1 and auto.batches_by_mode["standard"] >= 1
    assert auto.sweep_calls == 1 and auto.batches == sum(auto.batches_by_mode.values())
    assert "auto mode (1 deep," in auto.describe() and "standard packets" in auto.describe()
    # explicit modes ignore per-file depth
    std = estimate_run(scope, cfg, "standard", 5, budgets, file_modes={CRUCIAL: "deep"})
    assert std.batches_by_mode == {"standard": std.batches} and std.sweep_calls == 0
    # explicit file_modes override the path rule under auto
    none_deep = estimate_run(
        scope, cfg, "auto", 5, budgets, file_modes={fc.path: "standard" for fc in scope.files}
    )
    assert none_deep.sweep_calls == 0 and "deep" not in none_deep.batches_by_mode


def test_legacy_estimate_call_and_big_deep_run_is_much_faster(tmp_path):
    scope, _ = _env(tmp_path, {f"app/m{i:03d}.py": _lines(f"v{i}", 150) for i in range(120)})
    cfg = Config()
    est = estimate_run(scope, cfg, "deep", 5, 3000)  # legacy int budget
    assert est.batches >= 70 and est.mode == "deep"
    assert est.batches_by_mode == {"deep": est.batches}
    old_minutes = est.batches * 4.0 + 4.0 + math.ceil(est.verify_calls / 5) * 1.0
    assert old_minutes > 280 and est.minutes < old_minutes / 2
    assert est.minutes < 383 / 2


def test_triage_calls_are_batched(tmp_path):
    scope, _ = _env(tmp_path, {f"app/m{i:02d}.py": _lines("v", 5) for i in range(45)})
    est = estimate_run(scope, Config(), "standard", 5, 24_000)
    assert TRIAGE_FILES_PER_CALL == 20 and est.triage_calls == math.ceil(45 / 20) == 3
    off = merge_overrides(Config(), triage=dataclasses.replace(Config().triage, haiku=False))
    assert estimate_run(scope, off, "standard", 5, 24_000).triage_calls == 0


def test_depth_menu_and_parse_choice(tmp_path):
    scope, _ = _env(tmp_path, _mixed(n_std=3, lines=10))
    cfg = Config()
    ests = {m: estimate_run(scope, cfg, m, 5, 12_000) for m in ("quick", "standard", "deep")}
    plain = depth_menu(cfg, ests, "standard", True)
    assert "4) cancel" in plain and "auto (mixed" not in plain and "Choose [2]" in plain
    ests["auto"] = estimate_run(scope, cfg, "auto", 5, PACKET_DIFF_TOKENS)
    menu = depth_menu(cfg, ests, "auto", True)
    assert "4) auto" in menu and "mixed: deep only for packets with crucial files" in menu
    assert "5) cancel" in menu and "Choose [4]" in menu
    assert "1) quick" in menu and "3) deep" in menu

    modes = ("quick", "standard", "deep")
    with_auto = (*modes, "auto")
    assert parse_choice("", "standard") == "standard"
    assert parse_choice("3", "standard") == "deep" and parse_choice("deep", "standard") == "deep"
    assert parse_choice("4", "standard") == "cancel" and parse_choice("auto", "standard") == "cancel"
    assert parse_choice("4", "standard", with_auto) == "auto"
    assert parse_choice(" AUTO ", "standard", with_auto) == "auto"
    assert parse_choice("", "auto", with_auto) == "auto"
    assert parse_choice("5", "standard", with_auto) == "cancel"
    assert parse_choice("x", "standard", with_auto) == "cancel"
