"""Unit tests for the routing snapshot diff logic (#497).

``tests/e2e/routing_snapshot.py`` has no module-level dependency on the ``e2e``
extra (playwright/pypdf) -- those imports are all deferred inside ``measure()``
and the ``_write*`` functions -- so these pure ``compare()``/``format_diff()``
tests run under ``make test`` (``ci/test.sh``), not just ``make e2e``. The real
pipeline-backed snapshot test lives in ``tests/e2e/test_routing_snapshot.py``.
"""

from __future__ import annotations

import json

from tests.e2e.routing_snapshot import compare, format_diff


def _query(engine="dynamodb", cat="individual_highest_confidence", scores=None, final=None):
    entry = {"s": scores or {"dynamodb": 85, "aurora_mysql": 70}, "f": {"e": engine, "c": cat}}
    if final:
        entry["r"] = final
    return entry


def _sample(queries, consolidations=None):
    return {"query_count": len(queries), "queries": queries, "consolidations": consolidations or []}


def test_compare_returns_empty_for_identical_snapshots():
    sample = _sample({"q1": _query()})
    assert compare(sample, json.loads(json.dumps(sample))) == {}


def test_compare_flags_a_score_change():
    base = _sample({"q1": _query(scores={"dynamodb": 85, "aurora_mysql": 70})})
    moved = _sample({"q1": _query(scores={"dynamodb": 60, "aurora_mysql": 70})})
    diff = compare(base, moved)
    assert set(diff) == {"scores"}
    assert diff["scores"]["q1"]["before"]["dynamodb"] == 85
    assert diff["scores"]["q1"]["after"]["dynamodb"] == 60


def test_compare_flags_first_assignment_change():
    base = _sample({"q1": _query(engine="dynamodb")})
    moved = _sample({"q1": _query(engine="aurora_mysql")})
    diff = compare(base, moved)
    assert diff["first_assignment"]["q1"]["before"]["e"] == "dynamodb"
    assert diff["first_assignment"]["q1"]["after"]["e"] == "aurora_mysql"
    # final_assignment mirrors first_assignment when neither has an "r" override,
    # so it changes too -- a resolver move always shows up in both.
    assert diff["final_assignment"]["q1"]["after"]["e"] == "aurora_mysql"


def test_compare_flags_final_assignment_change_even_when_first_is_unchanged():
    base = _sample({"q1": _query(engine="dynamodb")})
    moved = _sample(
        {
            "q1": _query(
                engine="dynamodb",
                final={"e": "aurora_mysql", "c": "reality_check_no_unique_value", "fit": 60},
            )
        }
    )
    diff = compare(base, moved)
    assert "first_assignment" not in diff
    assert diff["final_assignment"]["q1"]["before"]["e"] == "dynamodb"
    assert diff["final_assignment"]["q1"]["after"]["e"] == "aurora_mysql"
    assert diff["final_assignment"]["q1"]["after"]["fit"] == 60


def test_compare_flags_a_fit_only_change():
    """#497 review, critical 1: an engine fit change that moves no query is still a
    real behavior change (e.g. a scoring-constant edit) and must not pass silently."""
    base = _sample(
        {
            "q1": _query(
                engine="dynamodb",
                final={"e": "aurora_mysql", "c": "reality_check_no_unique_value", "fit": 60},
            )
        }
    )
    moved = json.loads(json.dumps(base))
    moved["queries"]["q1"]["r"]["fit"] = 59
    diff = compare(base, moved)
    assert diff["final_assignment"]["q1"]["before"]["fit"] == 60
    assert diff["final_assignment"]["q1"]["after"]["fit"] == 59


def test_compare_flags_a_group_score_change():
    """#497 review, critical 2: the per-engine co-dependency group score must be
    part of the comparison, not just the winning engine."""
    base = _sample(
        {
            "q1": _query(
                engine="aurora_mysql",
                cat="co_dependency_group",
                scores={"aurora_mysql": 80},
            )
        }
    )
    base["queries"]["q1"]["f"]["grp"] = {
        "key": "abc123",
        "scores": {"aurora_mysql": 80.0, "dynamodb": 40.0},
    }
    moved = json.loads(json.dumps(base))
    moved["queries"]["q1"]["f"]["grp"]["scores"]["dynamodb"] = 10.0
    diff = compare(base, moved)
    assert "q1" in diff.get("first_assignment", {})


