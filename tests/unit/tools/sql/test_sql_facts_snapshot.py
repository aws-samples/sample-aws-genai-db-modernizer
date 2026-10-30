# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The facade's per-query query-shape facts match the committed snapshot (#796).

A sqlglot bump or a facade change that alters any query's facts fails here;
regenerate with ``uv run python -m tests.unit.tools.sql.sql_facts_snapshot
--write`` and list the changed queries in the PR.
"""

import json

from tests.unit.tools.sql.sql_facts_snapshot import BASELINE, dump_snapshot, measure


def test_sql_facts_match_the_committed_snapshot():
    assert BASELINE.read_text(encoding="utf-8") == dump_snapshot(measure()), (
        "facade query facts changed; regenerate with "
        "`uv run python -m tests.unit.tools.sql.sql_facts_snapshot --write` "
        "and explain every changed query in the PR"
    )


_FACTS_796 = frozenset({"join_count", "has_joins", "has_aggregation", "has_subquery"})


def _differs(facts: frozenset[str] | None = None) -> dict[tuple[str, str], tuple[str, ...]]:
    snapshot = json.loads(BASELINE.read_text(encoding="utf-8"))
    return {
        (sample, qid): tuple(f for f in row["regex_differs"] if facts is None or f in facts)
        for sample, queries in snapshot.items()
        for qid, row in queries.items()
        if any(facts is None or f in facts for f in row["regex_differs"])
    }


def test_every_regex_difference_on_the_samples_is_a_known_correction():
    """Only the 21 discourse queries whose facts the regex misread (#796) differ."""
    differs = _differs(_FACTS_796)
    assert {sample for sample, _ in differs} == {"discourse"}
    assert len(differs) == 21


def test_the_facts_893_added_differ_from_their_fallback_only_where_it_misreads():
    """The regex aggregate count counts the words (a ``count`` column, ``COUNT``
    in a comment); the regex timestamp column reads ``CAST(... AS date)`` or a word in
    a literal as a column. No other fact #893 added differs from its fallback on the
    samples."""
    added = _differs(None)
    names = {f for facts in added.values() for f in facts} - _FACTS_796
    assert names == {"aggregate_function_count", "timestamp_column"}
