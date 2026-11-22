# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 and the saved design give each Aurora PostgreSQL index one name (#2545).

PostgreSQL index names are unique within a schema, so the design base renames a collected
index whose name another table's index (or a table) already has (#2410). Step 1 builds its
base from every collector table, while a design covers only the engine's tables, so the two
could pick different names for one index. With a saved design, step 1 now gives each index
of the design's tables the design's name, and renames another table's index instead when
the two clash. Every index step 1 creates under another name is listed in
``step-1.ddl.sql`` with its collected and its new name.
"""

from __future__ import annotations

import re

import pytest

from src.tools.data_movement.dms_step import load_step1, render_ddl, write_step1_delivery
from tests.unit.tools.transformation_contract.conftest import col, table

from .dms_step_fixtures import write_pg_job


def _ix(name: str, *columns: str) -> dict:
    return {"index_name": name, "columns": list(columns), "is_unique": False, "index_type": "btree"}


def _design(*tables: str) -> list[str]:
    """The tables a saved design covers (``write_pg_job`` builds it from them)."""
    return list(tables)


_CREATE = re.compile(r'^CREATE INDEX "([^"]+)" ON "([^"]+)"', re.M)


def _created(ddl: str) -> dict[str, str]:
    """table -> the name of its one index, as step 1's DDL creates it."""
    return {table: name for name, table in _CREATE.findall(ddl)}


def _renamed_lines(text: str) -> list[str]:
    head = text.split("Indexes created under another name", 1)
    if len(head) == 1:
        return []
    entries: list[str] = []
    for ln in head[1].splitlines()[1:]:
        if ln.startswith("-- - "):
            entries.append(ln[5:])
        elif entries and ln.startswith("-- ") and ln.strip() != "--":
            entries[-1] += " " + ln[3:]
        else:
            break
    return entries


def test_step1_keeps_the_designs_name_where_its_own_base_would_rename_it(tmp_path):
    """The design covers orders and tags, not audit_log: it keeps orders' ix_note. On main,
    step 1 saw audit_log first and renamed orders' index instead."""
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_note", "msg")], "orders": [_ix("ix_note", "note")]},
        design=_design("orders", "tags"),
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"orders": "ix_note", "audit_log": "audit_log_msg_idx"}
    assert [(r.table_id, r.name, r.new_name, r.design_name) for r in inputs.renamed_indexes] == [
        ("public.audit_log", "ix_note", "audit_log_msg_idx", None)
    ]


def test_step1_takes_the_designs_new_name_before_another_tables_index_has_it(tmp_path):
    """The design renames tags' ix_x to tags_label_idx. audit_log, outside the design, has an
    index of that very name: step 1 renames audit_log's, not the design's."""
    store = write_pg_job(
        tmp_path,
        indexes={
            "audit_log": [_ix("tags_label_idx", "msg")],
            "orders": [_ix("ix_x", "note")],
            "tags": [_ix("ix_x", "label")],
        },
        design=_design("orders", "tags"),
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {
        "audit_log": "audit_log_msg_idx",
        "orders": "ix_x",
        "tags": "tags_label_idx",
    }
    assert sorted((r.table_id, r.name, r.new_name) for r in inputs.renamed_indexes) == [
        ("public.audit_log", "tags_label_idx", "audit_log_msg_idx"),
        ("public.tags", "ix_x", "tags_label_idx"),
    ]


def test_without_a_design_step1_names_the_indexes_as_before_and_lists_the_rename(tmp_path):
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_note", "msg")], "orders": [_ix("ix_note", "note")]},
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"audit_log": "ix_note", "orders": "orders_note_idx"}
    lines = _renamed_lines(render_ddl(inputs))
    assert len(lines) == 1
    assert lines[0].startswith("index public.orders.ix_note is created as orders_note_idx: ")
    assert "audit_log already has an index named ix_note in this schema" in lines[0]
    assert "saved schema design" not in lines[0]


def test_the_ddl_file_lists_each_renamed_index_with_its_old_and_new_name(tmp_path):
    store = write_pg_job(
        tmp_path,
        indexes={
            "audit_log": [_ix("tags_label_idx", "msg")],
            "orders": [_ix("ix_x", "note")],
            "tags": [_ix("ix_x", "label")],
        },
        design=_design("orders", "tags"),
    )
    lines = _renamed_lines(render_ddl(load_step1(store, "shop", "job1")))

    assert [ln.split(":", 1)[0] for ln in lines] == [
        "index public.audit_log.tags_label_idx is created as audit_log_msg_idx",
        "index public.tags.ix_x is created as tags_label_idx",
    ]
    assert all("use the new name wherever a script names this index" in ln for ln in lines)