def test_compare_flags_a_cache_engine_change():
    base = _sample({"q1": _query()})
    base["queries"]["q1"]["cache"] = "elasticache"
    moved = json.loads(json.dumps(base))
    del moved["queries"]["q1"]["cache"]
    diff = compare(base, moved)
    assert diff["cache_engine"]["q1"] == {"before": "elasticache", "after": None}


def test_compare_flags_consolidation_record_changes():
    base = _sample(
        {},
        consolidations=[
            {
                "from": "aurora_mysql",
                "to": "dynamodb",
                "n": 5,
                "action": "full",
                "cost": 150.0,
                "retained": [],
            }
        ],
    )
    moved = _sample({})
    diff = compare(base, moved)
    key = ("aurora_mysql", "dynamodb", "full")
    assert diff["consolidations"] == {key: {"before": base["consolidations"][0], "after": None}}


def test_compare_flags_consolidation_retained_queries_change():
    """#497 review, critical 1: queries_retained/retention_reason must be covered."""
    base = _sample(
        {},
        consolidations=[
            {
                "from": "aurora_mysql",
                "to": "dynamodb",
                "n": 5,
                "action": "partial",
                "cost": 0.0,
                "retained": ["q7"],
                "retention_reason": "Mandatory signal queries stay on their engine",
            }
        ],
    )
    moved = json.loads(json.dumps(base))
    moved["consolidations"][0]["retained"] = ["q7", "q8"]
    diff = compare(base, moved)
    key = ("aurora_mysql", "dynamodb", "partial")
    assert key in diff["consolidations"]
    assert diff["consolidations"][key]["after"]["retained"] == ["q7", "q8"]


def test_compare_keys_consolidations_by_from_to_action_not_the_whole_record():
    """#497 review, optional: a cost/count-only change on the same (from, to, action)
    must diff as a change to that one record, not the whole list collapsing."""
    base = _sample(
        {},
        consolidations=[
            {
                "from": "opensearch",
                "to": "aurora_mysql",
                "n": 10,
                "action": "full",
                "cost": 450.0,
                "retained": [],
            }
        ],
    )
    moved = json.loads(json.dumps(base))
    moved["consolidations"][0]["n"] = 11
    moved["consolidations"][0]["cost"] = 460.0
    diff = compare(base, moved)
    key = ("opensearch", "aurora_mysql", "full")
    assert set(diff["consolidations"]) == {key}
    assert diff["consolidations"][key]["before"]["n"] == 10
    assert diff["consolidations"][key]["after"]["n"] == 11


def test_compare_flags_a_new_or_removed_query():
    base = _sample({"q1": _query()})
    added = _sample({"q1": _query(), "q2": _query()})
    diff_added = compare(base, added)
    assert diff_added["first_assignment"]["q2"]["before"] is None
    assert diff_added["first_assignment"]["q2"]["after"]["e"] == "dynamodb"

    diff_removed = compare(added, base)
    assert diff_removed["first_assignment"]["q2"]["after"] is None


def test_compare_only_reports_the_queries_that_actually_changed():
    base = _sample({"q1": _query(engine="dynamodb"), "q2": _query(engine="aurora_mysql")})
    moved = json.loads(json.dumps(base))
    moved["queries"]["q1"]["f"]["e"] = "opensearch"
    diff = compare(base, moved)
    assert set(diff["first_assignment"]) == {"q1"}
    assert "q2" not in diff.get("first_assignment", {})
    assert "q2" not in diff.get("final_assignment", {})
    assert "q2" not in diff.get("scores", {})


