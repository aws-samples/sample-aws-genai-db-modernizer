"""No customer row data is kept: sample rows are dropped and counted (#387).

The collectors no longer read table rows. A collection or collector output written
before #387 may still carry ``sample_data`` on its tables; every reader drops it and
says how many rows it dropped, and an old saved collector output still loads.
"""

from __future__ import annotations

import copy
from typing import Any

from src.contracts.collector_output import CollectorOutputContract, Table
from src.shared.contract_versioning import load_contract
from src.shared.sample_rows import collector_output_without_sample_rows, without_sample_rows


def _old_collector_output() -> dict[str, Any]:
    """A collector output as a live collector wrote it before #387 (contract 3.x)."""
    return {
        "contract_version": "3.6",
        "job_id": "j",
        "metadata": {
            "collection_timestamp": "2026-10-16T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {"engine": "mysql", "version": "8.0.45", "hostname": "h"},
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 2,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                    "sample_data": [{"id": 1, "email": "a@example.com"}, {"id": 2}],
                },
                {
                    "table_id": "shop.users",
                    "table_name": "users",
                    "row_count": 1,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                    "sample_data": [{"id": 7}],
                },
                {
                    "table_id": "shop.audit",
                    "table_name": "audit",
                    "row_count": 0,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                    "sample_data": None,
                },
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


def test_the_table_contract_has_no_sample_rows() -> None:
    assert "sample_data" not in Table.model_fields


def test_without_sample_rows_drops_the_key_and_counts_the_rows() -> None:
    tables = _old_collector_output()["database_schema"]["tables"]
    before = copy.deepcopy(tables)
    kept, dropped = without_sample_rows(tables)
    assert dropped == 3
    assert all("sample_data" not in t for t in kept)
    assert [t["table_id"] for t in kept] == ["shop.orders", "shop.users", "shop.audit"]
    assert tables == before  # the input is not changed


def test_without_sample_rows_on_tables_without_any() -> None:
    kept, dropped = without_sample_rows([{"table_name": "t"}])
    assert (kept, dropped) == ([{"table_name": "t"}], 0)
    assert without_sample_rows(None) == ([], 0)
    assert without_sample_rows([]) == ([], 0)


def test_a_non_list_sample_value_is_dropped_and_counts_nothing() -> None:
    kept, dropped = without_sample_rows([{"table_name": "t", "sample_data": "x"}, "not a table"])
    assert kept == [{"table_name": "t"}, "not a table"]
    assert dropped == 0


def test_collector_output_without_sample_rows() -> None:
    payload = _old_collector_output()
    before = copy.deepcopy(payload)
    out, dropped = collector_output_without_sample_rows(payload)
    assert dropped == 3
    assert all("sample_data" not in t for t in out["database_schema"]["tables"])
    assert out["queries"] == payload["queries"]
    assert payload == before


def test_collector_output_without_a_schema_is_returned_as_is() -> None:
    out, dropped = collector_output_without_sample_rows({"job_id": "j"})
    assert (out, dropped) == ({"job_id": "j"}, 0)


def test_an_old_collector_output_with_sample_rows_still_loads_without_them() -> None:
    loaded = load_contract(
        CollectorOutputContract, _old_collector_output(), artifact="old/collector/output.json"
    )
    dumped = loaded.model_dump(mode="json")
    assert [t["table_id"] for t in dumped["database_schema"]["tables"]] == [
        "shop.orders",
        "shop.users",
        "shop.audit",
    ]
    assert all("sample_data" not in t for t in dumped["database_schema"]["tables"])
    assert "a@example.com" not in str(dumped)


def test_the_major_3_adapter_drops_rows_and_logs_the_count(caplog) -> None:
    """Once the #387 MAJOR bump is resolved, a stored 3.x collector output is read
    through this adapter; until then the model ignores the field it no longer has."""
    from src.shared.contract_versioning import _ADAPTERS, _collector_output_from_3

    assert (
        _ADAPTERS["src.contracts.collector_output:CollectorOutputContract"][3]
        is _collector_output_from_3
    )
    # Debug, not a warning: a job reads its collector output many times.
    with caplog.at_level("DEBUG", logger="src.shared.contract_versioning"):
        out = _collector_output_from_3(_old_collector_output())
    assert all("sample_data" not in t for t in out["database_schema"]["tables"])
    assert "dropped 3 sample row(s)" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_raw_collection_without_sample_rows() -> None:
    from src.shared.sample_rows import raw_collection_without_sample_rows

    raw: dict[str, Any] = {
        "metadata": {},
        "tables": [{"table_name": "t", "sample_data": [{"id": 1}, {"id": 2}]}],
    }
    out, dropped = raw_collection_without_sample_rows(raw)
    assert (out["tables"], dropped) == ([{"table_name": "t"}], 2)
    assert raw["tables"][0]["sample_data"] == [{"id": 1}, {"id": 2}]
    assert raw_collection_without_sample_rows({"metadata": {}}) == ({"metadata": {}}, 0)