def test_an_index_whose_design_name_step1_cannot_keep_says_the_design_names_it_otherwise(
    tmp_path,
):
    """The design (orders, tags) keeps orders' index audit_log; step 1 also creates the
    table audit_log, so it must rename the index and says the design's name differs."""
    store = write_pg_job(
        tmp_path, indexes={"orders": [_ix("audit_log", "note")]}, design=_design("orders", "tags")
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"orders": "orders_note_idx"}
    assert [(r.name, r.new_name, r.design_name) for r in inputs.renamed_indexes] == [
        ("audit_log", "orders_note_idx", "audit_log")
    ]
    (line,) = _renamed_lines(render_ddl(inputs))
    assert line.startswith("index public.orders.audit_log is created as orders_note_idx: ")
    assert "the schema already has a table named audit_log" in line
    assert line.endswith("The saved schema design names it audit_log.")


def test_no_rename_means_no_list_and_no_count(tmp_path):
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_msg", "msg")], "orders": [_ix("ix_note", "note")]},
        design=_design("orders", "tags"),
    )
    inputs = load_step1(store, "shop", "job1")

    assert inputs.renamed_indexes == ()
    assert "Indexes created under another name" not in render_ddl(inputs)
    assert write_step1_delivery(store, "shop", "job1")["indexes_renamed"] == 0


def test_the_delivery_counts_the_renamed_indexes(tmp_path):
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_note", "msg")], "orders": [_ix("ix_note", "note")]},
    )

    assert write_step1_delivery(store, "shop", "job1")["indexes_renamed"] == 1


@pytest.mark.passes_on_main
def test_a_skipped_design_leaves_step1_naming_as_without_one(tmp_path):
    """A skip record (``status: skipped``) has no tables: nothing to share names with."""
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_note", "msg")], "orders": [_ix("ix_note", "note")]},
        design=[],
    )

    assert _created(load_step1(store, "shop", "job1").ddl) == {
        "audit_log": "ix_note",
        "orders": "orders_note_idx",
    }


# ---------------------------------------------------------------------------
# The names the saved design stores (#2545 review)
# ---------------------------------------------------------------------------


def test_a_design_saved_before_2548_keeps_its_names_and_step1_says_where_it_cannot(tmp_path):
    """A design saved before #2548 kept ix_note on both orders and tags. Step 1 creates
    orders' under that name, cannot create tags' under it, and says the design names it
    ix_note."""
    store = write_pg_job(
        tmp_path,
        indexes={"orders": [_ix("ix_note", "note")], "tags": [_ix("ix_note", "label")]},
        design=_design("orders", "tags"),
        design_before_2548=True,
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"orders": "ix_note", "tags": "tags_label_idx"}
    assert [(r.table_id, r.name, r.new_name, r.design_name) for r in inputs.renamed_indexes] == [
        ("public.tags", "ix_note", "tags_label_idx", "ix_note")
    ]
    (line,) = _renamed_lines(render_ddl(inputs))
    assert line.endswith("The saved schema design names it ix_note.")


def test_an_index_the_designs_delta_renamed_gets_the_designs_stored_name(tmp_path):
    """The model's delta replaced orders' ix_note with orders_by_note on the same column: the
    design's DDL names it orders_by_note, and so does step 1."""
    delta = {"delta_version": "1.0", "tables": [{
        "table_name": "orders", "remove_indexes": ["ix_note"],
        "add_indexes": [{"index_name": "orders_by_note", "columns": ["note"]}]}]}  # fmt: skip
    store = write_pg_job(
        tmp_path,
        indexes={"orders": [_ix("ix_note", "note")]},
        design=_design("orders", "tags"),
        design_delta=delta,
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"orders": "orders_by_note"}
    assert [(r.name, r.new_name, r.design_name) for r in inputs.renamed_indexes] == [
        ("ix_note", "orders_by_note", None)
    ]
    (line,) = _renamed_lines(render_ddl(inputs))
    assert "the saved schema design creates it under this name" in line


