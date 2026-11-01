# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB design's GSIs priced from the design (#805): storage and the writes they add."""

from __future__ import annotations

import copy

import pytest

from src.agents.referee.gsi_cost import (
    gsi_cost_facts,
    gsi_costs_by_engine,
    tco_assumption,
)
from src.shared import catalog
from src.shared.catalog.prices import SECONDS_PER_MONTH

PRICES = catalog.pricing("dynamodb")
STORAGE = PRICES.storage_price_per_gb_month
WRITE = PRICES.write_unit_price_per_million
GB = 1024**3
OVERHEAD = PRICES.index_item_overhead_bytes


def _cost(design: dict | None, collector: dict, query_ids: list[str] | None = None) -> dict | None:
    return gsi_cost_facts(design, collector, query_ids, "dynamodb")


def _attrs(*names: str) -> list[dict]:
    return [{"name": n, "type": "S"} for n in names]


def _design() -> dict:
    return {
        "table_definitions": [
            {
                "table_name": "Comments",
                "aggregate_pattern": "separate",
                "source_tables": ["app.comments"],
                "partition_key": {"attribute_name": "comment_id", "attribute_type": "S"},
                "attributes": _attrs("comment_id", "post_id", "body"),
                "gsis": [
                    {
                        "gsi_name": "ByPost",
                        "partition_key": [{"attribute_name": "post_id", "attribute_type": "S"}],
                        "projection": "ALL",
                        "item_count": 2_000_000,
                        "item_size_bytes": 1500,
                    }
                ],
                "item_count": 2_000_000,
                "item_size_bytes": 1500,
            },
            {
                "table_name": "Posts",
                "aggregate_pattern": "separate",
                "source_tables": ["app.posts"],
                "partition_key": {"attribute_name": "id", "attribute_type": "S"},
                "attributes": _attrs("id", "title"),
                "gsis": [],
                "item_count": 10,
                "item_size_bytes": 100,
            },
        ],
        "access_patterns": [
            _write("W-1", "PutItem", "Comments", ["app.comments"], ["q-put"]),
            _write("W-2", "PutItem", "Posts", ["app.posts"], ["q-post"]),
        ],
    }


def _write(pid: str, op: str, table: str, sources: list[str], qids: list[str]) -> dict:
    # design_rps carries the peak and growth factors: the cost never reads it.
    return {
        "pattern_id": pid,
        "operation": op,
        "table_name": table,
        "source_tables": sources,
        "query_ids": qids,
        "design_rps": 9999.0,
        "item_size_bytes": 1500,
    }


def _collector(**rates: float) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": q, "query_type": "INSERT", "calls_per_second": cps}
                for q, cps in rates.items()
            ]
        }
    }


def test_storage_is_item_count_times_projected_size() -> None:
    cost = _cost(_design(), _collector())
    assert cost is not None
    gb = 2_000_000 * (1500 + 100) / GB  # plus DynamoDB's 100 bytes per index item
    assert cost["gsi_count"] == 1
    assert cost["storage_gb"] == round(gb, 4)
    assert cost["storage_cost_usd"] == round(gb * STORAGE, 2)
    assert cost["write_cost_usd"] == 0  # no collected write calls


def test_each_index_item_carries_100_bytes_of_overhead() -> None:
    """The DynamoDB Developer Guide's GSI storage rule: 100 bytes per index item on top
    of the projected attributes, so an index of tiny items still costs its overhead."""
    design = _design()
    design["table_definitions"][0]["gsis"][0].update(item_count=10_000_000, item_size_bytes=0)
    cost = _cost(design, _collector())
    assert cost is not None
    assert OVERHEAD == 100  # the engine file's, the Developer Guide's figure
    assert cost["storage_gb"] == round(10_000_000 * 100 / GB, 4)
    assert cost["storage_cost_usd"] == round(10_000_000 * 100 / GB * STORAGE, 2) > 0
    design["table_definitions"][0]["gsis"][0]["item_count"] = 0
    assert _cost(design, _collector())["storage_cost_usd"] == 0  # type: ignore[index]


