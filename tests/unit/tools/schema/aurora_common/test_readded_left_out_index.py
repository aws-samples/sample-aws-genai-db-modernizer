# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A left-out index the designer re-adds under a new name says so in every deliverable (#2571).

The draft leaves an index out when its name cannot be quoted or another index of its table
has it (#2402). Since #2416 the designer can re-add it through ``add_indexes`` under a new
name. When the re-added index is on the same table and columns, the merge's left-out note
says the schema design re-adds it as the new name. The engineering report and the results
page print that note's reason from ``facts.design_left_out``, the design's DDL comment is
written from the note, and step 1's DDL file reads the saved design's note, so all of them
say the same thing.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.report.engineering_report import render_engineering_report_md
from src.shared.left_out_objects import left_out_objects, readded_indexes
from src.shared.report_facts import compute_report_facts
from src.tools.data_movement.dms_step import load_step1, render_files
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from tests.unit.report.test_design_left_out_deliverables import _plain
from tests.unit.tools.data_movement.dms_step_fixtures import write_pg_job

PG, MY = "aurora_postgresql", "aurora_mysql"
DUPLICATE = (
    "the table already has an index named ix, so this one was not created; "
    "create it under another name if the queries need it"
)
READDED = f"{DUPLICATE}; the schema design re-adds it as ix_b on the same columns"


def _table(name: str, indexes: list[AgentIndex] | None = None) -> AgentTable:
    return AgentTable(
        table_id=f"app.{name}",
        table_name=name,
        row_count=10,
        columns=[AgentColumn(column_name=c, data_type="int", nullable=True) for c in "abc"],
        indexes=indexes,
    )


def _base(engine: str) -> AuroraDesignBase:
    source = "postgresql" if engine == PG else "mysql"
    ix = [AgentIndex(index_name="ix", columns=["a"], is_unique=False),
          AgentIndex(index_name="ix", columns=["b", "c"], is_unique=False)]  # fmt: skip
    return AuroraDesignBase(engine=engine, job_id="j", source_database="app",
                            source_engine=source, tables=[_table("t", ix), _table("u")])  # fmt: skip


def _merge(engine: str, table: str, name: str, columns: list[str]) -> dict:
    delta = {"delta_version": "1.0",
             "tables": [{"table_name": table,
                         "add_indexes": [{"index_name": name, "columns": columns}]}]}  # fmt: skip
    result = merge_design_delta(_base(engine), delta, strict=False)
    assert result.errors == []
    assert result.output is not None
    return dict(result.output)


def _left_out_note(output: dict) -> dict[str, str]:
    notes: list[dict[str, str]] = output["app_layer_notes"]
    [note] = [n for n in notes if n["feature"] == "duplicate_index_name"]
    return note


@pytest.mark.parametrize("engine", [PG, MY])
def test_a_readded_index_rewords_its_left_out_note(engine: str) -> None:
    output = _merge(engine, "t", "ix_b", ["b", "c"])
    assert _left_out_note(output) == {
        "feature": "duplicate_index_name",
        "source_object": "t.ix",
        "recommendation": f"Index not created: {READDED}.",
    }
    assert f"-- Not created: t.ix: {READDED}." in output["generated_ddl"].splitlines()
    assert readded_indexes(output["app_layer_notes"]) == {"t.ix": "ix_b"}


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("table", "columns"),
    [("t", ["b"]), ("t", ["c", "b"]), ("t", ["a"]), ("u", ["b", "c"])],
    ids=["fewer columns", "other order", "the kept index's columns", "another table"],
)
def test_an_index_on_other_columns_or_another_table_leaves_the_note_as_it_was(
    table: str, columns: list[str]
) -> None:
    output = _merge(MY, table, "ix_b", columns)
    assert _left_out_note(output)["recommendation"] == f"Index not created: {DUPLICATE}."
    assert readded_indexes(output["app_layer_notes"]) == {}


def test_a_note_that_says_nothing_about_a_readd_maps_to_nothing() -> None:
    notes = [{"feature": "duplicate_index_name", "source_object": "t.ix",
              "recommendation": f"Index not created: {DUPLICATE}."},
             {"feature": "index_renamed", "source_object": "t.ix",
              "recommendation": "Created as t_a_idx: another index has the name."}]  # fmt: skip
    assert readded_indexes(notes) == {}
    assert readded_indexes(None) == {}


def test_the_facts_and_the_engineering_report_say_it_is_readded() -> None:
    output = _merge(MY, "t", "ix_b", ["b", "c"])
    report = _plain()
    design = report["schema_designs"]["aurora_mysql"]
    design["app_layer_notes"] = [*design["app_layer_notes"], *output["app_layer_notes"]]
    report["facts"] = compute_report_facts(report)
    [entry] = report["facts"]["design_left_out"]
    [obj] = entry["objects"]
    assert obj["reason"] == f"{READDED}."
    assert obj == left_out_objects(output["app_layer_notes"])[0]
    md = render_engineering_report_md(report)
    # md_text escapes the underscore; the sentence is the fact's, word for word.
    assert (
        "- Duplicate index name on index `t.ix`: the table already has an index named ix, "
        "so this one was not created; create it under another name if the queries need it; "
        "the schema design re-adds it as ix\\_b on the same columns."
    ) in md.splitlines()


def _step1_left_out_lines(text: str) -> list[str]:
    head = text.split("Source objects not created", 1)
    if len(head) == 1:
        return []
    entries: list[str] = []
    for ln in head[1].splitlines()[1:]:
        if ln.startswith("-- - "):
            entries.append(ln[5:])
        elif not entries:
            continue  # the heading's own wrapped line
        elif ln.startswith("-- ") and ln.strip() != "--":
            entries[-1] += " " + ln[3:]
        else:
            break
    return entries


PAIR = [{"index_name": "orders_note", "columns": ["note"], "is_unique": False},
        {"index_name": "orders_note", "columns": ["id", "note"], "is_unique": False}]  # fmt: skip
STEP1_REASON = (
    "the table already has an index named orders_note, so this one was not created; "
    "create it under another name if the queries need it"
)


def _readd(name: str, columns: list[str]) -> dict:
    return {"delta_version": "1.0",
            "tables": [{"table_name": "orders",
                        "add_indexes": [{"index_name": name, "columns": columns}]}]}  # fmt: skip


def test_step1_says_the_schema_design_readds_the_index(tmp_path) -> None:
    store = write_pg_job(tmp_path, indexes={"orders": PAIR}, design=["orders", "tags"],
                         design_delta=_readd("orders_note_2", ["id", "note"]))  # fmt: skip
    [line] = _step1_left_out_lines(
        render_files(load_step1(store, "shop", "job1"))["step-1.ddl.sql"]
    )
    assert line == (
        f"index public.orders.orders_note: {STEP1_REASON}; the schema design re-adds it as "
        "orders_note_2 on the same columns."
    )


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "delta", [None, _readd("orders_note_2", ["note", "id"])], ids=["no re-add", "other columns"]
)
def test_step1_keeps_the_reason_when_the_design_readds_nothing_on_those_columns(
    tmp_path, delta
) -> None:
    store = write_pg_job(tmp_path, indexes={"orders": PAIR}, design=["orders", "tags"],
                         design_delta=delta)  # fmt: skip
    [line] = _step1_left_out_lines(
        render_files(load_step1(store, "shop", "job1"))["step-1.ddl.sql"]
    )
    assert line == f"index public.orders.orders_note: {STEP1_REASON}."
