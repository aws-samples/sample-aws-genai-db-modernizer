"""Every analysis cost estimate prices from the one price table (#1196).

Each estimate is checked twice: against the table's own figures (the stale constants it
used before are gone), and with the engine's pricing swapped, to show the estimate moves
with the table rather than with a constant of its own.
"""

from __future__ import annotations

import pytest

from src.agents.load_test import handler as load_test_handler
from src.shared import catalog
from src.shared.catalog.engines import LIST_PRICE_FIELDS
from src.tools.analysis.aurora_common_analysis_tools import estimate_aurora_costs
from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs
from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs
from src.tools.analysis.opensearch_analysis_tools import estimate_opensearch_costs
from src.tools.analysis.redis_analysis_tools import estimate_redis_costs

# One small table, one read a second: every engine sits at its smallest size.
COLLECTION = {
    "database_schema": {"tables": [{"table_id": "t", "size_mb": 100.0, "row_count": 1000}]},
    "queries": {"query_patterns": [{"query_type": "SELECT", "calls_per_second": 1.0}]},
}
ESTIMATES = {
    "aurora_mysql": lambda: estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="mysql"),
    "aurora_postgresql": lambda: estimate_aurora_costs(
        COLLECTION, "us-east-1", None, engine="postgresql"
    ),
    "documentdb": lambda: estimate_documentdb_costs(COLLECTION, "us-east-1"),
    "dynamodb": lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
    "opensearch": lambda: estimate_opensearch_costs(COLLECTION, "us-east-1"),
    "elasticache": lambda: estimate_redis_costs(COLLECTION, "us-east-1", None),
}


def test_aurora_is_priced_on_io_optimized_rates() -> None:
    est = estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="mysql")
    prov = est.cost_components["provisioned"]
    assert prov["instance_type"] == "db.r6g.large"
    assert prov["compute_monthly_usd"] == round(0.338 * 730, 2)
    assert prov["storage_monthly_usd"] == round(20 * 0.225, 2)
    assert est.cost_components["list_prices"] == "aurora_mysql.io_optimized"
    assert "Aurora I/O-Optimized storage: $0.225/GB/month" in est.pricing_assumptions


def test_documentdb_instance_is_the_standard_list_price() -> None:
    est = estimate_documentdb_costs(COLLECTION, "us-east-1")
    assert est.cost_components["instance"] == round(0.26315 * 730, 2)
    assert est.cost_components["list_prices"] == "documentdb.standard"


def test_opensearch_storage_is_gp3() -> None:
    est = estimate_opensearch_costs(COLLECTION, "us-east-1")
    assert est.cost_components["storage_cost_usd"] == round(20.0 * 0.122, 2)
    assert est.cost_components["instance_cost_usd"] == round(2 * 0.167 * 730, 2)


def test_elasticache_picks_a_node_that_exists_at_its_valkey_price() -> None:
    est = estimate_redis_costs(COLLECTION, "us-east-1", None)
    assert est.cost_components["instance_type"] == "cache.m7g.large"
    assert est.cost_components["primary_node"] == round(0.1264 * 730, 2)
    assert est.cost_components["instance_type"] in catalog.pricing("elasticache").instance_hourly


def test_the_load_test_prices_dynamodb_request_units_from_the_table() -> None:
    assert load_test_handler._request_unit_prices("dynamodb") == (0.125 / 1e6, 0.625 / 1e6)


@pytest.mark.parametrize("engine", sorted(ESTIMATES))
def test_an_estimate_moves_with_the_table(engine: str, monkeypatch) -> None:
    """Doubling every list price of the engine doubles everything it bills (the
    ElastiCache cross-AZ transfer aside, which is not an engine price)."""
    before = ESTIMATES[engine]().monthly_cost_usd
    real = catalog.pricing

    def doubled(eng: str):
        p = real(eng)
        if eng != engine:
            return p
        changes = {
            f: (
                {k: v * 2 for k, v in getattr(p, f).items()}
                if isinstance(getattr(p, f), dict)
                else getattr(p, f) * 2
            )
            for f in LIST_PRICE_FIELDS
            if getattr(p, f) not in (None, {}) and f != "instance_memory_gib"
        }
        return p.model_copy(update=changes)

    monkeypatch.setattr(catalog, "pricing", doubled)
    after = ESTIMATES[engine]().monthly_cost_usd
    assert after == pytest.approx(2 * before, abs=0.02), (before, after)


def test_every_estimate_bills_one_730_hour_month() -> None:
    """One month for every estimate (#1196): ``HOURS_PER_MONTH`` for capacity and
    ``SECONDS_PER_MONTH`` for request volumes, never a 30-day month of its own."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "src" / "tools" / "analysis"
    for name in (
        "aurora_common_analysis_tools.py",
        "documentdb_analysis_tools.py",
        "dynamodb_analysis_tools.py",
        "opensearch_analysis_tools.py",
        "redis_analysis_tools.py",
    ):
        text = (root / name).read_text(encoding="utf-8")
        for needle in ("24 * 30", "86400 * 30", "* 730", "= 730"):
            assert needle not in text, f"{name} bills its own month: {needle!r}"
        assert "HOURS_PER_MONTH" in text or "SECONDS_PER_MONTH" in text, name