def test_a_write_that_sets_the_keys_adds_one_write_per_item_at_average_load() -> None:
    cost = _cost(_design(), _collector(**{"q-put": 2.0, "q-post": 50.0}))
    assert cost is not None
    # 1500 bytes is two 1 KB write units; 2 calls a second, never design_rps.
    units = 2.0 * 2 * SECONDS_PER_MONTH
    assert cost["write_units_per_month"] == round(units, 2)
    assert cost["write_cost_usd"] == round(units / 1_000_000 * WRITE, 2)
    assert cost["total_monthly_cost_usd"] == round(
        cost["storage_cost_usd"] + cost["write_cost_usd"], 2
    )
    [index] = cost["indexes"]
    assert index["write_query_ids"] == ["q-put"]  # the Posts write touches no GSI


def test_each_item_a_call_changes_is_one_index_write() -> None:
    collector = _collector(**{"q-put": 2.0})
    collector["queries"]["query_patterns"][0]["rows_affected_avg"] = 3
    cost = _cost(_design(), collector)
    assert cost is not None
    assert cost["write_units_per_month"] == round(2.0 * 3 * 2 * SECONDS_PER_MONTH, 2)


@pytest.mark.parametrize("op", ["DeleteItem", "BatchWriteItem"])
def test_every_write_operation_writes_the_index(op: str) -> None:
    design = _design()
    design["access_patterns"][0]["operation"] = op
    cost = _cost(design, _collector(**{"q-put": 1.0}))
    assert cost is not None
    assert cost["write_units_per_month"] == round(2 * SECONDS_PER_MONTH, 2)


def test_a_transaction_writes_its_indexes_at_the_standard_rate() -> None:
    plain = _cost(_design(), _collector(**{"q-put": 1.0}))
    design = _design()
    design["access_patterns"][0]["operation"] = "TransactWriteItems"
    txn = _cost(design, _collector(**{"q-put": 1.0}))
    assert plain is not None and txn is not None
    assert txn["write_units_per_month"] == plain["write_units_per_month"] > 0


def test_reads_add_nothing() -> None:
    design = _design()
    design["access_patterns"].append(
        {**_write("R-1", "Query", "Comments", ["app.comments"], ["q-read"]), "gsi_name": "ByPost"}
    )
    cost = _cost(design, _collector(**{"q-read": 100.0}))
    assert cost is not None
    assert cost["write_units_per_month"] == 0


def test_only_the_engines_in_scope_queries_are_counted() -> None:
    collector = _collector(**{"q-put": 1.0})
    assert _cost(_design(), collector, query_ids=[])["write_cost_usd"] == 0  # type: ignore[index]
    assert _cost(_design(), collector, query_ids=["q-put"])["write_cost_usd"] > 0  # type: ignore[index]
    design = _design()
    design["access_patterns"][0]["in_scope"] = False
    assert _cost(design, collector)["write_cost_usd"] == 0  # type: ignore[index]


def test_a_query_in_two_writes_on_the_table_is_counted_once() -> None:
    design = _design()
    design["access_patterns"].append(
        _write("W-3", "BatchWriteItem", "Comments", ["app.comments"], ["q-put"])
    )
    once = _cost(_design(), _collector(**{"q-put": 1.0}))
    twice = _cost(design, _collector(**{"q-put": 1.0}))
    assert once is not None and twice is not None
    assert twice["write_units_per_month"] == once["write_units_per_month"]


