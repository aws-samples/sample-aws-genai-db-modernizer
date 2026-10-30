# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every consumer handles a row count the source does not have explicitly (#1636).

Before #1636 an unknown count reached every consumer as 0, so a table the source had
no statistics for was sized, scored and classified as an empty one: a 50 GB table
with no statistics was a "small reference table". Now the count is None, and:

- sizing arithmetic (schema-design metrics, the analysis cost band, the load test)
  uses the one stated assumption, the table's size over its declared row width, and
  says so (``row_count_assumed``);
- a row-count threshold (a small table, a large one, a bounded fan-out) reads the
  same assumption when the table's size is collected, and finds nothing when it is not;
- a figure that cannot use an assumption (a population rate from statistics) is left
  out, as it is with no statistics.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.access_pattern import KEY_RANGE, REFERENCE_READ, SchemaKeys, classify_query
from src.tools.analysis.scoring import _cost, build_table_profiles
from src.tools.schema.design_metrics import (
    build_design_metrics,
    row_count_or_assumed,
    schema_item_size_bytes,
    sizing_row_count,
)

_COLUMNS: list[dict[str, Any]] = [
    {"column_name": "id", "normalized_data_type": "integer", "data_type": "integer"},
    {
        "column_name": "name",
        "normalized_data_type": "string",
        "data_type": "varchar(90)",
        "max_length": 90,
    },
]
# len("id") + 8 + len("name") + 90 = 104 bytes a row.
_WIDTH = 104


def _table(name: str, rows: int | None, size_mb: float | None, **extra: Any) -> dict[str, Any]:
    return {
        "table_id": f"shop.{name}",
        "table_name": name,
        "schema_name": "shop",
        "primary_key": ["id"],
        "row_count": rows,
        "size_mb": size_mb,
        "columns": [dict(c) for c in _COLUMNS],
        **extra,
    }


def test_the_declared_width() -> None:
    assert schema_item_size_bytes(_COLUMNS) == _WIDTH


# --------------------------------------------------------------------------- the assumption


@pytest.mark.parametrize(
    ("rows", "size_mb", "threshold", "sizing"),
    [
        (5000, 2.0, (5000, False), (5000, False)),
        (0, 0.02, (0, False), (0, False)),  # an empty table is empty
        # 1 MB / 104 bytes = 10,082 rows, never 0.
        (None, 1.0, (10082, True), (10082, True)),
        (None, 0.0, (0, True), (0, True)),  # no pages: the size says empty
        # Neither collected: a threshold finds nothing; sizing takes the 0.01 MB stand-in.
        (None, None, (None, False), (101, True)),
    ],
)
def test_row_count_or_assumed_and_sizing_row_count(
    rows: int | None, size_mb: float | None, threshold: tuple, sizing: tuple
) -> None:
    table = _table("t", rows, size_mb)
    assert row_count_or_assumed(table) == threshold
    assert sizing_row_count(table) == sizing


def test_design_metrics_size_an_unknown_table_by_the_assumption_and_say_so() -> None:
    tables = [
        _table("users", 512, 1.0),
        _table("posts", None, 2.0, foreign_keys=[{"referenced_table": "users"}]),
        _table("empty", 0, 0.02),
    ]
    metrics = build_design_metrics("documentdb", tables, []).model_dump()
    by_id = {t["table_id"]: t for t in metrics["tables"]}
    # 2 MB / 104 bytes = 20,165 rows; the row size is the declared width, not 2 MB.
    assert (by_id["shop.posts"]["row_count"], by_id["shop.posts"]["row_count_assumed"]) == (
        20165,
        True,
    )
    assert by_id["shop.posts"]["avg_row_size_bytes"] == _WIDTH
    # A known count, and a real 0, are the collector's.
    assert (by_id["shop.users"]["row_count"], by_id["shop.users"]["row_count_assumed"]) == (
        512,
        False,
    )
    assert by_id["shop.users"]["avg_row_size_bytes"] == 2048
    assert (by_id["shop.empty"]["row_count"], by_id["shop.empty"]["row_count_assumed"]) == (
        0,
        False,
    )
    [embedding] = metrics["documentdb_embeddings"]
    assert embedding["counts_assumed"] is True
    assert embedding["avg_children_per_parent"] == round(20165 / 512, 2)


