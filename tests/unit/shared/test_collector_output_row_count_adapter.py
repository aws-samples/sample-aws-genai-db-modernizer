# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An older collector output's row counts are read again by the per-engine rule (#1636).

CollectorOutputContract's MAJOR bump for #1636 lets ``Table.row_count`` be None. An
output written before it carries 0 for every table its source had no count for, so
the MAJOR 4 adapter reads each count again: a 0 from a source that cannot tell an
empty table from an unknown one becomes None, SQL Server's exact 0 stays 0, and each
known count gains its basis. The MAJOR 3 adapter (#387) ends in it, so a 3.x output
gets both. Until the bump is resolved the stored file is the current MAJOR and is
read as it is; these tests call the adapters the resolved bump reads it through.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import (
    _ADAPTERS,
    _collector_output_from_3,
    _collector_output_from_4,
)

_KEY = "src.contracts.collector_output:CollectorOutputContract"


def _table(tid: str, row_count: int, size_mb: float = 0.5, **extra: Any) -> dict[str, Any]:
    return {
        "table_id": tid,
        "table_name": tid.split(".")[-1],
        "schema_name": "shop",
        "row_count": row_count,
        "size_mb": size_mb,
        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
        **extra,
    }


def _old_output(engine: str, tables: list[dict[str, Any]]) -> dict[str, Any]:
    """A collector output in the shape 4.1 wrote: no row_count_basis on any table."""
    return {
        "contract_version": "4.1",
        "job_id": "job-old",
        "metadata": {
            "collection_timestamp": "2026-10-20T00:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {"engine": engine, "version": "x", "hostname": "h"},
        },
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


def _counts(out: dict[str, Any]) -> dict[str, tuple]:
    return {
        t["table_id"]: (t["row_count"], t["row_count_basis"])
        for t in out["database_schema"]["tables"]
    }


def test_the_adapters_are_registered_for_majors_3_and_4() -> None:
    assert _ADAPTERS[_KEY][4] is _collector_output_from_4
    assert _ADAPTERS[_KEY][3] is _collector_output_from_3


@pytest.mark.parametrize(
    ("engine", "basis"), [("postgresql", "estimate"), ("oracle", "estimate"), ("", None)]
)
def test_an_old_zero_from_a_source_that_cannot_tell_becomes_unknown(
    engine: str, basis: str | None
) -> None:
    out = _collector_output_from_4(
        _old_output(engine, [_table("shop.empty_or_not", 0), _table("shop.orders", 1200)])
    )
    assert _counts(out) == {
        "shop.empty_or_not": (None, None),
        "shop.orders": (1200, basis),
    }


def test_an_old_sql_server_zero_stays_an_empty_table() -> None:
    out = _collector_output_from_4(
        _old_output("sqlserver", [_table("dbo.empty", 0), _table("dbo.orders", 9)])
    )
    assert _counts(out) == {"dbo.empty": (0, "estimate"), "dbo.orders": (9, "estimate")}


@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_an_old_mysql_family_zero_stays_the_servers_figure(engine: str) -> None:
    # TABLE_ROWS is never a coalesced NULL for a base table, and a 4.x output holds only
    # the total size, so its 0 is kept: exact for MyISAM, an estimate for InnoDB.
    out = _collector_output_from_4(
        _old_output(
            engine,
            [
                _table("shop.log", 0, storage_engine="MyISAM"),
                _table("shop.cart", 0, 0.13, storage_engine="InnoDB"),
            ],
        )
    )
    assert _counts(out) == {"shop.log": (0, "exact"), "shop.cart": (0, "estimate")}


@pytest.mark.parametrize("version", ["16.10", "14.2", "13.4", None])
def test_an_old_postgresql_zero_is_unknown_whatever_the_servers_version(
    version: str | None,
) -> None:
    # The 4.x collector clamped every count with max(row_count or 0, 0), 14+'s -1 too,
    # so no PostgreSQL 0 in a collector output is known to be real (a raw collection's
    # -1 survives, and keeps the version-based rule).
    old = _old_output("postgresql", [_table("shop.t", 0), _table("shop.u", 7)])
    old["metadata"]["source_database"]["version"] = version
    assert _counts(_collector_output_from_4(old)) == {
        "shop.t": (None, None),
        "shop.u": (7, "estimate"),
    }


def test_an_old_oracle_zero_size_becomes_unknown() -> None:
    out = _collector_output_from_4(
        _old_output("oracle", [_table("hr.part", 50, 0.0), _table("hr.emp", 10, 2.0)])
    )
    sizes = {t["table_id"]: t["size_mb"] for t in out["database_schema"]["tables"]}
    assert sizes == {"hr.part": None, "hr.emp": 2.0}


def test_a_table_written_since_1636_is_kept_as_it_is() -> None:
    table = _table("shop.empty", 0, row_count_basis="estimate")
    out = _collector_output_from_4(_old_output("postgresql", [table]))
    assert out["database_schema"]["tables"] == [table]


def test_the_stored_payload_is_not_changed_and_the_result_loads() -> None:
    old = _old_output("postgresql", [_table("shop.t", 0)])
    before = copy.deepcopy(old)
    out = _collector_output_from_4(old)
    assert old == before
    loaded = CollectorOutputContract.model_validate(out)
    assert loaded.database_schema.tables[0].row_count is None


def test_a_3x_output_gets_both_adapters() -> None:
    old = _old_output("oracle", [_table("hr.t", 0, sample_data=[{"id": 1}])])
    old["contract_version"] = "3.6"
    out = _collector_output_from_3(old)
    [table] = out["database_schema"]["tables"]
    assert "sample_data" not in table
    assert (table["row_count"], table["row_count_basis"]) == (None, None)


def test_an_output_with_no_tables_passes_through() -> None:
    old = _old_output("postgresql", [])
    old["database_schema"] = {}
    assert _collector_output_from_4(old) == old


@pytest.mark.parametrize("engine", ["postgresql", "oracle", ""])
def test_an_old_ddl_uploads_tables_keep_their_routing(engine: str) -> None:
    # A 4.x DDL upload wrote 0 rows and 0 MB for every table. Read again, the count is
    # unknown, and the table is marked as read from DDL alone, so routing still treats
    # it as small (the maintainer's interim rule on #1637).
    from src.shared.row_counts import is_unmeasured_ddl_table

    out = _collector_output_from_4(
        _old_output(engine, [_table("shop.settings", 0, 0.0), _table("shop.orders", 0, 2.0)])
    )
    settings, orders = out["database_schema"]["tables"]
    assert (settings["row_count"], settings["size_mb"], settings["from_ddl"]) == (None, None, True)
    assert is_unmeasured_ddl_table(settings)
    assert "from_ddl" not in orders and orders["size_mb"] == 2.0
