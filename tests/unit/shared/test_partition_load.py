# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1989: a DynamoDB design entry's load estimated per partition key value.

An entry is a table or GSI total; DynamoDB's limit is per key value. The estimate divides
the total by the key's collected distinct values (a unique key's row count, a column's
cardinality), takes the whole total for a key every item shares, and says "not measured"
when the collection holds neither. The total is never capped.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.shared.partition_load import (
    DistinctValues,
    hot_partition_rows,
    key_distinct_values,
    over_key_limit,
    partition_load,
)

OPTIONS = {
    "table_id": "wp.wp_options",
    "table_name": "wp_options",
    "row_count": 55_527,
    "primary_key": ["option_id"],
    "indexes": [
        {"index_name": "option_name", "columns": ["option_name"], "is_unique": True},
        {"index_name": "autoload", "columns": ["autoload"], "is_unique": False},
    ],
    "columns": [{"column_name": "option_name"}, {"column_name": "autoload"}],
}
POSTMETA = {
    "table_id": "wp.wp_postmeta",
    "table_name": "wp_postmeta",
    "row_count": 910_705,
    "primary_key": ["meta_id"],
    "indexes": [{"index_name": "post_id", "columns": ["post_id"], "is_unique": False}],
    "columns": [{"column_name": "post_id"}, {"column_name": "meta_id"}],
}
STATUS = {
    "table_id": "shop.orders",
    "table_name": "orders",
    "row_count": 10_000,
    "primary_key": ["id"],
    "columns": [{"column_name": "id"}, {"column_name": "status", "cardinality": 2}],
}


