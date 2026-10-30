import pytest

from preflight_review.errors import SchemaError
from preflight_review.findings import (
    Category,
    Evidence,
    Finding,
    Lane,
    Severity,
    Verdict,
    from_dict,
    normalise_title,
    severity_rank,
    stable_id,
    to_dict,
)


def make(**kw):
    base = dict(
        file="a.py",
        start_line=1,
        end_line=2,
        title="Missing null check",
        claim="x may be None",
        severity=Severity.major,
        category=Category.correctness,
    )
    base.update(kw)
    return Finding(**base)


def test_round_trip_with_enums_and_evidence():
    f = make(
        lane=Lane.nitpick,
        verdict=Verdict.CONFIRMED,
        evidence=[Evidence("a.py", 1, "x.y")],
        verifier_evidence=[Evidence("b.py", 3, "q")],
        trace=[{"file": "a.py", "line": 1, "note": "n"}],
    )
    d = to_dict(f)
    assert d["severity"] == "major" and d["verdict"] == "CONFIRMED"
    assert d["evidence"] == [{"file": "a.py", "line": 1, "quote": "x.y"}]
    assert from_dict(d) == f


def test_round_trip_defaults_json_safe():
    import json

    f = make()
    assert from_dict(json.loads(json.dumps(to_dict(f)))) == f


def test_from_dict_unknown_key():
    d = to_dict(make())
    d["bogus"] = 1
    with pytest.raises(SchemaError):
        from_dict(d)


def test_from_dict_bad_enum():
    d = to_dict(make())
    d["severity"] = "huge"
    with pytest.raises(SchemaError):
        from_dict(d)


def test_normalise_title():
    assert normalise_title("Missing null-check!") == "missing null check"
    assert normalise_title("  Missing   NULL check ") == "missing null check"


def test_stable_id_normalisation_matches():
    a = make(title="Missing null check!")
    b = make(title="  missing   NULL check")
    assert stable_id(a) == stable_id(b)
    assert len(stable_id(a)) == 12


def test_stable_id_differs():
    a = make()
    assert stable_id(a) != stable_id(make(file="b.py"))
    assert stable_id(a, "foo") != stable_id(a, "bar")
    assert stable_id(a) == stable_id(a, None) == stable_id(a, "") 


def test_severity_rank_order():
    order = ["critical", "major", "minor", "trivial", "info"]
    ranks = [severity_rank(s) for s in order]
    assert ranks == [4, 3, 2, 1, 0]
    assert severity_rank(Severity.major) == 3
