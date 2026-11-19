# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table's indexes reach the draft with one name each (#2402).

A collector can report two indexes of one table under the same name. One table cannot
hold both: Aurora MySQL refuses a second key of a name (error 1061, names compared
without case), Aurora PostgreSQL a second relation of a name (compared as written, since
the draft quotes it), and the merge pairs a table's statements with its index names.
``quotable_tables`` keeps the first and leaves each later one out with a
``DdlResult.left_out`` record, so the draft, the merge, a saved design's check and step 1
read the same indexes.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.tools.schema.aurora_common.ddl_generator import MYSQL, POSTGRES, quotable_tables
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta


def _reason(kept: str) -> str:
    # Spelled out, not imported: a test that cannot import fails on main for a missing
    # name, not for the behaviour it checks (``./ci/pre_merge.sh``).
    return (
        f"the table already has an index named {kept}, so this one was not created; "
        "create it under another name if the queries need it"
    )


def _table(name: str, indexes: list[AgentIndex], text: str = "int") -> AgentTable:
    return AgentTable(
        table_id=f"app.{name}",
        table_name=name,
        row_count=10,
        columns=[
            AgentColumn(column_name="a", data_type=text, nullable=True),
            AgentColumn(column_name="b", data_type="varchar", max_length=753, nullable=True),
        ],
        indexes=indexes,
    )


def _index(name: str, columns: list[str]) -> AgentIndex:
    return AgentIndex(index_name=name, columns=columns, is_unique=False)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("dialect", [MYSQL, POSTGRES], ids=["mysql", "postgres"])
def test_a_second_index_of_one_name_is_left_out_and_the_first_kept(dialect) -> None:
    """Both engines: the merge keys a table's indexes by name, so an exact copy goes."""
    first, second = _index("ix", ["a", "b"]), _index("ix", ["a"])
    [table], left_out = quotable_tables([_table("t", [first, second])], dialect, "mysql")
    assert table.indexes == [first]
    assert left_out == [{"table": "t", "kind": "index", "name": "ix", "reason": _reason("ix"),
                         "feature": "duplicate_index_name"}]  # fmt: skip


@pytest.mark.passes_on_main
def test_on_aurora_mysql_names_that_differ_only_in_case_are_one_name() -> None:
    first, second = _index("Ix_A", ["a"]), _index("ix_a", ["b"])
    [table], left_out = quotable_tables([_table("t", [first, second])], MYSQL, "mysql")
    assert table.indexes == [first]
    assert [(o["name"], o["reason"]) for o in left_out] == [("ix_a", _reason("Ix_A"))]


@pytest.mark.passes_on_main
def test_on_aurora_postgresql_names_that_differ_only_in_case_are_two_indexes() -> None:
    """The draft quotes every name on Aurora PostgreSQL: "Ix_A" and "ix_a" are two
    relations, and both are created."""
    first, second = _index("Ix_A", ["a"]), _index("ix_a", ["b"])
    [table], left_out = quotable_tables([_table("t", [first, second])], POSTGRES, "postgresql")
    assert table.indexes == [first, second] and left_out == []


def _created(engine: str, indexes: list[AgentIndex]) -> list[str]:
    base = AuroraDesignBase(engine=engine, job_id="j", source_database="d",
                            source_engine="mysql", tables=[_table("t", indexes)])  # fmt: skip
    design = merge_design_delta(base, {"delta_version": "1.0"}, strict=False).output
    assert design is not None
    created: list[str] = design["table_definitions"][0]["indexes"]
    return created


def test_the_merged_aurora_mysql_design_folds_case() -> None:
    assert len(_created("aurora_mysql", [_index("Ix_A", ["a"]), _index("ix_a", ["b"])])) == 1


@pytest.mark.passes_on_main  # main created both on Aurora PostgreSQL; this keeps that
def test_the_merged_aurora_postgresql_design_keeps_both_case_variants() -> None:
    assert _created("aurora_postgresql", [_index("Ix_A", ["a"]), _index("ix_a", ["b"])]) == [
        'CREATE INDEX "Ix_A" ON "t" ("a");',
        'CREATE INDEX "ix_a" ON "t" ("b");',
    ]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("dialect", [MYSQL, POSTGRES], ids=["mysql", "postgres"])
def test_every_later_copy_is_left_out(dialect) -> None:
    indexes = [
        _index("ix", ["a"]),
        _index("other", ["b"]),
        _index("IX", ["b"]),
        _index("ix", ["a", "b"]),
    ]
    [table], left_out = quotable_tables([_table("t", indexes)], dialect, "mysql")
    if dialect is MYSQL:
        assert [i.index_name for i in table.indexes or []] == ["ix", "other"]
        assert [o["name"] for o in left_out] == ["IX", "ix"]
    else:
        assert [i.index_name for i in table.indexes or []] == ["ix", "other", "IX"]
        assert [o["name"] for o in left_out] == ["ix"]


@pytest.mark.passes_on_main
def test_distinct_names_and_one_name_on_two_tables_are_kept() -> None:
    """Only a name repeated within one table is left out."""
    one = _table("t1", [_index("ix", ["a"]), _index("ix2", ["b"])])
    two = _table("t2", [_index("ix", ["a"])])
    tables, left_out = quotable_tables([one, two], MYSQL, "mysql")
    assert tables == [one, two] and left_out == []


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
@pytest.mark.parametrize("skipped_first", [True, False], ids=["skipped-first", "created-first"])
def test_the_merged_design_creates_one_index_per_name(engine: str, skipped_first: bool) -> None:
    """The issue's shape: on Aurora MySQL the wide index is skipped with a comment, the
    narrow one created. Only the first survives, with its own comment or statement, and
    the other becomes an app-layer note."""
    wide, narrow = _index("ix", ["a", "b"]), _index("ix", ["a"])
    pair = [wide, narrow] if skipped_first else [narrow, wide]
    base = AuroraDesignBase(engine=engine, job_id="j", source_database="d",
                            source_engine="mysql", tables=[_table("t", pair, text="text")])  # fmt: skip
    design = merge_design_delta(base, {"delta_version": "1.0"}, strict=False).output
    assert design is not None
    ddl = design["generated_ddl"]
    statements = design["table_definitions"][0]["indexes"]
    comments = [line for line in ddl.split("\n") if line.startswith("-- Index t.ix ")]
    if engine == "aurora_mysql" and skipped_first:
        assert statements == [] and len(comments) == 1
    else:
        assert len(statements) == 1 and comments == []
    [note] = [n for n in design["app_layer_notes"] if n["feature"] == "duplicate_index_name"]
    assert note == {"feature": "duplicate_index_name", "source_object": "t.ix",
                    "recommendation": f"Index not created: {_reason('ix')}."}  # fmt: skip
    assert "draft" not in note["recommendation"]