def test_format_diff_is_readable_and_grouped():
    base = _sample({"q1": _query(engine="dynamodb", scores={"dynamodb": 85, "aurora_mysql": 70})})
    moved = _sample(
        {"q1": _query(engine="aurora_mysql", scores={"dynamodb": 60, "aurora_mysql": 70})}
    )
    diff = compare(base, moved)
    message = format_diff("wordpress", diff)
    assert "wordpress" in message
    assert "q1" in message
    assert "dynamodb" in message and "aurora_mysql" in message
    assert "score" in message.lower()
    assert "first assignment" in message.lower()
    assert "final assignment" in message.lower()


def test_format_diff_includes_a_query_text_excerpt_when_given():
    base = _sample({"q1": _query(engine="dynamodb")})
    moved = _sample({"q1": _query(engine="aurora_mysql")})
    diff = compare(base, moved)
    message = format_diff(
        "wordpress", diff, query_text={"q1": "SELECT * FROM wp_posts WHERE id = 1"}
    )
    assert "SELECT * FROM wp_posts" in message


def test_format_diff_shows_consolidation_records():
    base = _sample(
        {},
        consolidations=[
            {
                "from": "aurora_mysql",
                "to": "dynamodb",
                "n": 5,
                "action": "full",
                "cost": 150.0,
                "retained": [],
            }
        ],
    )
    moved = _sample({})
    diff = compare(base, moved)
    message = format_diff("discourse", diff)
    assert "consolidation" in message.lower()
    assert "aurora_mysql" in message and "dynamodb" in message


def test_format_diff_truncates_a_large_number_of_changed_queries():
    base = _sample({f"q{i}": _query(engine="dynamodb") for i in range(50)})
    moved = _sample({f"q{i}": _query(engine="aurora_mysql") for i in range(50)})
    diff = compare(base, moved)
    message = format_diff("discourse", diff)
    assert "more" in message.lower()


def test_compare_flags_a_first_assignment_move_change():
    base = _sample({"q1": {**_query(), "mv": ["score:highest_score"]}})
    moved = _sample({"q1": {**_query(), "mv": ["tie:tie.source_compatible"]}})
    diff = compare(base, moved)
    assert set(diff) == {"moves"}
    assert diff["moves"]["q1"] == {
        "before": ["score:highest_score"],
        "after": ["tie:tie.source_compatible"],
    }
    assert "first-assignment moves" in format_diff("wordpress", diff)


def test_compare_flags_a_moves_by_pass_change():
    base = {**_sample({"q1": _query()}), "moves_by_pass": {"score": {"highest_score": 1}}}
    moved = {**_sample({"q1": _query()}), "moves_by_pass": {"tie": {"tie.source_compatible": 1}}}
    diff = compare(base, moved)
    assert set(diff) == {"moves_by_pass"}
    assert "moves by pass changed" in format_diff("wordpress", diff)


def test_compare_flags_a_reality_check_move_change():
    base = _sample({"q1": {**_query(), "rmv": ["settle:consolidate.no_unique_value"]}})
    moved = _sample({"q1": {**_query(), "rmv": ["sweep:sweep.orphan"]}})
    diff = compare(base, moved)
    assert set(diff) == {"rc_moves"}
    assert diff["rc_moves"]["q1"] == {
        "before": ["settle:consolidate.no_unique_value"],
        "after": ["sweep:sweep.orphan"],
    }
    assert "reality-check moves (pass:reason_code)" in format_diff("wordpress", diff)


def test_a_query_the_reality_check_did_not_move_has_no_rc_moves_entry():
    # ``rmv`` is written only for a query the check moved; absent equals empty.
    base = _sample({"q1": _query()})
    moved = _sample({"q1": {**_query(), "rmv": []}})
    assert compare(base, moved) == {}


def test_compare_flags_a_reality_check_moves_by_pass_change():
    base = {
        **_sample({"q1": _query()}),
        "rc_moves_by_pass": {"settle": {"consolidate.no_unique_value": 2}},
    }
    moved = {**_sample({"q1": _query()}), "rc_moves_by_pass": {}}
    diff = compare(base, moved)
    assert set(diff) == {"rc_moves_by_pass"}
    assert "reality-check moves by pass changed" in format_diff("wordpress", diff)
