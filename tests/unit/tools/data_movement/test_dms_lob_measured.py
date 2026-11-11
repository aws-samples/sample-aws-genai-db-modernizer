# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's LobMaxSize from measured LOB sizes, and the instance class it needs (#1918).

In a live run on a PostgreSQL 16 sample source with 311 tables, a 64 KB LobMaxSize truncated
a cache table's values (up to about 1.8 million characters), and larger sizes ran a
dms.t3.medium out of memory during change capture. The plan now sizes LobMaxSize from the
collection's measured longest values and raises the replication instance so the buffer fits.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from src.contracts.data_movement_plan import DmsSizing
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.dms_sizing import (
    MEASURED,
    lob_plan,
    lob_size_note,
    measured_lob_kb,
    not_moved_columns,
    size_for_lob,
    unmeasured_lob_tables,
)
from src.tools.data_movement.plan import build_step_plans

from .test_plan import _job, _waves
from .test_step1_replication import _prices

# The live source's longest value: stylesheet_cache.source_map, in UTF-8 bytes.
LONGEST = 1_866_112


def _collector(columns, engine="postgresql"):
    return {
        "metadata": {"source_database": {"engine": engine, "version": "16.4", "database_size_gb": 0.5}},
        "database_schema": {"tables": [{"table_id": "public.cache", "table_name": "cache", "columns": columns}]},
        "queries": {"query_patterns": [{"query_id": "w", "query_type": "INSERT", "calls_per_second": 1.0}]},
    }  # fmt: skip


def _c(name, data_type, measured=None):
    out = {"column_name": name, "data_type": data_type}
    if measured is not None:
        out["max_value_bytes"] = measured
    return out


def _sizing(cls="dms.t3.medium", allocated=50, included=50):
    return DmsSizing(row="small", instance_class=cls, allocated_storage_gb=allocated,
                     included_storage_gb=included, database_gb=0.5, writes_per_second=1.0,
                     reason="0.5 GB and 1 writes per second.")  # fmt: skip


def test_a_measured_postgresql_lob_runs_at_its_measured_size_with_headroom():
    # 1,866,112 bytes at 2 bytes per byte is 3645 KB; 1.25 times is 4557 KB; rounded up to 1 MB
    # steps, 5120 KB.
    size, [t] = lob_plan(_collector([_c("id", "int4"), _c("source_map", "text", LONGEST),
                                     _c("content", "text", 899_939)]), ["public.cache"], "postgresql")  # fmt: skip
    assert size == 5120 and (t.mode, t.max_size_kb) == ("limited", 5120)
    assert t.reason.startswith(MEASURED) and f"{LONGEST} bytes (source_map)" in t.reason
    assert dms_sizing().lob.measured_why in t.reason


def test_a_small_measured_value_runs_at_the_catalogs_size_not_below():
    size, [t] = lob_plan(_collector([_c("raw", "text", 7803)]), ["public.cache"], "postgresql")
    assert size == dms_sizing().lob.undeclared_max_kb == t.max_size_kb
    assert t.reason.startswith(MEASURED)


def test_a_measured_bytea_counts_one_byte_per_byte():
    # 3,000,000 bytes times 1.25 is 3663 KB, so 4096 KB; as text it would be 8192 KB.
    assert measured_lob_kb(3_000_000, "bytea", "postgresql") == 4096
    assert measured_lob_kb(3_000_000, "text", "postgresql") == 8192


def test_a_measured_size_never_exceeds_dms_maximum():
    assert measured_lob_kb(200_000_000, "text", "postgresql") == 102400


def test_a_partly_measured_table_runs_at_the_catalogs_size_and_is_named():
    collector = _collector([_c("a", "text", LONGEST), _c("b", "jsonb")])
    size, [t] = lob_plan(collector, ["public.cache"], "postgresql")
    assert size == dms_sizing().lob.undeclared_max_kb and not t.reason.startswith(MEASURED)
    assert "did not measure" in t.reason
    assert unmeasured_lob_tables([t], "postgresql") == ["public.cache"]
    note = lob_size_note(["public.cache"], 5120)
    assert (
        note is not None
        and "did not measure (public.cache)" in note
        and "5120 KB LobMaxSize is not checked against it" in note
        and "collect-postgresql.sql" in note
    )


def test_an_old_collection_without_measurements_falls_back_with_a_finding():
    collector = _collector([_c("id", "int4"), _c("body", "text")])
    size, [t] = lob_plan(collector, ["public.cache"], "postgresql")
    assert size == dms_sizing().lob.undeclared_max_kb
    assert unmeasured_lob_tables([t], "postgresql") == ["public.cache"]
    assert lob_size_note([], 64) is None


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_uses_measured_sizes_and_sizes_the_class_for_them(dialect):
    # #2123: a MySQL source is sized like a PostgreSQL one.
    collector = _collector([_c("body", "longtext", 10)], engine=dialect)
    _, [t] = lob_plan(collector, ["public.cache"], dialect)
    assert (t.mode, t.max_size_kb) == ("limited", dms_sizing().lob.undeclared_max_kb)
    assert t.reason.startswith(MEASURED) and unmeasured_lob_tables([t], dialect) == []
    assert size_for_lob(_sizing(), 5120, dialect).instance_class == "dms.r6i.large"


