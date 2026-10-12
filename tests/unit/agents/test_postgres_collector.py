"""Unit tests for the PostgreSQL collector's native type map."""

from src.agents.collector.postgres_collector import _PG_TYPE_MAP
from src.contracts.collector_output import NormalizedDataType


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
