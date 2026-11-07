# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1989 review: ``pg_stats.n_distinct`` is a count when positive and minus a fraction of
the table's rows when negative; 0 means unknown. The collector stores a count or nothing."""

from __future__ import annotations

import pytest

from src.tools.database.postgres_tools import n_distinct_count


@pytest.mark.parametrize(
    ("n_distinct", "reltuples", "expected"),
    [
        (42, 1_000, 42),
        (-1, 24_276, 24_276),  # every row distinct: a unique column
        (-0.5, 1_000, 500),
        (-0.25, "4000", 1_000),
        (0, 1_000, None),  # PostgreSQL does not know
        (-1, 0, None),  # no row estimate: no count
        (-1, None, None),
        (None, 1_000, None),
        ("x", 1_000, None),
    ],
)
def test_n_distinct_becomes_a_count_or_nothing(n_distinct, reltuples, expected) -> None:
    assert n_distinct_count(n_distinct, reltuples) == expected


def test_the_collector_reads_the_row_estimate_with_the_statistics() -> None:
    from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

    class _Stub(PostgreSQLRemoteCollector):
        def __init__(self) -> None:  # no connection
            self.sql = ""

        def _query(self, sql: str):  # type: ignore[override]
            self.sql = sql
            return [
                {"col": "id", "n_distinct": -1, "reltuples": 500},
                {"col": "kind", "n_distinct": 3, "reltuples": 500},
                {"col": "blank", "n_distinct": 0, "reltuples": 500},
            ]

    stub = _Stub()
    assert stub.collect_column_cardinality("posts") == {"id": 500, "kind": 3}
    assert "reltuples" in stub.sql