def _collection() -> dict:
    """An item collection whose GSI keys only the comment entity defines."""
    design = _design()
    design["table_definitions"][0] = {
        "table_name": "Content",
        "aggregate_pattern": "item_collection",
        "source_tables": ["app.posts", "app.comments"],
        "partition_key": {"attribute_name": "PK", "attribute_type": "S"},
        "sort_key": {"attribute_name": "SK", "attribute_type": "S"},
        "entities": [
            {"entity_type": "POST", "source_table": "app.posts", "attributes": _attrs("title")},
            {
                "entity_type": "COMMENT",
                "source_table": "app.comments",
                "attributes": _attrs("post_id", "body", "flagged"),
            },
        ],
        "gsis": [
            {
                "gsi_name": "ByPost",
                "partition_key": [{"attribute_name": "post_id", "attribute_type": "S"}],
                "projection": "KEYS_ONLY",
                "item_count": 100,
                "item_size_bytes": 80,
            }
        ],
        "item_count": 200,
        "item_size_bytes": 500,
    }
    design["access_patterns"] = [
        _write("W-1", "PutItem", "Content", ["comments"], ["q-comment"]),
        _write("W-2", "PutItem", "Content", ["app.posts"], ["q-post"]),
    ]
    return design


def test_a_write_whose_entity_lacks_the_index_keys_adds_no_index_write() -> None:
    cost = _cost(_collection(), _collector(**{"q-comment": 1.0, "q-post": 7.0}))
    assert cost is not None
    [index] = cost["indexes"]
    assert index["write_query_ids"] == ["q-comment"]
    assert cost["write_units_per_month"] == round(1 * SECONDS_PER_MONTH, 2)  # 80 B: one unit


def test_a_sparse_index_is_written_only_by_entities_that_carry_its_attribute() -> None:
    design = _collection()
    design["table_definitions"][0]["gsis"][0]["partition_key"] = [
        {"attribute_name": "title", "attribute_type": "S"}
    ]
    design["table_definitions"][0]["gsis"][0]["sparse_attribute"] = "flagged"
    cost = _cost(design, _collector(**{"q-comment": 1.0, "q-post": 7.0}))
    assert cost is not None
    assert cost["indexes"][0]["write_query_ids"] == []


def test_an_overloaded_key_no_entity_defines_is_taken_as_set_by_every_write() -> None:
    design = _collection()
    design["table_definitions"][0]["gsis"][0]["partition_key"] = [
        {"attribute_name": "GSI1PK", "attribute_type": "S"}
    ]
    cost = _cost(design, _collector(**{"q-comment": 1.0, "q-post": 7.0}))
    assert cost is not None
    assert cost["indexes"][0]["write_query_ids"] == ["q-comment", "q-post"]


def test_a_design_without_gsis_has_no_cost() -> None:
    design = _design()
    design["table_definitions"][0]["gsis"] = []
    assert _cost(design, _collector(**{"q-put": 1.0})) is None
    assert _cost(None, _collector()) is None
    assert gsi_costs_by_engine({"dynamodb": None}, _collector(), None) == {}


def test_only_the_dynamodb_design_is_priced() -> None:
    found = gsi_costs_by_engine(
        {"dynamodb": _design(), "documentdb": copy.deepcopy(_design())}, _collector(), None
    )
    assert list(found) == ["dynamodb"]


def test_the_engines_query_ids_decide_the_queries() -> None:
    collector = _collector(**{"q-put": 1.0})
    found = gsi_costs_by_engine({"dynamodb": _design()}, collector, {"dynamodb": []})
    assert found["dynamodb"]["write_cost_usd"] == 0
    found = gsi_costs_by_engine({"dynamodb": _design()}, collector, {"dynamodb": ["q-put"]})
    assert found["dynamodb"]["write_cost_usd"] > 0


def test_the_assumption_line_states_the_share_and_what_it_counts() -> None:
    cost = _cost(_design(), _collector(**{"q-put": 2.0}))
    assert cost is not None
    line = tco_assumption(cost)
    assert f"${cost['total_monthly_cost_usd']:,.2f}/mo" in line
    assert f"${cost['storage_cost_usd']:,.2f} of storage" in line
    assert f"${cost['write_cost_usd']:,.2f} of index writes" in line
    assert "1 global secondary index " in line
    assert "100 bytes of index overhead per item" in line
    assert f"{PRICES.write_unit_bytes // 1024} KB" in line
    assert "reads are not added" in line and "free tier" in line
    assert ";" not in line  # the reports join assumptions with "; "


