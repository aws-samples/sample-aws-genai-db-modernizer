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
