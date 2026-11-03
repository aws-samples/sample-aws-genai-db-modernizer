# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two per-query steps of the routing stay linear at 10,000 statements (#384).

``QueryCostModel.cost`` counted the queries sharing an engine's standing cost by
copying the set on every call, and ``_moving_members`` scanned every query's trace for
each grouped query. Both gave n^2 work over a pass. Each now gives the same answer
without the copy or the scan; these tests check the answer is the one it was, and that
the work no longer grows with the workload.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.query_cost import QueryCostModel
from src.agents.referee.reality_check_rules import GroupedTrace, _moving_members
from src.shared import catalog
from tests.unit.tools.sql._timing import assert_cost_ratio

QIDS = st.sets(st.sampled_from([f"q{i}" for i in range(12)] + [""]), max_size=12)


@settings(max_examples=200, deadline=None)
@given(servable=QIDS, qid=st.sampled_from(["q0", "q5", "q11", "qX", ""]))
def test_the_standing_share_is_the_one_the_set_copy_gave(servable, qid):
    model = QueryCostModel(source_engine="")
    servable = frozenset(servable)
    for engine in ("dynamodb", "aurora_postgresql"):
        cost = model.cost({"query_id": qid}, engine, servable)
        expected = catalog.pricing(engine).base_monthly / len(servable | {qid})
        assert cost.standing_share == expected


def test_the_standing_share_does_not_copy_the_workload():
    model = QueryCostModel(source_engine="")
    small = frozenset(f"q{i}" for i in range(100))
    large = frozenset(f"q{i}" for i in range(200_000))

    def calls(servable: frozenset[str]):
        return lambda: [model.cost({"query_id": "new"}, "dynamodb", servable) for _ in range(200)]

    # A copy of 200,000 ids per call is thousands of times the 100-id copy; without
    # one the two cost about the same.
    assert_cost_ratio(calls(large), calls(small), max_ratio=20)


# -- _moving_members ---------------------------------------------------------------------


def _trace(groups: dict[str, str | None]) -> dict[str, dict]:
    return {q: ({"group": {"key": g}} if g else {}) for q, g in groups.items()}


GROUPS = st.dictionaries(
    st.sampled_from([f"q{i}" for i in range(10)]),
    st.sampled_from(["a", "b", None]),
    min_size=1,
)


@settings(max_examples=200, deadline=None)
@given(groups=GROUPS, data=st.data())
def test_the_index_gives_the_members_the_scan_gave(groups, data):
    trace = _trace(groups)
    qids = list(trace)
    staying = set(data.draw(st.lists(st.sampled_from(qids))))
    held = set(data.draw(st.lists(st.sampled_from(qids))))
    indexed = GroupedTrace(trace)
    for qid in [*qids, "unknown"]:
        assert _moving_members(qid, indexed, staying, held) == _moving_members(
            qid, trace, staying, held
        )


def test_the_index_is_a_copy_that_reads_like_the_trace():
    trace = _trace({"q1": "a", "q2": "a", "q3": None})
    indexed = GroupedTrace(trace)
    assert indexed == trace
    assert indexed.group_members == {"a": ["q1", "q2"]}
    assert _moving_members("q2", indexed, (), {"q1", "q2"}) == ["q1", "q2"]
    assert _moving_members("q3", indexed, (), {"q3"}) == ["q3"]


def test_a_members_lookup_does_not_read_the_whole_trace():
    def lookups(n: int):
        # n queries in groups of two: the scan read all n traces for every lookup.
        trace = GroupedTrace(_trace({f"q{i}": f"g{i // 2}" for i in range(n)}))
        held = set(trace)
        qids = list(trace)[:200]
        return lambda: [_moving_members(qid, trace, (), held) for qid in qids]

    # The scan makes a lookup in 20,000 queries 100x one in 200; the index, about 1x.
    assert_cost_ratio(lookups(20_000), lookups(200), max_ratio=10)


def test_the_wrapped_trace_refuses_every_write():
    trace = GroupedTrace(_trace({"q1": "a", "q2": None}))
    writes = [
        lambda: trace.__setitem__("q3", {}),
        lambda: trace.__delitem__("q1"),
        lambda: trace.update({"q3": {}}),
        lambda: trace.setdefault("q3", {}),
        lambda: trace.pop("q1"),
        lambda: trace.popitem(),
        trace.clear,
    ]
    for write in writes:
        with pytest.raises(TypeError, match="read-only"):
            write()
    assert trace.group_members == {"a": ["q1"]} and set(trace) == {"q1", "q2"}


def test_a_group_changed_after_wrapping_is_refused():
    plain = _trace({"q1": "a", "q2": "a"})
    trace = GroupedTrace(plain)
    plain["q2"]["group"]["key"] = "b"  # the payload is shared with the resolver's dict
    with pytest.raises(RuntimeError, match="group changed"):
        _moving_members("q2", trace, (), {"q1", "q2"})