def _update(projection: str, sql: str | None, **gsi: object) -> dict | None:
    design = _design()
    design["access_patterns"][0]["operation"] = "UpdateItem"
    design["table_definitions"][0]["gsis"][0].update(projection=projection, **gsi)
    collector = _collector(**{"q-put": 1.0})
    collector["metadata"] = {"source_database": {"engine": "mysql"}}
    if sql is not None:
        collector["queries"]["query_patterns"][0].update(query_type="UPDATE", query_text=sql)
    cost = _cost(design, collector)
    assert cost is not None
    return cost


@pytest.mark.parametrize(
    ("projection", "sql", "written", "gsi"),
    [
        ("KEYS_ONLY", "UPDATE comments SET body = ? WHERE comment_id = ?", False, {}),
        ("KEYS_ONLY", "UPDATE comments SET post_id = ? WHERE comment_id = ?", True, {}),
        (
            "INCLUDE",
            "UPDATE comments SET body = ? WHERE comment_id = ?",
            True,
            {"projected_attributes": ["body"]},
        ),
        (
            "INCLUDE",
            "UPDATE comments SET body = ? WHERE comment_id = ?",
            False,
            {"projected_attributes": ["comment_id"]},
        ),
        ("ALL", "UPDATE comments SET body = ? WHERE comment_id = ?", True, {}),
        # The statement names no column: counted on every index its item is in.
        ("KEYS_ONLY", "not sql at all (", True, {}),
        ("KEYS_ONLY", None, True, {}),
    ],
)
def test_an_update_writes_the_index_only_when_it_changes_what_the_index_holds(
    projection: str, sql: str | None, written: bool, gsi: dict
) -> None:
    cost = _update(projection, sql, **gsi)
    assert cost is not None
    assert (cost["indexes"][0]["write_query_ids"] == ["q-put"]) is written


def test_an_updated_column_maps_to_its_attribute_by_source_column() -> None:
    design = _design()
    table = design["table_definitions"][0]
    table["attributes"] = [
        {"name": "comment_id", "type": "S", "source_column": "comment_id"},
        {"name": "PostId", "type": "S", "source_column": "post_id"},
        {"name": "body", "type": "S", "source_column": "body"},
    ]
    table["gsis"][0].update(
        projection="KEYS_ONLY",
        partition_key=[{"attribute_name": "PostId", "attribute_type": "S"}],
    )
    design["access_patterns"][0]["operation"] = "UpdateItem"
    collector = _collector(**{"q-put": 1.0})
    collector["queries"]["query_patterns"][0].update(
        query_type="UPDATE", query_text="UPDATE comments SET post_id = ? WHERE comment_id = ?"
    )
    cost = _cost(design, collector)
    assert cost is not None
    assert cost["indexes"][0]["write_query_ids"] == ["q-put"]


def _orders(projection: str = "KEYS_ONLY", **gsi: object) -> dict:
    """An Orders table with an index keyed on ``status``, and one update of it."""
    return {
        "table_definitions": [
            {
                "table_name": "Orders",
                "aggregate_pattern": "separate",
                "source_tables": ["orders"],
                "partition_key": {"attribute_name": "order_id", "attribute_type": "S"},
                "attributes": _attrs("order_id", "status", "flag", "name", "email"),
                "gsis": [
                    {
                        "gsi_name": "ByStatus",
                        "partition_key": [{"attribute_name": "status", "attribute_type": "S"}],
                        "projection": projection,
                        "item_count": 0,
                        "item_size_bytes": 50,
                        **gsi,
                    }
                ],
                "item_count": 0,
                "item_size_bytes": 200,
            }
        ],
        "access_patterns": [_write("W-1", "UpdateItem", "Orders", ["orders"], ["q-upd"])],
    }


def _sql(sql: str, engine: str = "postgresql", cps: float = 1.0, **query: object) -> dict:
    return {
        "metadata": {"source_database": {"engine": engine}},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q-upd",
                    "query_type": "UPDATE",
                    "query_text": sql,
                    "calls_per_second": cps,
                    **query,
                }
            ]
        },
    }


