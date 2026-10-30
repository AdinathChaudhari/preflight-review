import pytest

from preflight_review.errors import StateError
from preflight_review.state import (
    STATE_PATH,
    BranchState,
    FindingState,
    State,
    load_state,
    save_state,
)


def test_missing_is_empty(tmp_path):
    assert load_state(tmp_path) == State()


def test_round_trip(tmp_path):
    s = State(
        branches={
            "feat": BranchState(
                last_reviewed_sha="abc",
                round=2,
                findings={"id1": FindingState(status="open", first_round=1, last_seen_round=2)},
            )
        }
    )
    save_state(tmp_path, s)
    assert load_state(tmp_path) == s


def test_no_temp_files_left(tmp_path):
    save_state(tmp_path, State())
    save_state(tmp_path, State())
    assert [p.name for p in (tmp_path / STATE_PATH).parent.iterdir()] == ["state.json"]


def test_corrupt_json(tmp_path):
    p = tmp_path / STATE_PATH
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    with pytest.raises(StateError):
        load_state(tmp_path)


def test_wrong_version(tmp_path):
    p = tmp_path / STATE_PATH
    p.parent.mkdir(parents=True)
    p.write_text('{"version": 7, "branches": {}}')
    with pytest.raises(StateError):
        load_state(tmp_path)


def test_finding_state_new_fields_round_trip_and_legacy(tmp_path):
    s = State(branches={"b": BranchState(findings={
        "x": FindingState(status="open", title="T", file="a.py", start_line=4)})})
    save_state(tmp_path, s)
    assert load_state(tmp_path) == s
    p = tmp_path / STATE_PATH
    import json as _j
    data = _j.loads(p.read_text())
    del data["branches"]["b"]["findings"]["x"]["title"]
    del data["branches"]["b"]["findings"]["x"]["file"]
    del data["branches"]["b"]["findings"]["x"]["start_line"]
    p.write_text(_j.dumps(data))
    fs = load_state(tmp_path).branches["b"].findings["x"]
    assert fs.title is None and fs.file is None and fs.start_line is None