# --------------------------------------------------------------------------- cost band


class _NoPatterns:
    patterns_detected: list = []
    anti_patterns_detected: list = []


def _cost_score(table: dict[str, Any]) -> int:
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [table]},
        "queries": {"query_patterns": []},
    }
    profile = build_table_profiles(collector, _NoPatterns())[table["table_id"]]
    return _cost(profile)


def test_an_unknown_large_table_is_not_scored_as_an_empty_one() -> None:
    # 5 GB with no statistics: about 51.6 million rows by its size, the largest band.
    unknown_large = _cost_score(_table("big", None, 5120.0))
    known_empty = _cost_score(_table("big", 0, 5120.0))
    known_large = _cost_score(_table("big", 50_000_000, 5120.0))
    assert unknown_large == known_large < known_empty


def test_an_unknown_small_table_scores_as_its_size_says() -> None:
    assert _cost_score(_table("tiny", None, 0.02)) == _cost_score(_table("tiny", 200, 0.02))


# --------------------------------------------------------------------------- access pattern


def _class(table: dict[str, Any], sql: str) -> tuple[str, str]:
    keys = SchemaKeys.from_collector({"database_schema": {"tables": [table]}})
    found = classify_query(
        {"query_id": "q", "query_text": sql, "query_type": "SELECT"}, keys, "postgresql"
    )
    return found.name, found.reason


def test_an_unknown_large_table_read_whole_is_not_a_reference_read() -> None:
    assert _class(_table("settings", None, 5120.0), "SELECT id, name FROM settings")[0] == (
        KEY_RANGE
    )


def test_an_unknown_small_table_read_whole_is_one_and_the_reason_says_why() -> None:
    name, reason = _class(_table("settings", None, 0.02), "SELECT id, name FROM settings")
    assert name == REFERENCE_READ
    # 0.02 MB / 104 bytes = 202 rows.
    assert "about 202 rows by its size; the source has no row count" in reason


def test_a_table_with_neither_count_nor_size_is_not_known_small() -> None:
    assert _class(_table("settings", None, None), "SELECT id, name FROM settings")[0] == (KEY_RANGE)


@pytest.mark.passes_on_main  # a known count's reason is unchanged
def test_a_known_small_table_keeps_its_reason() -> None:
    name, reason = _class(_table("settings", 40, 0.02), "SELECT id, name FROM settings")
    assert name == REFERENCE_READ
    assert "a small table (40 rows)" in reason


# --------------------------------------------------------------------------- analysis rules


def _parent_child(child_rows: int | None, child_size: float | None) -> dict[str, Any]:
    parent = _table("customers", 1000, 0.2)
    child = _table(
        "orders",
        child_rows,
        child_size,
        columns=[*_COLUMNS, {"column_name": "customer_id", "normalized_data_type": "integer"}],
        foreign_keys=[
            {
                "constraint_name": "fk",
                "columns": ["customer_id"],
                "referenced_table": "customers",
                "referenced_columns": ["id"],
            }
        ],
    )
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [parent, child]},
        "queries": {"query_patterns": []},
    }


def _bounded(collector: dict[str, Any]) -> list:
    from src.tools.analysis.dynamodb_analysis_tools import (
        detect_denormalization_subtypes,
        detect_relationships,
    )

    found = detect_denormalization_subtypes(collector, detect_relationships(collector))
    return [o for o in found if o.subtype == "bounded-parent-child"]


def test_an_unknown_child_is_not_a_bounded_child_of_any_parent() -> None:
    # Before #1636 the unknown child counted 0 rows: ratio 0, always "bounded".
    # 50 GB of 123-byte rows is about 436 million rows: 436,480 per customer.
    assert _bounded(_parent_child(None, 51200.0)) == []
    # A child whose size says it is small is bounded, by the assumption.
    [small] = _bounded(_parent_child(None, 2.0))
    assert small.cardinality_ratio is not None and small.cardinality_ratio <= 100
    # With neither a count nor a size, nothing says it is bounded.
    assert _bounded(_parent_child(None, None)) == []


