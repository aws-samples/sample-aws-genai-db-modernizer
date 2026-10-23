"""The data movement plan generator on hand-built jobs (#1413).

Each case builds the smallest finished job that reaches one rule of
``src.tools.data_movement.plan``: the keys a hub topic can use, the key designs and
attributes no writer can build, a wave 1 with no Aurora target, a source dialect with
no Debezium connector, the cost with and without prices, and the round trip through
the contract registry. The sample jobs are covered end to end in
``tests/e2e/test_data_movement_plan.py``.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pytest

from src.contracts.data_movement_plan import DataMovementPlan
from src.shared.catalog.prices import PriceTable
from src.shared.contract_versioning import contract_for_artifact, current_version, load_contract
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.plan import JobInputs, build_plans, write_plans


def _col(name: str, nullable: bool = False) -> dict[str, Any]:
    return {"column_name": name, "nullable": nullable}


def _table(
    table_id: str,
    columns: list[dict[str, Any]],
    pk: list[str] | None = None,
    indexes: list[dict[str, Any]] | None = None,
    rows: int | None = 10,
) -> dict[str, Any]:
    return {
        "table_id": table_id,
        "table_name": table_id.split(".")[-1],
        "columns": columns,
        "primary_key": pk or [],
        "indexes": indexes or [],
        "row_count": rows,
    }


def _unique(name: str, cols: list[str]) -> dict[str, Any]:
    return {"index_name": name, "columns": cols, "is_unique": True, "predicate": None}


TABLES = [
    _table("shop.orders", [_col("id"), _col("status")], pk=["id"], rows=500),
    # No primary key, a unique index on NOT NULL columns: keyed by the index.
    _table(
        "shop.order_tags",
        [_col("order_id"), _col("tag")],
        indexes=[_unique("ux_order_tag", ["order_id", "tag"])],
    ),
    # No primary key, the only unique index has a nullable column: no key.
    _table(
        "shop.audit",
        [_col("event"), _col("ref", nullable=True)],
        indexes=[_unique("ux_audit", ["event", "ref"])],
    ),
    _table("shop.settings", [_col("name"), _col("value")], pk=["name"]),
]


def _qa(qid: str, engine: str, tables: list[str], cache: str | None = None) -> dict[str, Any]:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "source_tables": tables,
        "in_scope": True,
        "cache_engine": cache,
        "cache_dropped": False,
    }


def _waves(first_engines: list[str] | None = None) -> list[dict[str, Any]]:
    all_tables = [t["table_id"] for t in TABLES] + ["shop.order_view"]
    return [
        {
            "wave": 1,
            "title": "Move to Aurora MySQL",
            "engines": ["aurora_mysql"] if first_engines is None else first_engines,
            "moves_from": ["mysql"],
            "homogeneity": "homogeneous" if first_engines is None else "heterogeneous",
            "tables": all_tables,
            "gate": "Aurora MySQL serves every query.",
        },
        {
            "wave": 2,
            "title": "Cache hot reads with ElastiCache",
            "engines": ["elasticache"],
            "moves_from": [],
            "fronts": "aurora_mysql",
            "tables": [],
            "gate": "Cache hit rate verified.",
        },
        {
            "wave": 3,
            "title": "Move key-value queries to DynamoDB",
            "engines": ["dynamodb"],
            "moves_from": ["aurora_mysql"],
            "tables": ["shop.audit", "shop.order_tags", "shop.order_view", "shop.orders"],
            "table_owners": [{"table": "shop.order_tags", "owner": "aurora_mysql", "sync": "CDC"}],
            "gate": "DynamoDB serves its queries.",
        },
    ]


def _job(
    *,
    waves: list[dict[str, Any]] | None = None,
    source_engine: str = "mysql",
    deployment_type: str | None = None,
    designs: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> JobInputs:
    report = {
        "migration_waves": _waves() if waves is None else waves,
        "assignment_summary": {"version": 2},
        "table_sources": [
            {
                "table": "shop.orders",
                "source_of_truth": "dynamodb",
                "write_engines": ["dynamodb"],
                "write_query_ids": ["w1"],
                "replicas": [{"engine": "aurora_mysql", "read_query_ids": ["r2", "r1"]}],
            },
            {
                "table": "shop.order_tags",
                "source_of_truth": "aurora_mysql",
                "write_engines": ["aurora_mysql"],
                "write_query_ids": ["w2"],
                "replicas": [{"engine": "dynamodb", "read_query_ids": ["r3"]}],
            },
        ],
    }
    assignment = {
        "version": 2,
        "query_assignments": [
            _qa("r1", "aurora_mysql", ["shop.orders"]),
            _qa("r2", "aurora_mysql", ["orders"], cache="elasticache"),
            _qa("r3", "dynamodb", ["shop.order_tags"]),
            _qa("r4", "dynamodb", ["shop.orders", "shop.audit"]),
            _qa("r5", "aurora_mysql", ["shop.settings"], cache="elasticache"),
            _qa("w1", "dynamodb", ["shop.orders"]),
            _qa("w2", "aurora_mysql", ["shop.order_tags"]),
        ],
    }
    collector = {
        "metadata": {
            "source_database": {
                "engine": source_engine,
                "database_name": "shop",
                "deployment_type": deployment_type,
            }
        },
        "database_schema": {
            "tables": TABLES,
            "views": [{"view_id": "shop.order_view", "view_name": "order_view"}],
        },
    }
    return JobInputs(
        job_id="job-1",
        database_name="shop",
        report=report,
        assignment=assignment,
        collector=collector,
        schema_designs=designs or {},
    )


def _ec_design(key_pattern: str, fields: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    return (
        "schema-elasticache/v2/schema_output.json",
        {
            "contract_version": "1.1",
            "key_designs": [
                {
                    "key_pattern": key_pattern,
                    "source_tables": ["shop.orders"],
                    "fields_mapped": fields or ["status"],
                }
            ],
        },
    )


def _plans(**kwargs: Any) -> list[DataMovementPlan]:
    return build_plans(_job(**kwargs), prices=None)


def _kinds(plan: DataMovementPlan, kind: str) -> list[tuple[str | None, str | None]]:
    return [(u.table, u.item) for u in plan.unsupported if u.kind == kind]


def test_one_plan_per_wave_in_order() -> None:
    plans = _plans()
    assert [p.wave for p in plans] == [1, 2, 3]
    assert {p.wave_count for p in plans} == {3}
    assert {p.assignment_version for p in plans} == {2}


def test_wave_one_is_dms_from_the_legacy_source_with_no_hub() -> None:
    wave1 = _plans()[0]
    assert wave1.cdc.tool == "dms"
    assert wave1.cdc.connector_class is None
    assert wave1.source.engine == "mysql"
    assert wave1.hub == []
    assert wave1.initial_load.strategy == "dms_full_load"
    assert [(t.engine, t.role) for t in wave1.targets] == [("aurora_mysql", "owner")]
    assert wave1.source.views == ["shop.order_view"]
    assert [s.name for s in wave1.stacks] == ["dms-replication"]
    # The table with no usable key cannot have its updates and deletes replicated.
    assert _kinds(wave1, "no_key") == [("shop.audit", None)]


def test_later_waves_are_debezium_from_the_aurora_engine() -> None:
    for plan in _plans()[1:]:
        assert plan.cdc.tool == "debezium"
        assert plan.source.engine == "aurora_mysql"
        assert plan.cdc.connector_class == "io.debezium.connector.mysql.MySqlConnector"
        assert plan.cdc.snapshot_mode == "no_data"
        assert plan.initial_load.position_capture == "binlog_position"
        assert {s.name for s in plan.cdc.required_source_settings} >= {
            "binlog_format",
            "binlog_row_image",
        }


def test_hub_keys_come_from_the_primary_key_or_a_not_null_unique_index() -> None:
    wave3 = _plans()[2]
    keys = {h.table: (h.key_columns, h.key_source) for h in wave3.hub}
    assert keys == {
        "shop.orders": (["id"], "primary_key"),
        "shop.order_tags": (["order_id", "tag"], "unique_index"),
    }
    assert all(h.cleanup_policy == "compact" for h in wave3.hub)
    assert wave3.cdc.topic_prefix == "dm-job-1-w3"
    assert all(h.topic == f"dm-job-1-w3.{h.table}" for h in wave3.hub)
    # The nullable unique index is no key, and a view has no change stream.
    assert _kinds(wave3, "no_key") == [("shop.audit", None)]
    assert _kinds(wave3, "view") == [("shop.order_view", None)]


def test_every_table_a_target_reads_has_a_topic_or_an_unsupported_entry() -> None:
    for plan in _plans()[1:]:
        read = {t for target in plan.targets for t in target.tables}
        covered = {h.table for h in plan.hub} | {
            u.table for u in plan.unsupported if u.kind in {"no_key", "view", "unknown_table"}
        }
        assert read <= covered, plan.wave


def test_the_cache_wave_is_a_cache_replica_of_the_engine_it_fronts() -> None:
    wave2 = _plans()[1]
    assert [(t.engine, t.role, t.source_of_truth) for t in wave2.targets] == [
        ("elasticache", "cache", "aurora_mysql")
    ]
    assert wave2.targets[0].tables == ["shop.orders", "shop.settings"]
    # The cached reads may lag; the bare "orders" name resolves to its table.
    assert [(r.table, r.read_query_ids) for r in wave2.gates.lagging_reads] == [
        ("shop.orders", ["r2"]),
        ("shop.settings", ["r5"]),
    ]
    assert {g.check for g in wave2.gates.parity} == {"sampled_key_compare"}


def test_owner_and_replica_targets_follow_the_wave_owners() -> None:
    wave3 = _plans()[2]
    assert [(t.engine, t.role, t.source_of_truth, t.tables) for t in wave3.targets] == [
        ("dynamodb", "owner", "dynamodb", ["shop.audit", "shop.order_view", "shop.orders"]),
        ("dynamodb", "replica", "aurora_mysql", ["shop.order_tags"]),
    ]
    # DynamoDB's read of the Aurora-owned table lags; the reads Aurora keeps of the
    # table DynamoDB takes over need a feed the hub cannot give.
    assert [(r.table, r.engine, r.read_query_ids) for r in wave3.gates.lagging_reads] == [
        ("shop.order_tags", "dynamodb", ["r3"])
    ]
    reverse = [(u.table, u.engine) for u in wave3.unsupported if u.kind == "reverse_replication"]
    assert reverse == [("shop.orders", "aurora_mysql"), ("shop.orders", "elasticache")]


def test_a_target_with_no_schema_design_is_unsupported() -> None:
    plans = _plans()
    assert [u.engine for u in plans[1].unsupported if u.kind == "no_schema_design"] == [
        "elasticache"
    ]
    assert [u.engine for u in plans[2].unsupported if u.kind == "no_schema_design"] == ["dynamodb"]
    assert not [u for u in plans[0].unsupported if u.kind == "no_schema_design"]


def test_a_cache_key_design_whose_placeholder_is_a_column_is_buildable() -> None:
    wave2 = _plans(designs={"elasticache": _ec_design("shop:order:{id}")})[1]
    ref = wave2.targets[0].transformation
    assert ref is not None and ref.artifact == "schema-elasticache/v2/schema_output.json"
    assert ref.design_version == "1.1"
    assert _kinds(wave2, "key_design") == []
    assert _kinds(wave2, "attribute") == []
    # shop.settings is cached but no key design covers it.
    assert _kinds(wave2, "unmapped_table") == [("shop.settings", None)]


def test_a_cache_key_design_whose_placeholder_is_no_column_is_unsupported() -> None:
    wave2 = _plans(designs={"elasticache": _ec_design("shop:order:{order_uuid}", ["total"])})[1]
    assert _kinds(wave2, "key_design") == [("shop.orders", "shop:order:{order_uuid}")]
    assert _kinds(wave2, "attribute") == [("shop.orders", "shop:order:{order_uuid} total")]


def test_a_dynamodb_attribute_with_no_source_column_is_unsupported() -> None:
    design = {
        "contract_version": "1.0",
        "table_definitions": [
            {
                "table_name": "Orders",
                "source_tables": ["orders", "order_tags", "audit"],
                "attributes": [
                    {"name": "id", "source_table": "orders", "source_column": "id"},
                    {"name": "order_no", "source_table": "orders", "source_column": "number"},
                    {"name": "label", "source_table": None, "source_column": None},
                    {
                        "name": "tag_count",
                        "source_table": None,
                        "source_column": None,
                        "calculation": "count of tags",
                    },
                ],
            }
        ],
    }
    wave3 = _plans(designs={"dynamodb": ("schema-dynamodb/v2/schema_output.json", design)})[2]
    assert _kinds(wave3, "attribute") == [
        (None, "Orders.label"),
        ("shop.orders", "Orders.order_no"),
    ]
    assert _kinds(wave3, "unmapped_table") == [("shop.order_view", None)]


def test_a_wave_one_with_no_aurora_engine_names_no_target() -> None:
    waves = _waves(first_engines=[])
    wave1 = build_plans(_job(waves=waves, source_engine="sqlserver"), prices=None)[0]
    assert wave1.targets == []
    assert wave1.cdc.tool == "dms"
    assert _kinds(wave1, "no_target") == [(None, None)]
    assert {s.name for s in wave1.cdc.required_source_settings} >= {"change data capture"}


def test_a_source_with_no_debezium_connector_is_unsupported() -> None:
    waves = _waves()
    waves[2]["moves_from"] = ["informix"]
    wave3 = _plans(waves=waves)[2]
    assert wave3.cdc.connector_class is None
    assert wave3.source.dialect is None
    assert [u.engine for u in wave3.unsupported if u.kind == "source_dialect"] == ["informix"]


def test_a_job_with_no_cache_wave_plans_its_waves() -> None:
    waves = [w for w in _waves() if w["engines"] != ["elasticache"]]
    waves[1]["wave"] = 2
    plans = _plans(waves=waves)
    assert [p.wave for p in plans] == [1, 2]
    assert not any(t.role == "cache" for p in plans for t in p.targets)
    assert plans[1].cdc.tool == "debezium"


def test_a_job_with_no_waves_has_no_plans() -> None:
    assert _plans(waves=[]) == []


def _table_of(sections: dict[str, dict[str, dict[str, Any]]]) -> PriceTable:
    return PriceTable(
        region="us-east-1",
        retrieved_on=dt.date(2026, 11, 2),
        published={"AmazonMSK": dt.date(2026, 11, 1)},
        sections=sections,
    )


# Round numbers chosen so each component's arithmetic is checked by hand; they are test
# inputs, not AWS prices.
PRICES = _table_of(
    {
        "dms": {
            "replication": {
                "instance_hourly": {"dms.r6i.large": 0.5},
                "storage_price_per_gb_month": 0.1,
            }
        },
        "msk": {
            "provisioned": {
                "instance_hourly": {"kafka.m5.large": 0.2},
                "storage_price_per_gb_month": 0.1,
            }
        },
        "msk_connect": {"workers": {"mcu_price_per_hour": 0.1}},
    }
)


def _sized(size_mb: float | None) -> JobInputs:
    """The default job with every table measured at ``size_mb`` (None: not measured)."""
    job = _job()
    tables = copy.deepcopy(job.collector["database_schema"]["tables"])
    for t in tables:
        t["size_mb"] = size_mb
    collector = {
        **job.collector,
        "database_schema": {**job.collector["database_schema"], "tables": tables},
    }
    return dataclasses.replace(job, collector=collector)


def test_the_dms_wave_costs_one_instance_month_and_its_storage() -> None:
    wave1 = build_plans(_job(), prices=PRICES)[0]
    # 0.5 x 730 hours + 0.1 x 100 GB
    assert wave1.monthly_cost_usd == 375.0
    assert not [u for u in wave1.unsupported if u.kind == "cost"]
    text = " ".join(wave1.cost_assumptions)
    assert "dms.r6i.large" in text and "retrieved on 2026-11-02" in text


def test_the_first_debezium_wave_carries_the_brokers_and_later_waves_do_not() -> None:
    # 100 MiB per table: 1 GiB per broker.
    plans = build_plans(_sized(100.0), prices=PRICES)
    first, later = plans[1], plans[2]
    for plan in (first, later):
        connectors = 1 + len(plan.targets)
        storage = 0.1 * 3 * math.ceil(sum(100.0 for _ in plan.hub) / 1024)
        connect = 0.1 * connectors * 730
        brokers = 0.2 * 3 * 730 if plan is first else 0.0
        assert plan.monthly_cost_usd == pytest.approx(round(brokers + storage + connect, 2))
        assert not [u for u in plan.unsupported if u.kind == "cost"]
    assert later.monthly_cost_usd < first.monthly_cost_usd


def test_topic_storage_is_each_brokers_copy_rounded_up_to_a_gib() -> None:
    small = build_plans(_sized(1.0), prices=PRICES)[2]
    big = build_plans(_sized(2048.0), prices=PRICES)[2]
    hub = len(small.hub)
    assert hub and len(big.hub) == hub
    difference = big.monthly_cost_usd - small.monthly_cost_usd
    # small: 1 GiB per broker; big: ceil(hub x 2 GiB) per broker.
    assert difference == pytest.approx(0.1 * 3 * (2 * hub - 1), abs=0.01)


def test_an_unmeasured_table_leaves_the_debezium_cost_null_with_a_reason() -> None:
    plan = build_plans(_sized(None), prices=PRICES)[2]
    assert plan.monthly_cost_usd is None
    cost = [u for u in plan.unsupported if u.kind == "cost"]
    assert [u.item for u in cost] == ["hub_storage"]
    assert "size_mb" in cost[0].reason


@pytest.mark.parametrize(
    ("sections", "items"),
    [
        ({}, ["dms", "dms_storage"]),
        ({"dms": {"replication": {"storage_price_per_gb_month": 0.1}}}, ["dms"]),
        ({"dms": {"replication": {"instance_hourly": {"dms.r6i.large": 0.5}}}}, ["dms_storage"]),
    ],
)
def test_a_missing_dms_price_leaves_the_cost_null_with_a_reason(
    sections: dict[str, dict[str, dict[str, Any]]], items: list[str]
) -> None:
    wave1 = build_plans(_job(), prices=_table_of(sections or {"aurora_mysql": {"standard": {}}}))[0]
    assert wave1.monthly_cost_usd is None
    assert wave1.cost_assumptions
    cost = [u for u in wave1.unsupported if u.kind == "cost"]
    assert [u.item for u in cost] == items
    assert all("has no price for" in u.reason for u in cost)


def test_a_missing_msk_connect_price_names_that_component() -> None:
    sections = {k: v for k, v in PRICES.sections.items() if k != "msk_connect"}
    plan = build_plans(_sized(1.0), prices=_table_of(sections))[2]
    assert plan.monthly_cost_usd is None
    assert [u.item for u in plan.unsupported if u.kind == "cost"] == ["msk_connect"]


def test_without_a_price_table_no_wave_states_a_cost() -> None:
    plans = build_plans(_job(), prices=None)
    assert all(p.monthly_cost_usd is None for p in plans)
    assert all(any(u.kind == "cost" for u in p.unsupported) for p in plans)
    assert not any("retrieved on" in a for p in plans for a in p.cost_assumptions)


def test_written_plans_are_the_registered_contract_and_read_back(tmp_path: Path) -> None:
    job = _job()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        "shop/job-1/synthesis/v2/report.json", {"contract_version": "1.0", **job.report}
    )
    store.write_json("shop/job-1/assignment/v2/assignment.json", dict(job.assignment))
    store.write_json("shop/job-1/collector/output.json", dict(job.collector))
    written = write_plans(store, "shop", "job-1")
    assert written == [f"shop/job-1/data-movement/wave-{n}/plan.json" for n in (1, 2, 3)]
    for path in written:
        assert contract_for_artifact(path) is DataMovementPlan
        payload = store.read_json(path)
        plan = load_contract(DataMovementPlan, payload, artifact=path)
        assert plan.contract_version == current_version(DataMovementPlan)
    # Writing again gives the same bytes.
    first = [(tmp_path / p).read_bytes() for p in written]
    write_plans(store, "shop", "job-1")
    assert [(tmp_path / p).read_bytes() for p in written] == first
    assert json.loads(first[0])["wave"] == 1


def test_each_debezium_wave_has_its_own_topic_prefix_and_names_a_signal_table() -> None:
    wave1, wave2, wave3 = _plans()
    assert wave1.cdc.topic_prefix is None and wave1.cdc.signal_table is None
    assert [p.cdc.topic_prefix for p in (wave2, wave3)] == ["dm-job-1-w2", "dm-job-1-w3"]
    for plan in (wave2, wave3):
        assert plan.cdc.signal_table == "shop.debezium_signal"
        signal = [s for s in plan.cdc.required_source_settings if s.name == "signal table"]
        assert len(signal) == 1 and signal[0].value.startswith("shop.debezium_signal (")
        assert all(h.topic.startswith(f"{plan.cdc.topic_prefix}.") for h in plan.hub)


@pytest.mark.parametrize(
    ("engine", "deployment", "expected", "absent"),
    [
        ("postgresql", None, "wal_level", "rds.logical_replication"),
        ("postgresql", "on_premises", "wal_level", "rds.logical_replication"),
        ("postgresql", "rds_instance", "rds.logical_replication", "wal_level"),
        ("mysql", "ec2_self_managed", "binlog retention", "binlog retention hours"),
        ("mysql", "rds_instance", "binlog retention hours", "binlog retention"),
    ],
)
def test_wave_one_settings_follow_the_source_deployment(
    engine: str, deployment: str | None, expected: str, absent: str
) -> None:
    wave1 = build_plans(_job(source_engine=engine, deployment_type=deployment), prices=None)[0]
    names = {s.name for s in wave1.cdc.required_source_settings}
    assert expected in names and absent not in names


def test_an_rds_sql_server_source_enables_capture_through_the_rds_procedure() -> None:
    waves = _waves(first_engines=[])
    job = _job(waves=waves, source_engine="sqlserver", deployment_type="rds_instance")
    cdc = build_plans(job, prices=None)[0].cdc
    capture = [s for s in cdc.required_source_settings if s.name == "change data capture"]
    assert "msdb.dbo.rds_cdc_enable_db" in capture[0].note
    assert "recovery model" not in {s.name for s in cdc.required_source_settings}


def test_later_waves_read_the_retained_engine_as_managed_whatever_the_source() -> None:
    wave3 = build_plans(_job(deployment_type="on_premises"), prices=None)[2]
    names = {s.name for s in wave3.cdc.required_source_settings}
    assert "binlog retention hours" in names


def test_a_replica_of_a_table_nothing_writes_still_lists_its_lagging_reads() -> None:
    waves = _waves()
    waves[2]["tables"].append("shop.settings")
    waves[2]["table_owners"].append(
        {"table": "shop.settings", "owner": "aurora_mysql", "sync": "CDC"}
    )
    job = _job(waves=waves)
    job.assignment["query_assignments"].append(_qa("r6", "dynamodb", ["shop.settings"]))
    wave3 = build_plans(job, prices=None)[2]
    replica = [t for t in wave3.targets if t.role == "replica"]
    assert replica[0].tables == ["shop.order_tags", "shop.settings"]
    assert ("shop.settings", "dynamodb", ["r6"]) in [
        (r.table, r.engine, r.read_query_ids) for r in wave3.gates.lagging_reads
    ]
    # Its owner is relational: the hub feeds it, nothing is unsupported.
    assert ("shop.settings", "dynamodb") not in [
        (u.table, u.engine) for u in wave3.unsupported if u.kind == "reverse_replication"
    ]


def test_a_replica_of_a_non_relational_owner_is_a_reverse_feed_even_if_nothing_writes_it() -> None:
    waves = _waves()
    waves[2]["engines"] = ["documentdb", "dynamodb"]
    waves[2]["table_owners"] = [{"table": "shop.settings", "owner": "dynamodb", "sync": "CDC"}]
    waves[2]["tables"] = ["shop.orders", "shop.settings"]
    job = _job(waves=waves)
    job.assignment["query_assignments"].extend(
        [_qa("r7", "documentdb", ["shop.settings"]), _qa("r8", "dynamodb", ["shop.settings"])]
    )
    wave3 = build_plans(job, prices=None)[2]
    roles = {(t.engine, t.role, t.source_of_truth): t.tables for t in wave3.targets}
    assert roles[("documentdb", "replica", "dynamodb")] == ["shop.settings"]
    assert ("shop.settings", "documentdb", ["r7"]) in [
        (r.table, r.engine, r.read_query_ids) for r in wave3.gates.lagging_reads
    ]
    reverse = {(u.table, u.engine) for u in wave3.unsupported if u.kind == "reverse_replication"}
    assert ("shop.settings", "documentdb") in reverse


def test_the_owner_agrees_with_table_owners_and_table_sources() -> None:
    wave3 = _plans()[2]
    by_table = {t: tg for tg in wave3.targets for t in tg.tables}
    # table_owners names the Aurora owner; table_sources names DynamoDB as the writer.
    assert by_table["shop.order_tags"].source_of_truth == "aurora_mysql"
    assert by_table["shop.orders"].role == "owner"
    assert by_table["shop.orders"].source_of_truth == "dynamodb"


def test_parity_is_checked_per_target_role() -> None:
    wave2, wave3 = _plans()[1:]
    assert {g.check for g in wave2.gates.parity} == {"sampled_key_compare"}
    assert {g.check for g in wave3.gates.parity} == {"row_count_and_key_checksum"}


def test_a_dynamodb_item_collection_checks_its_entities() -> None:
    design = {
        "contract_version": "1.0",
        "table_definitions": [
            {
                "table_name": "Shop",
                "source_tables": [],
                "attributes": None,
                "entities": [
                    {
                        "entity_type": "ORDER",
                        "source_table": "shop.orders",
                        "pk_template": "ORDER#{id}",
                        "sk_template": "STATUS#{state}",
                        "attributes": [
                            {"name": "id", "source_table": "shop.orders", "source_column": "id"},
                            {
                                "name": "total",
                                "source_table": "shop.orders",
                                "source_column": "total",
                            },
                        ],
                    },
                    {
                        "entity_type": "TAG",
                        "source_table": "shop.order_tags",
                        "pk_template": "ORDER#{order_id}",
                        "sk_template": "TAG#{tag}",
                        "attributes": [
                            {
                                "name": "pair",
                                "source_table": "shop.order_tags",
                                "source_column": ["order_id", "label"],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    wave3 = _plans(designs={"dynamodb": ("schema-dynamodb/v2/schema_output.json", design)})[2]
    assert _kinds(wave3, "key_design") == [("shop.orders", "Shop#ORDER STATUS#{state}")]
    assert _kinds(wave3, "attribute") == [
        ("shop.order_tags", "Shop#TAG.pair"),
        ("shop.orders", "Shop#ORDER.total"),
    ]
    # The entities map both their tables.
    unmapped = [t for t, _ in _kinds(wave3, "unmapped_table")]
    assert "shop.orders" not in unmapped and "shop.order_tags" not in unmapped


def test_a_job_with_no_aurora_wave_plans_no_dms_move() -> None:
    waves = [w for w in _waves() if w["wave"] != 1]
    for i, w in enumerate(waves, start=1):
        w["wave"] = i
    plans = _plans(waves=waves)
    assert [p.cdc.tool for p in plans] == ["debezium", "debezium"]
    assert plans[0].source.engine == "aurora_mysql"


def test_the_aurora_wave_is_found_by_its_marker_not_its_position() -> None:
    waves = _waves()
    waves[0]["homogeneity"] = None  # an older report: the relational first wave still counts
    assert _plans(waves=waves)[0].cdc.tool == "dms"
    waves[0]["engines"] = ["dynamodb"]
    assert _plans(waves=waves)[0].cdc.tool == "debezium"


def test_a_job_with_no_assignment_is_refused(tmp_path: Path) -> None:
    job = _job()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        "shop/job-1/synthesis/v2/report.json", {"contract_version": "1.0", **job.report}
    )
    store.write_json("shop/job-1/collector/output.json", dict(job.collector))
    with pytest.raises(FileNotFoundError, match="no assignment v2"):
        write_plans(store, "shop", "job-1")


@pytest.mark.passes_on_main
def test_the_committed_price_table_has_no_data_movement_prices_until_a_refresh_adds_them() -> None:
    # #1424: the sections come with the first online run of scripts/refresh_prices.py, so
    # no plan states a cost from the committed table and each says which price is missing.
    plans = build_plans(_job())
    assert all(p.monthly_cost_usd is None for p in plans)
    assert all(any(u.kind == "cost" for u in p.unsupported) for p in plans)