def _attr(name: str, table: str, column: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": "S", "source_table": table, **extra}
    if column is not None:
        out["source_column"] = column
    return out


def _table(name: str, pk: str, attributes: list[dict], **extra: Any) -> dict[str, Any]:
    return {
        "table_name": name,
        "partition_key": {"attribute_name": pk, "attribute_type": "S"},
        "attributes": attributes,
        **extra,
    }


def _entry(table: str, units: float, operation: str = "read", gsi: str | None = None):
    return {
        "table_name": table,
        "gsi_name": gsi,
        "operation": operation,
        "rcu_or_wcu_per_second": units,
        "contributing_patterns": ["q2", "q1"],
    }


VALUES = DistinctValues.from_collector_tables([OPTIONS, POSTMETA, STATUS])


def test_a_unique_key_has_the_tables_row_count_and_a_column_its_cardinality() -> None:
    assert key_distinct_values(OPTIONS) == [
        {"columns": ["option_id"], "distinct_values": 55_527, "basis": "unique_key"},
        {"columns": ["option_name"], "distinct_values": 55_527, "basis": "unique_key"},
    ]
    assert key_distinct_values(STATUS)[-1] == {
        "columns": ["status"],
        "distinct_values": 2,
        "basis": "column_statistics",
    }
    # A non-unique column of a source that collects no cardinality: nothing is known.
    assert VALUES.of("wp.wp_postmeta", ["post_id"]) is None


def test_a_table_with_no_row_count_gives_its_unique_key_no_count() -> None:
    assert key_distinct_values({**OPTIONS, "row_count": None}) == []


def test_a_row_count_of_zero_is_not_measured_unless_the_switch_says_one_key(monkeypatch) -> None:
    """#1989 review: an estimate of 0 rows proves nothing, so the key is not measured and
    can never raise HIGH; the maintainer's switch makes it one key value instead."""
    import src.shared.partition_load as module

    assert key_distinct_values({**STATUS, "row_count": 0, "columns": []}) == []
    monkeypatch.setattr(module, "ZERO_ROW_TABLE_IS_ONE_KEY", True)
    (pk,) = key_distinct_values({**STATUS, "row_count": 0, "columns": []})
    assert pk["distinct_values"] == 1


def test_a_cardinality_of_zero_is_not_measured_and_never_high() -> None:
    """Probe from the review: 5M rows, 3,500 RCU on a column whose cardinality reads 0."""
    table = {
        "table_id": "big.events",
        "row_count": 5_000_000,
        "columns": [{"column_name": "kind", "cardinality": 0}],
    }
    values = DistinctValues.from_collector_tables([table])
    assert values.of("big.events", ["kind"]) is None
    design = {
        "table_definitions": [_table("Events", "kind", [_attr("kind", "big.events", "kind")])]
    }
    row = partition_load(_entry("Events", 3_500.0), design, values)
    assert row["per_key_basis"] == "not_measured"
    assert not over_key_limit(row)


def test_a_cardinality_is_capped_at_the_row_count_and_a_one_row_table_has_one_value() -> None:
    table = {"table_id": "t", "row_count": 10, "columns": [{"column_name": "c", "cardinality": 99}]}
    assert key_distinct_values(table)[-1]["distinct_values"] == 10
    one = {"table_id": "t", "row_count": 1, "columns": [{"column_name": "c"}]}
    assert key_distinct_values(one)[-1] == {
        "columns": ["c"],
        "distinct_values": 1,
        "basis": "column_statistics",
    }


def test_the_total_spreads_over_a_unique_keys_distinct_values() -> None:
    design = {
        "table_definitions": [
            _table("Options", "option_name", [_attr("option_name", "wp.wp_options", "option_name")])
        ]
    }
    row = partition_load(_entry("Options", 9_087.9527), design, VALUES)
    assert row["total_pct_of_key_limit"] == pytest.approx(302.9318)  # never capped at 100
    assert row["distinct_key_values"] == 55_527
    assert row["per_key_basis"] == "distinct_values"
    assert row["per_key_units_per_second"] == pytest.approx(9_087.9527 / 55_527, abs=1e-4)
    assert row["per_key_pct_of_limit"] == pytest.approx(0.0055, abs=1e-4)
    assert row["query_ids"] == ["q1", "q2"]
    assert not over_key_limit(row)


def test_a_key_with_no_collected_distinct_values_is_not_measured() -> None:
    design = {
        "table_definitions": [
            _table("Postmeta", "post_id", [_attr("post_id", "wp.wp_postmeta", "post_id")])
        ]
    }
    row = partition_load(_entry("Postmeta", 1_004.5, "write"), design, VALUES)
    assert row["per_key_basis"] == "not_measured"
    assert row["per_key_units_per_second"] is None
    assert row["per_key_pct_of_limit"] is None
    assert row["total_pct_of_key_limit"] == pytest.approx(100.45)
    assert not over_key_limit(row)


def test_a_constant_key_carries_the_whole_total_on_one_value() -> None:
    constant = _attr("scope", "shop.orders", calculation={"type": "constant", "value": "ALL"})
    design = {"table_definitions": [_table("Orders", "scope", [constant])]}
    row = partition_load(_entry("Orders", 1_200.0, "write"), design, VALUES)
    assert row["per_key_basis"] == "single_key"
    assert row["distinct_key_values"] == 1
    assert row["per_key_pct_of_limit"] == pytest.approx(120.0)
    assert over_key_limit(row)


def test_an_entity_template_with_no_column_is_one_key_value() -> None:
    design = {
        "table_definitions": [
            {
                "table_name": "Config",
                "partition_key": {"attribute_name": "PK", "attribute_type": "S"},
                "entities": [
                    {
                        "entity_type": "SETTING",
                        "source_table": "wp.wp_options",
                        "pk_template": "SETTINGS",
                        "sk_template": "OPT#{option_name}",
                        "attributes": [_attr("option_name", "wp.wp_options", "option_name")],
                    }
                ],
            }
        ]
    }
    row = partition_load(_entry("Config", 3_300.0), design, VALUES)
    assert row["per_key_basis"] == "single_key"
    assert over_key_limit(row)


def test_an_item_collection_divides_by_its_most_selective_known_entity() -> None:
    """Posts and their meta share post_id: the posts' unique ID is a lower bound on the
    collection's key values, though the meta's post_id has no count of its own."""
    posts = {
        "table_id": "wp.wp_posts",
        "table_name": "wp_posts",
        "row_count": 24_276,
        "primary_key": ["ID"],
        "columns": [{"column_name": "ID"}],
    }
    values = DistinctValues.from_collector_tables([posts, POSTMETA])
    design = {
        "table_definitions": [
            {
                "table_name": "Posts",
                "partition_key": {"attribute_name": "PK", "attribute_type": "S"},
                "entities": [
                    {
                        "entity_type": "POST",
                        "source_table": "wp.wp_posts",
                        "pk_template": "POST#{ID}",
                        "sk_template": "POST",
                        "attributes": [_attr("ID", "wp.wp_posts", "ID")],
                    },
                    {
                        "entity_type": "META",
                        "source_table": "wp.wp_postmeta",
                        "pk_template": "POST#{post_id}",
                        "sk_template": "META#{meta_id}",
                        "attributes": [_attr("post_id", "wp.wp_postmeta", "post_id")],
                    },
                ],
            }
        ]
    }
    row = partition_load(_entry("Posts", 2_427.6, "write"), design, values)
    assert row["distinct_key_values"] == 24_276
    assert row["per_key_units_per_second"] == pytest.approx(0.1, abs=1e-4)


def test_a_low_cardinality_gsi_key_can_be_over_the_limit() -> None:
    design = {
        "table_definitions": [
            _table(
                "Orders",
                "id",
                [_attr("id", "shop.orders", "id"), _attr("status", "shop.orders", "status")],
                gsis=[
                    {
                        "gsi_name": "ByStatus",
                        "partition_key": [{"attribute_name": "status", "attribute_type": "S"}],
                    }
                ],
            )
        ]
    }
    row = partition_load(_entry("Orders", 7_000.0, gsi="ByStatus"), design, VALUES)
    assert row["distinct_key_values"] == 2
    assert row["per_key_pct_of_limit"] == pytest.approx(7_000 / 2 / 3_000 * 100, abs=1e-3)
    assert over_key_limit(row)
    # The same load on the base table, keyed by the unique id, is spread over 10,000 values.
    base = partition_load(_entry("Orders", 7_000.0), design, VALUES)
    assert base["distinct_key_values"] == 10_000
    assert not over_key_limit(base)


def test_a_multi_attribute_key_with_an_unknown_part_and_a_constant_is_not_measured() -> None:
    constant = _attr("kind", "wp.wp_postmeta", calculation={"type": "constant", "value": "M"})
    design = {
        "table_definitions": [
            _table(
                "Postmeta",
                "meta_id",
                [_attr("post_id", "wp.wp_postmeta", "post_id"), constant],
                gsis=[
                    {
                        "gsi_name": "ByPost",
                        "partition_key": [
                            {"attribute_name": "kind", "attribute_type": "S"},
                            {"attribute_name": "post_id", "attribute_type": "N"},
                        ],
                    }
                ],
            )
        ]
    }
    row = partition_load(_entry("Postmeta", 3_000.0, gsi="ByPost"), design, VALUES)
    assert row["per_key_basis"] == "not_measured"


def test_a_table_the_design_does_not_define_is_not_measured() -> None:
    row = partition_load(_entry("Missing", 5_000.0), {"table_definitions": []}, VALUES)
    assert row["per_key_basis"] == "not_measured"


def test_rows_keep_only_entries_above_the_at_risk_share_and_compute_it_from_the_units() -> None:
    design = {
        "table_definitions": [
            _table("Options", "option_name", [_attr("option_name", "wp.wp_options", "option_name")])
        ],
        "hot_partition_analysis": [
            # The model's own flag and capped figure are not what decides.
            {**_entry("Options", 9_087.95), "utilization_pct": 100, "at_risk": True},
            {**_entry("Options", 600.0, "write"), "utilization_pct": 60, "at_risk": False},
            {**_entry("Options", 2_000.0), "utilization_pct": 100, "at_risk": True},
        ],
    }
    rows = hot_partition_rows({"DynamoDB": design}, [OPTIONS])
    assert [(r["engine"], r["total_units_per_second"]) for r in rows] == [("dynamodb", 9_087.95)]


def test_rows_without_the_collection_read_as_not_measured() -> None:
    design = {
        "table_definitions": [
            _table("Options", "option_name", [_attr("option_name", "wp.wp_options", "option_name")])
        ],
        "hot_partition_analysis": [_entry("Options", 9_087.95)],
    }
    (row,) = hot_partition_rows({"dynamodb": design})
    assert row["per_key_basis"] == "not_measured"


def test_design_metrics_and_the_collector_give_the_same_counts() -> None:
    from src.tools.schema.design_metrics import build_design_metrics

    metrics = build_design_metrics("dynamodb", [OPTIONS, POSTMETA, STATUS], []).model_dump(
        mode="json"
    )
    from_metrics = DistinctValues.from_design_metrics(metrics)
    for table, columns in (
        ("wp.wp_options", ["option_name"]),
        ("wp.wp_postmeta", ["post_id"]),
        ("shop.orders", ["status"]),
        ("shop.orders", ["id", "status"]),
    ):
        assert from_metrics.of(table, columns) == VALUES.of(table, columns), (table, columns)


def _sharded(transformation: dict | None = None, calculation: dict | None = None) -> dict:
    shard = _attr("shard", "wp.wp_options", "option_name")
    if transformation:
        shard["transformation"] = transformation
    if calculation:
        shard["calculation"] = calculation
    return {
        "table_definitions": [
            _table(
                "Options",
                "option_name",
                [_attr("option_name", "wp.wp_options", "option_name"), shard],
                gsis=[
                    {
                        "gsi_name": "ByShard",
                        "partition_key": [{"attribute_name": "shard", "attribute_type": "N"}],
                    }
                ],
            )
        ]
    }


def test_a_modulo_key_has_at_most_n_values_whatever_its_source_column_holds() -> None:
    """Review probe: a GSI on ``x % 4`` at 30,000 RCU is about 250% of one key value's
    limit (HIGH), never the source column's 55,527 values."""
    design = _sharded({"type": "number-format", "format": "hash(option_name) % 4"})
    row = partition_load(_entry("Options", 30_000.0, gsi="ByShard"), design, VALUES)
    assert row["distinct_key_values"] == 4
    assert row["distinct_key_bound"] == "at_most"
    assert row["per_key_pct_of_limit"] == pytest.approx(250.0)
    assert over_key_limit(row)


def test_any_other_transform_or_calculation_on_the_key_is_not_measured() -> None:
    for design in (
        _sharded({"type": "date-format", "format": "YYYY-MM"}),
        _sharded(calculation={"type": "case", "cases": [{"when": "a", "then": "b"}]}),
    ):
        row = partition_load(_entry("Options", 30_000.0, gsi="ByShard"), design, VALUES)
        assert row["per_key_basis"] == "not_measured", design


def test_a_static_entity_in_an_item_collection_is_one_key_value() -> None:
    """Review item 3: a CONFIG entity next to USER#{id} carries its own load on one key,
    never spread over the users' 10,000 ids."""
    design = {
        "table_definitions": [
            {
                "table_name": "App",
                "partition_key": {"attribute_name": "PK", "attribute_type": "S"},
                "entities": [
                    {
                        "entity_type": "ORDER",
                        "source_table": "shop.orders",
                        "pk_template": "ORDER#{id}",
                        "sk_template": "ORDER",
                        "attributes": [_attr("id", "shop.orders", "id")],
                    },
                    {
                        "entity_type": "CONFIG",
                        "source_table": "wp.wp_options",
                        "pk_template": "CONFIG",
                        "sk_template": "OPT#{option_name}",
                        "attributes": [_attr("option_name", "wp.wp_options", "option_name")],
                    },
                ],
            }
        ]
    }
    row = partition_load(_entry("App", 3_300.0), design, VALUES)
    assert (row["per_key_basis"], row["distinct_key_values"]) == ("single_key", 1)
    assert over_key_limit(row)


def _with_patterns(*conditions: tuple[str, float]) -> dict:
    design = {
        "table_definitions": [
            _table("Options", "option_name", [_attr("option_name", "wp.wp_options", "option_name")])
        ],
        "access_patterns": [
            {
                "pattern_id": f"P{i}",
                "query_ids": [f"q{i + 1}"],
                "operation": "Query",
                "table_name": "Options",
                "key_condition": condition,
                "design_rps": rps,
            }
            for i, (condition, rps) in enumerate(conditions)
        ],
    }
    return design


def test_an_access_pattern_on_one_literal_key_carries_its_share_on_that_key() -> None:
    """Decision 1's access-pattern half: a pattern always using the same key value puts
    its share of the load on that one value, whatever the key's distinct values."""
    design = _with_patterns(("PK=option_name = 'siteurl'", 75.0), ("PK=option_name", 25.0))
    row = partition_load(_entry("Options", 4_000.0), design, VALUES)
    assert row["per_key_source"] == "access_pattern"
    assert (row["per_key_basis"], row["distinct_key_values"]) == ("single_key", 1)
    assert row["per_key_units_per_second"] == pytest.approx(3_000.0)  # 75% of 4,000


def test_an_in_list_fan_out_spreads_over_its_k_values_at_most() -> None:
    design = _with_patterns(("PK=option_name IN (0..31)", 10.0))
    row = partition_load(_entry("Options", 32_000.0), design, VALUES)
    assert (row["distinct_key_values"], row["distinct_key_bound"]) == (32, "at_most")
    assert row["per_key_units_per_second"] == pytest.approx(1_000.0)
    listed = _with_patterns(("PK IN ('a', 'b', 'c')", 10.0))
    assert partition_load(_entry("Options", 300.0), listed, VALUES)["distinct_key_values"] == 3


def test_a_key_condition_that_names_no_literal_reads_the_key_values() -> None:
    design = _with_patterns(("PK=option_name", 10.0))
    row = partition_load(_entry("Options", 9_000.0), design, VALUES)
    assert (row["per_key_source"], row["distinct_key_values"]) == ("key_values", 55_527)


def test_every_row_marks_its_total_as_an_upper_bound() -> None:
    row = partition_load(_entry("Missing", 1.0), {"table_definitions": []}, VALUES)
    assert row["total_is_upper_bound"] is True


def test_an_entry_without_units_reads_its_own_utilization() -> None:
    entry = {"table_name": "Missing", "operation": "write", "utilization_pct": 90.0}
    assert partition_load(entry, {}, VALUES)["total_units_per_second"] == 900.0


def test_an_entry_with_another_operation_is_refused_and_left_out_of_the_rows() -> None:
    with pytest.raises(ValueError):
        partition_load({**_entry("Options", 9_000.0), "operation": "scan"}, {}, VALUES)
    design = {"hot_partition_analysis": [{**_entry("Options", 9_000.0), "operation": "scan"}]}
    assert hot_partition_rows({"dynamodb": design}) == []
