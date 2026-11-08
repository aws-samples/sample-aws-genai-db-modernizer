# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A larger LOB limit only for the tables that need it (#1918).

In a live run on a PostgreSQL 16 sample source with 311 tables (about 100 with LOB columns),
a task-wide 5120 KB LobMaxSize, sized for one cache table, ran a 16 GiB replication instance
out of memory after about 55 minutes of change capture under a heavier write load: limited LOB
mode allocates LobMaxSize per LOB column of every buffered row. The task now runs at the
catalog's size, and a table whose measured values are longer gets its own limit in a
``table-settings`` rule with ``lob-settings`` (mode ``limited``, ``bulk-max-size`` in KB).
"""

from __future__ import annotations

import copy
import dataclasses

from src.contracts.data_movement_plan import LobTable
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.dms_sizing import lob_plan, table_lob_limits, task_lob_kb
from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    bundle_inputs,
    render_ddl,
    table_mappings,
    task_settings,
)
from src.tools.data_movement.plan import build_step_plans

from .dms_step_fixtures import make_bundle
from .test_plan import _job, _waves

# The live source's longest value: stylesheet_cache.source_map, in UTF-8 bytes (5120 KB).
LONGEST = 1_866_112
DEFAULT = dms_sizing().lob.undeclared_max_kb


def _table(table_id, columns, key=("id",)):
    return {"table_id": table_id, "table_name": table_id.split(".")[1], "primary_key": list(key),
            "columns": [{"column_name": "id", "data_type": "int4"}, *columns]}  # fmt: skip


def _collector(*tables):
    return {"metadata": {"source_database": {"engine": "postgresql", "version": "16.4"}},
            "database_schema": {"tables": list(tables)}}  # fmt: skip


def _text(name, measured):
    return {"column_name": name, "data_type": "text", "max_value_bytes": measured}


def _cache(key=("id",)):
    return _table("public.cache", [_text("source_map", LONGEST)], key)


def _small():
    return _table("public.posts", [_text("raw", 7803)])


def test_one_large_table_leaves_the_task_at_the_catalogs_size():
    size, tables = lob_plan(
        _collector(_cache(), _small()), ["public.cache", "public.posts"], "postgresql"
    )
    assert size == DEFAULT
    assert {t.table: t.max_size_kb for t in tables} == {
        "public.cache": 5120,
        "public.posts": DEFAULT,
    }
    assert [t.table for t in table_lob_limits(tables, size)] == ["public.cache"]


def test_when_every_lob_table_needs_more_the_task_takes_the_smallest():
    bigger = _table("public.blobs", [_text("body", 3_000_000)])  # 8192 KB
    size, tables = lob_plan(
        _collector(_cache(), bigger), ["public.cache", "public.blobs"], "postgresql"
    )
    assert size == 5120
    assert [(t.table, t.max_size_kb) for t in table_lob_limits(tables, size)] == [
        ("public.blobs", 8192)
    ]


def test_a_large_table_without_a_primary_key_raises_the_task_size():
    # DMS takes LOB settings only on a table with a primary key, so that table's size stays
    # task-wide.
    size, tables = lob_plan(
        _collector(_cache(key=()), _small()), ["public.cache", "public.posts"], "postgresql"
    )
    assert size == 5120 and table_lob_limits(tables, size) == []


def test_no_lob_table_keeps_the_catalogs_default_and_mysql_keeps_its_rule():
    assert task_lob_kb([]) == dms_sizing().lob.default_max_kb
    tables = [LobTable(table="s.a", mode="limited", columns=["c"], max_size_kb=8),
              LobTable(table="s.b", mode="limited", columns=["c"], max_size_kb=32)]  # fmt: skip
    # MySQL keeps the largest size task-wide, with no table of its own.
    assert task_lob_kb(tables, dialect="mysql") == 32
    # Tables within the catalog's size keep the largest of theirs, as before.
    assert task_lob_kb(tables, dialect="postgresql") == 32


def _per_table_inputs(engine="aurora_postgresql", task_kb=DEFAULT):
    b = make_bundle(engine)
    s = "public" if engine == "aurora_postgresql" else "shop"
    tables = [LobTable(table=f"{s}.customers", mode="limited", columns=["bio"], max_size_kb=5120,
                       reason="Measured: the longest LOB value is 1866112 bytes (bio); the table runs "
                              "in limited LOB mode at 5120 KB."),
              LobTable(table=f"{s}.orders", mode="limited", columns=["note"], max_size_kb=DEFAULT,
                       reason="Measured: the longest LOB value is 7803 bytes (note).")]  # fmt: skip
    rep = b.plan.replication.model_copy(update={"lob_max_size_kb": task_kb, "lob_tables": tables})
    plan = b.plan.model_copy(update={"replication": rep})
    return bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))


def _lob_rules(inputs):
    return [r for r in table_mappings(inputs)["rules"] if r["rule-type"] == "table-settings"]


def test_the_task_mapping_gives_only_the_larger_table_its_own_limited_lob_rule():
    inputs = _per_table_inputs()
    mappings = table_mappings(inputs)["rules"]
    assert task_settings(inputs)["TargetMetadata"]["LobMaxSize"] == DEFAULT
    assert _lob_rules(inputs) == [{
        "rule-type": "table-settings", "rule-id": "4", "rule-name": "4",
        "object-locator": {"schema-name": "public", "table-name": "customers"},
        "lob-settings": {"mode": "limited", "bulk-max-size": "5120"}}]  # fmt: skip
    ids = [r["rule-id"] for r in mappings]
    assert len(ids) == len(set(ids)) and [r["rule-type"] for r in mappings[:3]] == ["selection"] * 3


def test_a_task_at_the_larger_size_gets_no_table_rule_nor_does_mysql():
    assert _lob_rules(_per_table_inputs(task_kb=5120)) == []
    assert _lob_rules(_per_table_inputs("aurora_mysql")) == []


def test_the_ddl_header_names_the_table_with_the_larger_limit_and_why():
    ddl = " ".join(
        line.removeprefix("-- ") for line in render_ddl(_per_table_inputs()).splitlines()
    )
    assert f"limited LOB mode at {DEFAULT} KB (LobMaxSize)" in ddl
    assert "1 table gets a larger limit of its own" in ddl
    assert "public.customers at 5120 KB" in ddl
    assert dms_sizing().lob.table_settings_why in ddl


def _pg_job():
    inputs = _job(waves=_waves(["aurora_postgresql"]), source_engine="postgresql")
    collector = copy.deepcopy(dict(inputs.collector))
    collector["metadata"]["source_database"].update(version="16.4", database_size_gb=0.5)
    collector["queries"] = {
        "query_patterns": [{"query_id": "w", "query_type": "INSERT", "calls_per_second": 1.0}]
    }
    for t in collector["database_schema"]["tables"]:
        for c in t["columns"]:
            c.setdefault("data_type", "int4")
        t["primary_key"] = t.get("primary_key") or ["id"]
        if t["table_id"] == "shop.orders":
            t["columns"].append(_text("blob", LONGEST))
        elif t["table_id"] == "shop.audit":
            t["columns"].append(_text("bio", 7803))
    return dataclasses.replace(inputs, collector=collector)


def test_the_plan_sizes_the_class_for_the_largest_table_limit_and_says_which():
    # A live run (#1918): dms.t3.medium held a 64 KB task with one table at 5120 KB through a
    # heavy write load, where a task-wide 2048 KB had run it out of memory.
    rep = build_step_plans(_pg_job(), prices=None)[0][0].replication
    assert rep is not None and rep.lob_max_size_kb == DEFAULT
    assert rep.sizing.instance_class == "dms.t3.medium"
    assert "shop.orders" in rep.sizing.reason and "5120 KB" in rep.sizing.reason
    assert f"the task's LobMaxSize is {DEFAULT} KB" in rep.sizing.reason
    assert "fits dms.t3.medium (a table's own limit up to 5120 KB)" in rep.sizing.reason


def test_a_table_limit_is_checked_against_the_class_s_table_limit_not_its_task_limit():
    from src.contracts.data_movement_plan import DmsSizing
    from src.tools.data_movement.dms_sizing import size_for_lob

    small = DmsSizing(row="small", instance_class="dms.t3.medium", allocated_storage_gb=50,
                      included_storage_gb=50, database_gb=0.5, writes_per_second=1.0, reason="r.")  # fmt: skip
    one_table = size_for_lob(small, 5120, "postgresql", table="s.cache", task_kb=DEFAULT)
    assert one_table.instance_class == "dms.t3.medium"
    task_wide = size_for_lob(small, 5120, "postgresql")
    assert task_wide.instance_class == "dms.r6i.large"
    bigger = size_for_lob(small, 8192, "postgresql", table="s.cache", task_kb=DEFAULT)
    assert bigger.instance_class == "dms.r6i.large" and "raised from dms.t3.medium" in bigger.reason
    # A task above dms.t3.medium's task-wide limit raises the class even for a small table limit.
    assert (
        size_for_lob(small, 5120, "postgresql", table="s.cache", task_kb=128).instance_class
        == "dms.r6i.large"
    )


def _with_arrays(inputs):
    from src.contracts.lineage_ops import ColumnRef, CopyOp
    from src.contracts.transformation_contract import FieldRule

    def field(table_id, name, target_type):
        return FieldRule(source_column=name, target_column=name, target_type=target_type,
                         op=CopyOp(op="copy", column=ColumnRef(table_id=table_id, column=name)))  # fmt: skip

    rules = []
    for r in inputs.contract.rules:
        if r.target_table == "orders":
            r = r.model_copy(update={"fields": [*r.fields, field(r.source_table, "tags", "VARCHAR(40)[]"),
                                                field(r.source_table, "names", "TEXT[]"),
                                                field(r.source_table, "ids", "INTEGER[]"),
                                                field(r.source_table, "ips", "INET[]")]})  # fmt: skip
        rules.append(r)
    return dataclasses.replace(inputs, contract=inputs.contract.model_copy(update={"rules": rules}))


def test_a_character_array_column_is_validated_as_text_on_both_sides():
    # A live run (#1918): DMS validation failed on a VARCHAR[] column (convert_to has no array
    # form), retried about 40 times a minute and ran the task out of memory.
    rules = [
        r
        for r in table_mappings(_with_arrays(_per_table_inputs()))["rules"]
        if r["rule-type"] == "validation"
    ]
    # The MD5 is cast to a bounded VARCHAR(64), as DMS's own LOB hashes are: a bare md5()
    # returns text, a LOB to DMS, and every non-NULL array was reported mismatched (#2043).
    cast = "CAST(upper(md5(CAST(${column-name} AS text))) AS VARCHAR(64))"
    assert [(r["object-locator"]["table-name"], r["object-locator"]["column-name"]) for r in rules] == [
        ("orders", "names"), ("orders", "tags")]  # fmt: skip
    assert all(r["rule-action"] == "override-validation-function" and r["rule-target"] == "column"
               and r["source-function"] == r["target-function"] == cast for r in rules)  # fmt: skip
    ids = [r["rule-id"] for r in table_mappings(_with_arrays(_per_table_inputs()))["rules"]]
    assert ids == [str(i) for i in range(1, len(ids) + 1)]


def test_mysql_gets_no_array_validation_rule():
    inputs = _with_arrays(_per_table_inputs("aurora_mysql"))
    assert all(r["rule-type"] == "selection" for r in table_mappings(inputs)["rules"])