@pytest.mark.passes_on_main  # a known count's ratio is unchanged
def test_a_known_child_count_still_decides() -> None:
    assert _bounded(_parent_child(50_000, 51200.0))
    assert _bounded(_parent_child(500_000, 0.2)) == []


def _extended_reference(lookup_rows: int | None, lookup_size: float | None) -> bool:
    from src.tools.analysis.documentdb_analysis_tools import analyze_documentdb_use_cases

    big = _table("orders", 2_000_000, 400.0)
    lookup = _table("statuses", lookup_rows, lookup_size)
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [big, lookup]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": (
                        "SELECT o.id, s.name FROM orders o JOIN statuses s ON s.id = o.id"
                    ),
                    "query_type": "SELECT",
                    "tables_accessed": ["shop.orders", "shop.statuses"],
                    "calls_per_second": 5.0,
                    "has_joins": True,
                    "join_count": 1,
                }
            ]
        },
    }
    analysis = analyze_documentdb_use_cases(collector)
    return any(
        "q1" in (p.query_ids or []) and "extended" in str(p.pattern_type).lower()
        for p in analysis.patterns_detected
    )


def test_an_unknown_lookup_table_is_small_only_when_its_size_says_so() -> None:
    assert _extended_reference(None, 0.02)  # about 202 rows by its size
    assert not _extended_reference(None, 5120.0)  # about 51.6 million rows
    # Before #1636 a table with no count and no size counted 0 rows: "small".
    assert not _extended_reference(None, None)


# --------------------------------------------------------------------------- substring scans


def _substring(size_mb: float | None):
    from src.agents.referee.substring_match import engine_reason, scan_penalty, substring_matches
    from src.shared.catalog.patterns import pattern

    threshold = pattern("substring-match").scan_threshold
    assert threshold is not None
    collector = {
        "metadata": {"source_database": {"engine": "mysql"}},
        "database_schema": {"tables": [_table("users", None, size_mb)]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT id FROM users WHERE name LIKE '%@example.com'",
                    "query_type": "SELECT",
                    "tables_accessed": ["shop.users"],
                    "calls_per_second": threshold.min_calls_per_second,
                }
            ]
        },
    }
    match = substring_matches(collector)["q1"]
    return match, scan_penalty(match, "aurora_mysql"), engine_reason("aurora_mysql", match)


def test_a_scan_of_an_unknown_large_table_is_penalised_and_says_its_rows_are_assumed() -> None:
    # Before #1636 the unknown table counted 0 rows, so its scan was never penalised.
    match, penalty, reason = _substring(5120.0)
    assert (match.table_rows, match.table_rows_assumed) == (51622203, True)
    assert penalty > 0
    assert "about 51,622,203 rows by its size (the source has no row count)" in str(reason)
    assert "no collected row count" not in str(reason)


def test_a_scan_of_an_unknown_small_table_is_not_penalised() -> None:
    match, penalty, _ = _substring(0.02)
    assert (match.table_rows, match.table_rows_assumed, penalty) == (202, True, 0)
    assert match.approximations == ()


def test_a_scan_of_a_table_with_neither_count_nor_size_says_it_cannot_be_sized() -> None:
    match, penalty, reason = _substring(None)
    assert (match.table_rows, penalty) == (None, 0)
    assert "has no collected row count, so the scan cannot be sized" in str(reason)


@pytest.mark.passes_on_main  # routing's cost dimension read an unknown count as no width before
def test_routing_gives_a_table_with_no_row_count_no_row_width() -> None:
    from src.agents.referee.query_cost import row_widths

    collector = {
        "database_schema": {"tables": [_table("unknown", None, 5120.0), _table("known", 1024, 1.0)]}
    }
    assert row_widths(collector) == {"shop.known": 1024.0}


def test_the_assumption_divides_the_data_size_not_the_indexes() -> None:
    # An empty wordpress-like table: one 16 KB data page and 0.11 MB of indexes. Dividing
    # the total would assume 1,311 rows; the data alone gives 202.
    table = _table("t", None, 0.13, data_size_mb=0.02)
    assert row_count_or_assumed(table) == (202, True)
    assert sizing_row_count(table) == (202, True)
