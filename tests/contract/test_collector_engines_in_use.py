"""``metadata.engines_in_use`` (collector contract 3.2, #949).

The AWS database engines the application already runs today next to its source. Only
these (and the source engine itself) can be retired for a saving; an engine the plan
considered and dropped never can. Optional: a collection written before 3.2 runs only
its source.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import current_version


def _data(**metadata: Any) -> dict[str, Any]:
    return {
        "job_id": "j",
        "metadata": {
            "collection_timestamp": "2026-10-16T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {"engine": "mysql", "version": "8.0.45", "hostname": "h"},
            **metadata,
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 1,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                }
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


def test_a_collection_written_before_3_2_loads_with_none_in_use() -> None:
    contract = CollectorOutputContract(**_data())
    assert contract.contract_version == current_version(CollectorOutputContract)
    assert contract.metadata.engines_in_use == []


def test_engines_in_use_round_trip_lower_cased() -> None:
    contract = CollectorOutputContract(**_data(engines_in_use=["ElastiCache", "documentdb"]))
    dumped = contract.model_dump(mode="json")
    assert dumped["metadata"]["engines_in_use"] == ["elasticache", "documentdb"]


@pytest.mark.parametrize("bad", ["", "not an id", "drop;table"])
def test_an_engine_in_use_is_an_engine_id(bad: str) -> None:
    with pytest.raises(ValidationError):
        CollectorOutputContract(**_data(engines_in_use=[bad]))