def test_the_class_is_raised_so_the_lob_buffer_fits():
    s = size_for_lob(_sizing(), 5120, "postgresql")
    assert s.instance_class == "dms.r6i.large" and s.included_storage_gb == 100
    assert s.allocated_storage_gb == 100
    assert (
        "LobMaxSize of 5120 KB needs dms.r6i.large" in s.reason
        and "raised from dms.t3.medium" in s.reason
    )
    assert s.reason.startswith(_sizing().reason)


def test_the_class_stays_when_it_holds_the_lob_buffer():
    s = size_for_lob(_sizing(), 64, "postgresql")
    assert s.instance_class == "dms.t3.medium" and "fits dms.t3.medium" in s.reason
    big = size_for_lob(_sizing("dms.r6i.xlarge", 100, 100), 5120, "postgresql")
    assert big.instance_class == "dms.r6i.xlarge"


def test_a_buffer_above_every_class_takes_the_largest_with_a_note():
    s = size_for_lob(_sizing(), 30000, "postgresql")
    assert s.instance_class == dms_sizing().lob.instance_classes[-1].instance_class
    assert s.note is not None and "customer" in s.note and "30000 KB" in s.note


def test_the_catalog_classes_grow_in_memory_and_buffer():
    classes = dms_sizing().lob.instance_classes
    assert [c.instance_class for c in classes][:2] == ["dms.t3.medium", "dms.r6i.large"]
    assert classes[0].max_lob_kb == dms_sizing().lob.undeclared_max_kb


@pytest.mark.parametrize(
    ("data_type", "dialect", "flagged"),
    [("tsvector", "postgresql", True), ("_tsvector", "postgresql", True), ("tsvector[]", "postgresql", True),
     ("tsquery", "postgresql", False), ("text", "postgresql", False), ("tsvector", "mysql", False)],
)  # fmt: skip
def test_not_moved_columns_are_the_catalogs_types(data_type, dialect, flagged):
    assert not_moved_columns([_c("s", data_type)], dialect) == (["s"] if flagged else [])


def _pg_job(measured: int | None):
    inputs = _job(waves=_waves(["aurora_postgresql"]), source_engine="postgresql")
    collector = copy.deepcopy(dict(inputs.collector))
    collector["metadata"]["source_database"].update(version="16.4", database_size_gb=0.5)
    collector["queries"] = {
        "query_patterns": [{"query_id": "w", "query_type": "INSERT", "calls_per_second": 1.0}]
    }
    for t in collector["database_schema"]["tables"]:
        for c in t["columns"]:
            c.setdefault("data_type", "int4")
        if t["table_id"] == "shop.orders":
            t["columns"].append(_c("blob", "text", measured))
    return dataclasses.replace(inputs, collector=collector)


def test_the_plan_sizes_the_class_and_cost_from_the_measured_lob():
    hourly = {"dms.t3.medium": 0.149, "dms.r6i.large": 0.353}
    prices = _prices(instance_hourly={"dms.t3.medium": 0.0745, "dms.r6i.large": 0.176},
                     multi_az_instance_hourly=hourly, storage_price_per_gb_month=0.115,
                     multi_az_storage_price_per_gb_month=0.23)  # fmt: skip
    small = build_step_plans(_pg_job(7803), prices=prices)[0][0]
    big = build_step_plans(_pg_job(LONGEST), prices=prices)[0][0]
    assert small.replication is not None and big.replication is not None
    assert small.replication.sizing.instance_class == "dms.t3.medium"
    assert (big.replication.sizing.instance_class, big.replication.lob_max_size_kb) == (
        "dms.r6i.large",
        5120,
    )
    assert "raised from dms.t3.medium" in big.replication.sizing.reason
    assert small.monthly_cost_usd is not None and big.monthly_cost_usd is not None
    assert big.monthly_cost_usd > small.monthly_cost_usd
    assert not any("did not measure" in n for n in big.notes)


def test_the_plan_says_when_the_collection_did_not_measure():
    plan = build_step_plans(_pg_job(None), prices=None)[0][0]
    assert (
        plan.replication is not None and plan.replication.sizing.instance_class == "dms.t3.medium"
    )
    assert any("did not measure" in n and "shop.orders" in n for n in plan.notes)


def test_a_tsvector_table_is_left_out_of_the_lob_sizing():
    inputs = _pg_job(LONGEST)
    for t in inputs.collector["database_schema"]["tables"]:
        if t["table_id"] == "shop.orders":
            t["columns"].append(_c("search", "tsvector"))
    rep = build_step_plans(inputs, prices=None)[0][0].replication
    assert rep is not None and "shop.orders" not in [t.table for t in rep.lob_tables]
    assert rep.sizing.instance_class == "dms.t3.medium"
