# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table's row count and size keep three states: a number, a real 0, unknown (#1636).

``src.shared.row_counts`` is the one place the per-engine rule lives. These cases are
each engine's own semantics, for a collection written since #1636 (it carries
``row_count_basis``) and for one written before (it does not, and its scripts turned
some unknown counts into 0).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.shared.row_counts import (
    ESTIMATE,
    EXACT,
    NO_SIZE_ASSUMPTION,
    ONE_PAGE_MB,
    UNKNOWN_ROW_COUNT_ASSUMPTION,
    assumption_size_mb,
    engine_basis,
    engine_major_version,
    read_data_size_mb,
    read_row_count,
    read_size_mb,
    row_count_facts,
    unknown_size_assumption,
)


def _new(row_count: object, basis: str | None = ESTIMATE, **extra: object) -> dict:
    return {"row_count": row_count, "row_count_basis": basis, **extra}


# --------------------------------------------------------------------------- new scripts


@pytest.mark.parametrize("engine", ["postgresql", "oracle", "mysql", "mariadb", "db2"])
def test_a_null_count_is_unknown_from_every_estimating_source(engine: str) -> None:
    read = read_row_count(_new(None), engine)
    assert (read.row_count, read.basis) == (None, None)


@pytest.mark.parametrize("engine", ["postgresql", "oracle", "sqlserver", "db2"])
def test_a_reported_zero_is_an_empty_table_in_a_new_collection(engine: str) -> None:
    read = read_row_count(_new(0), engine)
    assert (read.row_count, read.basis) == (0, ESTIMATE)


@pytest.mark.parametrize("engine", ["postgresql", "db2", "sqlserver", "oracle", ""])
def test_a_negative_count_is_unknown_from_any_source(engine: str) -> None:
    # PostgreSQL 14+ reltuples and Db2 CARD say "no statistics" with -1.
    assert read_row_count(_new(-1), engine).row_count is None
    assert read_row_count({"row_count": -1}, engine).row_count is None


