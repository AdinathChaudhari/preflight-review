from preflight_review.config import Config
from preflight_review.context.learnings import Learning, retrieve_learnings


def _l(i: str, glob: str) -> Learning:
    return Learning(i, "2026-10-30", glob, "text", None, 0)


def test_path_filtering():
    ls = [_l("L1", "src/**"), _l("L2", "docs/**")]
    out = retrieve_learnings(ls, ["src/a.py"], Config())
    assert [l.id for l in out] == ["L1"]


def test_cap_applied():
    cfg = Config()
    cfg.learnings.cap = 2
    ls = [_l(f"L{i}", "src/**") for i in range(5)]
    out = retrieve_learnings(ls, ["src/a.py"], cfg)
    assert [l.id for l in out] == ["L0", "L1"]