def _sales_orders(index: str) -> dict:
    t: dict = table("sales", "orders", [col("id", "int8", 1), col("note", "text", 2)], pk=["id"])
    return {**t, "indexes": [_ix(index, "note")]}


def _created_by_schema(ddl: str) -> dict[str, str]:
    """``schema.table`` -> the name of its one index, in a DDL of several schemas."""
    found = re.findall(r'^CREATE INDEX "([^"]+)" ON "([^"]+)"\."([^"]+)"', ddl, re.M)
    return {f"{schema}.{t}": name for name, schema, t in found}


def test_a_design_of_two_schemas_names_each_schemas_table_by_its_schema(tmp_path):
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_note", "msg")], "orders": [_ix("ix_note", "note")]},
        extra_tables=[_sales_orders("ix_note")],
        design=["public.orders", "sales.orders"],
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created_by_schema(inputs.ddl) == {
        "public.audit_log": "audit_log_msg_idx",
        "public.orders": "ix_note",
        "sales.orders": "ix_note",
    }


def test_a_one_schema_design_of_a_two_schema_source_finds_its_tables_schema(tmp_path):
    """The design (public's orders and tags) names its tables without a schema, and sales
    has an orders too: tags, only in public, says the design's orders is public's."""
    store = write_pg_job(
        tmp_path,
        indexes={
            "audit_log": [_ix("ix_note", "msg")],
            "orders": [_ix("ix_note", "note")],
            "tags": [_ix("ix_tag", "label")],
        },
        extra_tables=[_sales_orders("ix_tag")],
        design=["public.orders", "public.tags"],
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created_by_schema(inputs.ddl) == {
        "public.audit_log": "audit_log_msg_idx",
        "public.orders": "ix_note",
        "public.tags": "ix_tag",
        "sales.orders": "ix_tag",
    }


def test_a_design_table_two_schemas_share_with_nothing_to_tell_them_apart_names_both(tmp_path):
    """The design holds public's orders only and names no schema: either orders could be
    it, so step 1 gives each the design's name for the index the design stores."""
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("ix_a", "msg")], "orders": [_ix("ix_a", "note")]},
        extra_tables=[_sales_orders("ix_a")],
        design=["public.orders"],
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created_by_schema(inputs.ddl) == {
        "public.audit_log": "audit_log_msg_idx",
        "public.orders": "ix_a",
        "sales.orders": "ix_a",
    }


def test_a_hostile_index_name_stays_inside_the_header_comment(tmp_path):
    hostile = "ix */ DROP TABLE orders; --"
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix(hostile, "msg")], "orders": [_ix(hostile, "note")]},
    )
    text = render_ddl(load_step1(store, "shop", "job1"))

    (line,) = _renamed_lines(text)
    assert line.startswith(f"index public.orders.{hostile} is created as orders_note_idx: ")
    header = text.split("\nCREATE SCHEMA", 1)[0]
    assert all(ln.startswith("--") or not ln.strip() for ln in header.splitlines())
    # The name reaches SQL only quoted, as the CREATE INDEX of each table.
    assert not [ln for ln in text.splitlines() if ln.lstrip().upper().startswith("DROP")]
    assert f'CREATE INDEX "{hostile}" ON "audit_log" ("msg");' in text


def test_a_stored_name_postgresql_gives_a_primary_key_index_is_renamed_and_said(tmp_path):
    """A design saved before #2547 kept audit_log's index orders_pkey, the name PostgreSQL
    gives orders' primary key index: step 1 cannot create it under that name, and says
    the design names it so."""
    store = write_pg_job(
        tmp_path,
        indexes={"audit_log": [_ix("orders_pkey", "msg")]},
        design=_design("audit_log", "orders"),
        design_before_2548=True,
    )
    inputs = load_step1(store, "shop", "job1")

    assert _created(inputs.ddl) == {"audit_log": "audit_log_msg_idx"}
    assert [(r.name, r.new_name, r.design_name) for r in inputs.renamed_indexes] == [
        ("orders_pkey", "audit_log_msg_idx", "orders_pkey")
    ]
    (line,) = _renamed_lines(render_ddl(inputs))
    assert "primary key index of orders" in line
    assert line.endswith("The saved schema design names it orders_pkey.")