def test_the_statistics_time_travels_with_the_count() -> None:
    read = read_row_count(_new(1200, stats_analyzed_at="2026-10-20T08:15:00.5+00:00"), "oracle")
    assert (read.row_count, read.basis) == (1200, ESTIMATE)
    assert read.analyzed_at == datetime(2026, 10, 20, 8, 15, 0, 500000, tzinfo=UTC)
    assert read.fields()["stats_analyzed_at"] == read.analyzed_at


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-20T08:15:00", datetime(2026, 10, 20, 8, 15)),  # Oracle TO_CHAR
        ("2026-10-20 08:15:00+00", datetime(2026, 10, 20, 8, 15, tzinfo=UTC)),  # psql text
        ("2026-10-20T08:15:00Z", datetime(2026, 10, 20, 8, 15, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_statistics_times_are_read_or_dropped(text: str | None, expected: datetime | None) -> None:
    assert read_row_count(_new(5, stats_analyzed_at=text), "oracle").analyzed_at == expected


def test_the_engines_basis_wins_and_an_unknown_source_keeps_the_reported_one() -> None:
    assert read_row_count(_new(7, "exact"), "sqlserver").basis == ESTIMATE
    assert read_row_count(_new(7, "guess"), "oracle").basis == ESTIMATE
    assert read_row_count(_new(7, "exact"), "cassandra").basis == EXACT
    assert read_row_count(_new(7, "guess"), "cassandra").basis is None


@pytest.mark.parametrize(
    ("engine", "storage", "basis"),
    [
        # Microsoft documents sys.partitions.rows as "the approximate number of rows".
        ("sqlserver", None, ESTIMATE),
        ("mysql", "MyISAM", EXACT),
        ("mariadb", "Aria", EXACT),
        ("mysql", "InnoDB", ESTIMATE),
        ("mariadb", None, ESTIMATE),
        ("postgresql", None, ESTIMATE),
        ("oracle", None, ESTIMATE),
        ("db2", None, ESTIMATE),
        ("", None, None),
        ("cassandra", None, None),
    ],
)
def test_each_engines_basis(engine: str, storage: str | None, basis: str | None) -> None:
    assert engine_basis(engine, storage) == basis


# --------------------------------------------------------------------------- MySQL zeros


@pytest.mark.parametrize("basis", [{}, {"row_count_basis": ESTIMATE}], ids=["old", "new"])
@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_an_innodb_zero_on_one_page_is_an_empty_table_old_or_new(engine: str, basis: dict) -> None:
    # TABLE_ROWS is NULL only for views and INFORMATION_SCHEMA tables, so the old
    # IFNULL(table_rows, 0) never made a base table's 0; an empty InnoDB table is one
    # 16 KB page, 0.02 MB as the scripts round it (wordpress's 31 zeros).
    table = {"row_count": 0, "engine": "InnoDB", "data_size_mb": ONE_PAGE_MB, **basis}
    assert read_row_count(table, engine).fields()["row_count"] == 0


@pytest.mark.parametrize("basis", [{}, {"row_count_basis": ESTIMATE}], ids=["old", "new"])
def test_an_innodb_zero_on_more_than_a_page_is_unknown_old_or_new(basis: dict) -> None:
    # Data beyond one page holds rows: the 0 is statistics that have not caught up.
    table = {"row_count": 0, "engine": "InnoDB", "data_size_mb": 0.03, **basis}
    assert read_row_count(table, "mysql").row_count is None


def test_an_innodb_zero_with_no_data_size_is_the_servers_figure() -> None:
    # A collector output written before #1636 holds only the total size.
    assert read_row_count({"row_count": 0, "storage_engine": "InnoDB"}, "mysql").row_count == 0


@pytest.mark.parametrize("key", ["engine", "storage_engine"])
def test_a_myisam_zero_is_exact_whatever_its_size(key: str) -> None:
    read = read_row_count({"row_count": 0, key: "MyISAM", "data_size_mb": 5.0}, "mysql")
    assert (read.row_count, read.basis) == (0, EXACT)


# --------------------------------------------------------------------------- old collections


@pytest.mark.parametrize("engine", ["oracle", "postgresql", "", "unknown"])
def test_an_old_zero_from_a_source_that_cannot_tell_is_unknown(engine: str) -> None:
    # Oracle NVL(NUM_ROWS, 0) and PostgreSQL before 14's 0 for a table never analysed
    # cannot tell an empty table from an unknown one; neither can an unknown source.
    read = read_row_count({"row_count": 0}, engine)
    assert (read.row_count, read.basis) == (None, None)


@pytest.mark.parametrize(("major", "expected"), [(13, None), (14, 0), (16, 0), (None, None)])
def test_an_old_postgresql_zero_reads_by_the_servers_version(
    major: int | None, expected: int | None
) -> None:
    # PostgreSQL 14 added reltuples -1 for "never analysed", so an older collection of
    # a 14+ server holds a 0 only for a table its statistics found empty.
    assert read_row_count({"row_count": 0}, "postgresql", engine_major=major).row_count == expected


@pytest.mark.parametrize(
    ("version", "major"),
    [("PostgreSQL 16.10 on x86_64-pc-linux-gnu", 16), ("13.4", 13), ("", None), (None, None)],
)
def test_the_major_version(version: str | None, major: int | None) -> None:
    assert engine_major_version(version) == major


@pytest.mark.parametrize("engine", ["sqlserver", "db2"])
def test_an_old_zero_from_a_kept_or_distinct_unknown_source_stays_zero(engine: str) -> None:
    assert read_row_count({"row_count": 0}, engine).row_count == 0


def test_an_old_nonzero_count_is_kept_with_the_engines_basis() -> None:
    assert read_row_count({"row_count": 12537, "engine": "InnoDB"}, "mysql").fields() == {
        "row_count": 12537,
        "row_count_basis": ESTIMATE,
        "stats_analyzed_at": None,
    }
    assert read_row_count({"row_count": 290}, "sqlserver").basis == ESTIMATE


@pytest.mark.parametrize("value", ["12", 12.0, "x", True, float("nan")])
def test_odd_values(value: object) -> None:
    expected = 12 if value in ("12", 12.0) else None
    assert read_row_count({"row_count": value}, "sqlserver").row_count == expected


# --------------------------------------------------------------------------- sizes


def test_a_size_is_data_plus_index_and_none_without_a_data_size() -> None:
    assert read_size_mb({"data_size_mb": 2.5, "index_size_mb": 1.5}, "mysql") == 4.0
    assert read_data_size_mb({"data_size_mb": 2.5, "index_size_mb": 1.5}, "mysql") == 2.5
    # Oracle's script reports no index size: the data size is the table's size.
    assert read_size_mb({"data_size_mb": 2.5, "row_count_basis": ESTIMATE}, "oracle") == 2.5
    assert read_size_mb({"index_size_mb": 1.5}, "mysql") is None
    assert read_size_mb({"data_size_mb": None, "index_size_mb": 1.5}, "postgresql") is None


def test_a_reported_zero_size_is_real_but_an_old_oracle_zero_is_unknown() -> None:
    assert read_size_mb({"data_size_mb": 0, "index_size_mb": 0}, "postgresql") == 0.0
    assert read_size_mb({"data_size_mb": 0, "row_count_basis": None}, "oracle") == 0.0
    # Before #1636 the Oracle script wrote 0 when it found no TABLE segment.
    assert read_size_mb({"data_size_mb": 0}, "oracle") is None
    assert read_data_size_mb({"data_size_mb": 0}, "oracle") is None
    assert read_size_mb({"data_size_mb": 0}, "sqlserver") == 0.0


def test_the_assumption_divides_the_data_size_never_the_indexes() -> None:
    assert assumption_size_mb({"size_mb": 0.13, "data_size_mb": 0.02}) == 0.02
    assert assumption_size_mb({"size_mb": 0.13, "data_size_mb": None}) is None
    # An output written before #1636 records only the total.
    assert assumption_size_mb({"size_mb": 0.13}) == 0.13
    assert assumption_size_mb({"size_mb": None}) is None


def test_the_unknown_size_assumption_line() -> None:
    assert unknown_size_assumption(0) is None
    assert unknown_size_assumption(1) == (
        "1 source table has no collected size, so no storage is priced for it"
    )
    assert unknown_size_assumption(3) == (
        "3 source tables have no collected size, so no storage is priced for them"
    )


# --------------------------------------------------------------------------- facts


def _table(
    tid: str, count: int | None, basis: str | None, at: str | None = None, size: float | None = 1.0
) -> dict:
    return {
        "table_id": tid,
        "row_count": count,
        "row_count_basis": basis,
        "stats_analyzed_at": at,
        "size_mb": size,
        "data_size_mb": size,
    }


def test_row_count_facts_count_each_state_and_state_the_assumption() -> None:
    facts = row_count_facts(
        [
            _table("a", 10, ESTIMATE, "2026-10-01T00:00:00"),
            _table("b", 0, ESTIMATE, "2026-09-01T00:00:00"),
            _table("c", None, None),
            _table("d", 5, EXACT),
            {"row_count": 3},  # no table id: not a collected table
        ]
    )
    assert facts == {
        "table_count": 4,
        "unknown_count": 1,
        "no_size_count": 0,
        "ddl_table_count": 0,
        "exact_count": 1,
        "estimate_count": 2,
        "stats_oldest_at": "2026-09-01T00:00:00",
        "stats_newest_at": "2026-10-01T00:00:00",
        "statement": (
            "1 of 4 source tables have no row count in the source's statistics; "
            + UNKNOWN_ROW_COUNT_ASSUMPTION
        ),
    }


def test_a_table_with_no_size_either_is_stated_as_the_fixed_stand_in() -> None:
    # A DDL-only collection: no count and no size, so no data size to divide.
    facts = row_count_facts(
        [_table("a", None, None, size=None), _table("b", None, None, size=None)]
    )
    assert (facts["unknown_count"], facts["no_size_count"]) == (2, 2)
    assert facts["statement"] == (
        "All 2 source tables have no row count and no collected size; " + NO_SIZE_ASSUMPTION
    )
    assert "0.01 MB stand-in" in NO_SIZE_ASSUMPTION
    assert "over its declared row width" not in str(facts["statement"])


def test_both_cases_are_stated_when_both_occur() -> None:
    facts = row_count_facts(
        [_table("a", None, None), _table("b", None, None, size=None), _table("c", 4, ESTIMATE)]
    )
    assert facts["statement"] == (
        "1 of 3 source tables have no row count in the source's statistics; "
        + UNKNOWN_ROW_COUNT_ASSUMPTION
        + ". 1 of 3 source tables have no row count and no collected size; "
        + NO_SIZE_ASSUMPTION
    )


def test_no_statement_when_every_count_is_known() -> None:
    facts = row_count_facts([_table("a", 0, EXACT), _table("b", 3, EXACT)])
    assert facts["statement"] is None
    assert (facts["unknown_count"], facts["exact_count"]) == (0, 2)


def test_the_statement_when_no_count_is_known() -> None:
    assert str(row_count_facts([_table("a", None, None)])["statement"]).startswith(
        "The source table has"
    )


def test_the_assumption_states_its_method_and_never_calls_a_table_empty() -> None:
    from src.shared.prose_style import find_style_hits

    assert "collected data size divided by its declared row width" in UNKNOWN_ROW_COUNT_ASSUMPTION
    assert UNKNOWN_ROW_COUNT_ASSUMPTION.endswith("not as 0")
    assert not find_style_hits(UNKNOWN_ROW_COUNT_ASSUMPTION)


@pytest.mark.parametrize(
    "phrase",
    [
        "a rough estimate of the rows",
        "the count leans low",
        "the figure leans high",
        "a Rough  Estimate",
    ],
)
def test_the_style_guard_rejects_a_direction_or_looseness_claim(phrase: str) -> None:
    # #1636: the deliverables once said the assumed count was "a rough estimate that
    # leans low"; neither claim held, and the guard keeps both out.
    from src.shared.prose_style import find_style_hits

    assert [h.kind for h in find_style_hits(f"Row counts: {phrase}.")] == ["hedge"]


def test_every_form_of_the_statement_is_plain_prose() -> None:
    # The deliverables' style guard (src.shared.prose_style) holds for each case.
    from src.shared.prose_style import find_style_hits

    statement = row_count_facts(
        [_table("a", None, None), _table("b", None, None, size=None), _table("c", 4, ESTIMATE)]
    )["statement"]
    assert find_style_hits(f"Row counts: {statement}.") == []
    assert find_style_hits(str(unknown_size_assumption(2))) == []


def test_storage_is_the_collected_sizes_and_the_rest_are_counted() -> None:
    from src.shared.row_counts import collected_storage_mb, known_size_mb, unknown_size_line

    tables = [{"size_mb": 2.5}, {"size_mb": 0.0}, {"size_mb": None}, {}, "not a table"]
    assert collected_storage_mb(tables) == (2.5, 2)  # type: ignore[arg-type]
    assert unknown_size_line(tables) == [  # type: ignore[arg-type]
        "2 source tables have no collected size, so no storage is priced for them"
    ]
    assert unknown_size_line([{"size_mb": 1.0}]) == []
    assert (known_size_mb({"size_mb": 0}), known_size_mb({"size_mb": None})) == (0.0, None)
