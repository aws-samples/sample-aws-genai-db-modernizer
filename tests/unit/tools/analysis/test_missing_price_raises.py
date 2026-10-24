"""A price missing from the price table raises, naming the engine and the price (#1395).

An estimate never prices a gap at $0: a hole in ``src/catalog/prices.toml`` is found by a
test or a run, not by a reader of a report. A price that is free is an explicit ``0.0``
in the table (``documentdb.io_optimized`` I/O), which stays valid.
"""

from __future__ import annotations

import pytest

from src.agents.load_test import handler as load_test_handler
from src.shared import catalog
from src.tools.analysis.aurora_common_analysis_tools import estimate_aurora_costs
from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs
from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs
from src.tools.analysis.opensearch_analysis_tools import estimate_opensearch_costs
from src.tools.analysis.redis_analysis_tools import estimate_redis_costs

COLLECTION = {
    "database_schema": {"tables": [{"table_id": "t", "size_mb": 100.0, "row_count": 1000}]},
    "queries": {"query_patterns": [{"query_type": "SELECT", "calls_per_second": 1.0}]},
}

# (catalog engine id, estimator, the price that goes missing)
CASES = [
    (
        "aurora_mysql",
        lambda: estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="mysql"),
        "acu_price_per_hour",
    ),
    (
        "aurora_mysql",
        lambda: estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="mysql"),
        "storage_price_per_gb_month",
    ),
    (
        "aurora_postgresql",
        lambda: estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="postgresql"),
        "acu_price_per_hour",
    ),
    (
        "aurora_postgresql",
        lambda: estimate_aurora_costs(COLLECTION, "us-east-1", None, engine="postgresql"),
        "storage_price_per_gb_month",
    ),
    (
        "documentdb",
        lambda: estimate_documentdb_costs(COLLECTION, "us-east-1"),
        "read_unit_price_per_million",
    ),
    (
        "documentdb",
        lambda: estimate_documentdb_costs(COLLECTION, "us-east-1"),
        "storage_price_per_gb_month",
    ),
    (
        "dynamodb",
        lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
        "read_unit_price_per_million",
    ),
    (
        "dynamodb",
        lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
        "write_unit_price_per_million",
    ),
    (
        "dynamodb",
        lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
        "storage_price_per_gb_month",
    ),
    # #1476: the request units are sized by the engine file's unit sizes.
    (
        "dynamodb",
        lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
        "read_unit_bytes",
    ),
    (
        "dynamodb",
        lambda: estimate_dynamodb_costs(COLLECTION, "us-east-1"),
        "write_unit_bytes",
    ),
    (
        "opensearch",
        lambda: estimate_opensearch_costs(COLLECTION, "us-east-1"),
        "storage_price_per_gb_month",
    ),
    (
        "elasticache",
        lambda: estimate_redis_costs(COLLECTION, "us-east-1", None),
        "backup_price_per_gb_month",
    ),
    (
        "elasticache",
        lambda: estimate_redis_costs(COLLECTION, "us-east-1", None),
        "cross_az_transfer_price_per_gb",
    ),
    (
        "dynamodb",
        lambda: load_test_handler._request_unit_prices("dynamodb"),
        "read_unit_price_per_million",
    ),
    (
        "dynamodb",
        lambda: load_test_handler._request_unit_prices("dynamodb"),
        "write_unit_price_per_million",
    ),
]


@pytest.mark.parametrize(("engine", "estimate", "price"), CASES)
def test_a_missing_price_raises_naming_the_engine_and_the_price(
    engine: str, estimate, price: str, monkeypatch
) -> None:
    real = catalog.pricing

    def without(eng: str):
        p = real(eng)
        return p.model_copy(update={price: None}) if eng == engine else p

    monkeypatch.setattr(catalog, "pricing", without)
    with pytest.raises(ValueError) as exc:
        estimate()
    assert engine in str(exc.value)
    assert price in str(exc.value)


@pytest.mark.passes_on_main
def test_an_explicit_zero_price_is_a_price(monkeypatch) -> None:
    """DocumentDB I/O-Optimized bills no request fee: a written 0.0, not a missing price."""
    real = catalog.pricing
    monkeypatch.setattr(
        catalog,
        "pricing",
        lambda eng: (
            real(eng).model_copy(update={"read_unit_price_per_million": 0.0})
            if eng == "documentdb"
            else real(eng)
        ),
    )
    est = estimate_documentdb_costs(COLLECTION, "us-east-1")
    assert est.cost_components["io_requests"] == 0.0