def test_an_update_that_changes_an_index_key_is_two_index_writes() -> None:
    """The Developer Guide: changing an indexed key deletes the old entry and puts the
    new one. The probe: 1 call/s on a KEYS_ONLY index keyed on status."""
    cost = _cost(_orders(), _sql("UPDATE orders SET status = $1 WHERE order_id = $2"))
    assert cost is not None
    assert cost["write_units_per_month"] == 2 * SECONDS_PER_MONTH == 5_256_000
    assert cost["indexes"][0]["writes_per_item"] == {"q-upd": 2}


@pytest.mark.parametrize(
    ("sql", "writes"),
    [
        ("UPDATE orders SET name = $1 WHERE order_id = $2", 1),  # projected only
        ("UPDATE orders SET flag = $1 WHERE order_id = $2", 0),  # neither
        (
            "INSERT INTO orders (order_id) VALUES ($1) ON CONFLICT (order_id) DO UPDATE SET status = 'x'",
            2,
        ),
        ("not sql at all (", 2),  # the statement does not say
    ],
)
def test_an_updates_index_writes_follow_what_it_changes(sql: str, writes: int) -> None:
    cost = _cost(_orders("INCLUDE", projected_attributes=["name"]), _sql(sql))
    assert cost is not None
    assert cost["write_units_per_month"] == writes * SECONDS_PER_MONTH


def test_a_set_list_read_only_partly_counts_as_unknown() -> None:
    """PostgreSQL reads ``(name, email) = (...)`` as no column, so the parse names only
    ``flag``: the update is taken to change the index keys, never as changing nothing."""
    design = _orders()
    design["table_definitions"][0]["gsis"][0]["partition_key"] = [
        {"attribute_name": "name", "attribute_type": "S"}
    ]
    sql = "UPDATE orders SET flag = true, (name, email) = ($1, $2) WHERE order_id = $3"
    cost = _cost(design, _sql(sql))
    assert cost is not None
    assert cost["write_units_per_month"] == 2 * SECONDS_PER_MONTH


def test_the_set_list_is_read_in_the_source_dialect() -> None:
    """T-SQL brackets read as a column only in the SQL Server dialect: read in a generic
    one, the update would be taken to change the index keys (two writes)."""
    design = _orders()
    design["table_definitions"][0]["attributes"] = _attrs("order_id", "status", "displayname")
    sql = "UPDATE [dbo].[Orders] SET [DisplayName] = @p0 WHERE [order_id] = @p1"
    cost = _cost(design, _sql(sql, engine="sqlserver"))
    assert cost is not None
    assert cost["write_units_per_month"] == 0  # DisplayName is neither a key nor projected


@pytest.mark.parametrize(
    ("query", "rows"),
    [
        ({"rows_affected_avg": 0.0156}, 0.0156),  # a fraction above zero stays a fraction
        ({"rows_affected_avg": 0.0}, 1.0),  # exactly zero: missing data, one row a call
        ({"rows_affected_avg": 0.0, "rows_returned_avg": 3}, 3.0),  # as a missing value
        ({"rows_affected_avg": 4}, 4.0),
        ({"rows_returned_avg": 2}, 2.0),
        ({}, 1.0),  # no row statistics: one row a call
    ],
)
def test_the_collected_rows_per_call_are_used_as_they_are(query: dict, rows: float) -> None:
    sql = "UPDATE orders SET status = $1 WHERE order_id = $2"
    cost = _cost(_orders(), _sql(sql, cps=8.0, **query))
    assert cost is not None
    assert cost["write_units_per_month"] == round(8.0 * rows * 2 * SECONDS_PER_MONTH, 2)


def test_the_line_says_how_updates_and_sparse_items_are_counted() -> None:
    cost = _cost(_design(), _collector(**{"q-put": 2.0}))
    assert cost is not None
    line = tco_assumption(cost)
    assert "may set the index's keys" in line
    assert "two for an update that changes an index key" in line
    assert "collected rows per call" in line
    assert "exactly zero counted as one row" in line
