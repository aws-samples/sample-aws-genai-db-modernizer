# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the PostgreSQL collector's native type map and index methods."""

from src.agents.collector.postgres_collector import _PG_TYPE_MAP, _build_tables
from src.contracts.collector_output import IndexType, NormalizedDataType


class TestTypeMapExactVsApproximateNumerics:
    """NUMERIC and MONEY carry real precision/scale (or a fixed implicit
    scale), so they stay exact numerics. REAL/DOUBLE PRECISION are
    approximate and must never resolve through the exact-numeric path, which
    would reach a target NUMERIC(n,0) and truncate every fractional value
    (#156)."""

    def test_numeric_and_money_are_exact(self):
        assert _PG_TYPE_MAP["numeric"] == NormalizedDataType.decimal
        assert _PG_TYPE_MAP["money"] == NormalizedDataType.decimal

    def test_real_and_double_precision_are_approximate(self):
        assert _PG_TYPE_MAP["real"] == NormalizedDataType.floating_point
        assert _PG_TYPE_MAP["double precision"] == NormalizedDataType.floating_point


def test_live_index_methods_reach_the_contract_or_fall_back_to_other():
    """#293: the live collector passes ``am.amname`` through the same normalizer as the
    offline path, so BRIN loads as itself and an extension method does not crash."""
    raw = {
        "tables": [
            {
                "table_name": "t",
                "columns": [{"column_name": "id", "data_type": "bigint"}],
                "indexes": [
                    {
                        "index_name": "i_brin",
                        "columns": ["id"],
                        "is_unique": False,
                        "index_type": "brin",
                    },
                    {
                        "index_name": "i_bloom",
                        "columns": ["id"],
                        "is_unique": False,
                        "index_type": "bloom",
                    },
                ],
            }
        ]
    }

    [table] = _build_tables(raw)

    assert [i.index_type for i in table.indexes] == [IndexType.brin, IndexType.other]


def test_datetime_precision_flows_from_the_query_row_to_the_column():
    """#292: ``datetime_precision`` is read like ``numeric_precision``; an older
    collection without the key loads as not reported."""
    raw = {
        "tables": [
            {
                "table_name": "t",
                "columns": [
                    {"column_name": "a", "data_type": "timestamp", "datetime_precision": 3},
                    {"column_name": "b", "data_type": "timestamp", "datetime_precision": None},
                    {"column_name": "c", "data_type": "timestamp"},
                ],
            }
        ]
    }

    cols = _build_tables(raw)[0].columns

    assert [c.datetime_precision for c in cols] == [3, None, None]


def test_both_postgres_collector_queries_select_datetime_precision():
    from pathlib import Path
    from unittest.mock import MagicMock

    from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

    script = Path(__file__).resolve().parents[3] / "scripts" / "collect-postgresql.sql"
    assert "datetime_precision" in script.read_text()

    c = PostgreSQLRemoteCollector(
        ssm=MagicMock(), host="h", port=1, database="d", secret_arn="s"
    )  # pragma: allowlist secret
    seen: list[str] = []
    c._query = lambda q: seen.append(q) or []  # type: ignore[method-assign]
    c.collect_columns("t")
    assert "datetime_precision" in seen[0]
