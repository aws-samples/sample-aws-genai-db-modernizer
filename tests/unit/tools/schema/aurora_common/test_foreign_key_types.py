# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A foreign key column gets exactly the type of the column it references (#2239).

Aurora MySQL refuses ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`` when the two
columns' types are incompatible (ERROR 3780: a TEXT column, or another integer or decimal
type), so its draft types the child column as its parent, and the parent column of a
foreign key is a key column itself: a lengthless UNIQUE ``nvarchar`` gets a key-sized type,
not LONGTEXT. Aurora PostgreSQL accepts those pairs, so its draft keeps the source's types.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    generate_mysql_ddl,
    generate_pg_ddl,
)

PG = "aurora_postgresql"
MY = "aurora_mysql"


def _col(name: str, data_type: str, normalized: str, **extra) -> dict:
    return {
        "column_name": name,
        "data_type": data_type,
        "normalized_data_type": normalized,
        "nullable": extra.pop("nullable", True),
        **extra,
    }


def _table(name: str, columns: list[dict], **extra) -> AgentTable:
    return AgentTable.model_validate(
        {"table_id": f"db.{name}", "table_name": name, "row_count": 1, "columns": columns, **extra}
    )


def _fk(name: str, columns: list[str], table: str, referenced: list[str], **extra) -> dict:
    return {
        "constraint_name": name,
        "columns": columns,
        "referenced_table": table,
        "referenced_columns": referenced,
        **extra,
    }


def _generate(target: str, tables: list[AgentTable], engine: str = "sqlserver") -> DdlResult:
    return (generate_pg_ddl if target == PG else generate_mysql_ddl)(tables, source_engine=engine)


def _types(result: DdlResult) -> dict[tuple[str, str], str]:
    return {(t.table_name, c.name): c.aurora_type for t in result.tables for c in t.columns}


def _issue_tables() -> list[AgentTable]:
    """The issue's case: a parent with a lengthless UNIQUE nvarchar that is not its primary
    key, and a child whose foreign key references it (a trimmed collection, or a DDL
    upload with bare types)."""
    parent = _table(
        "p",
        [
            _col("id", "int", "integer", nullable=False),
            _col("code", "nvarchar", "string", nullable=False),
            _col("name", "nvarchar", "string", max_length=50, nullable=False),
        ],
        primary_key=["id"],
        indexes=[{"index_name": "ux_code", "columns": ["code"], "is_unique": True}],
    )
    child = _table(
        "c",
        [
            _col("id", "int", "integer", nullable=False),
            _col("code", "nvarchar", "string"),
            _col("name", "nvarchar", "string"),
        ],
        primary_key=["id"],
        foreign_keys=[
            _fk("fk_c_code", ["code"], "p", ["code"]),
            _fk("fk_c_name", ["name"], "p", ["name"]),
        ],
    )
    return [parent, child]


def test_a_lengthless_unique_parent_column_and_its_child_get_one_key_type():
    result = _generate(MY, _issue_tables())
    types = _types(result)
    assert types[("p", "code")] == types[("c", "code")] == "VARCHAR(255)"
    # Both are still flagged: the length is a guess.
    flagged = {(r["table"], r["column"]) for r in result.residuals}
    assert {("p", "code"), ("c", "code")} <= flagged
    # The parent's UNIQUE index keeps its UNIQUE, with no key prefix.
    assert "CREATE UNIQUE INDEX `ux_code` ON `p` (`code`);" in result.full_ddl
    assert not [n for n in result.index_notes if n["index"] == "ux_code"]


@pytest.mark.passes_on_main
def test_a_string_child_keeps_its_own_length():
    """MySQL accepts string columns of different lengths in a foreign key, so the
    lengthless child keeps VARCHAR(255) next to its VARCHAR(50) parent, still flagged."""
    result = _generate(MY, _issue_tables())
    types = _types(result)
    assert (types[("p", "name")], types[("c", "name")]) == ("VARCHAR(50)", "VARCHAR(255)")
    assert not [n for n in result.column_notes if n["kind"] == "foreign_key_type"]
    assert ("c", "name") in {(r["table"], r["column"]) for r in result.residuals}


def _pg(name: str, data_type: str, **extra) -> dict:
    normalized = {"integer": "integer", "bigint": "integer", "smallint": "integer"}
    return _col(name, data_type, normalized.get(data_type, "string"), **extra)


@pytest.mark.passes_on_main
def test_the_reviewers_composite_index_still_creates():
    """PR #2617 review: copying the parent's VARCHAR(700) onto the child put the child's
    UNIQUE (code, other) index at 5,200 bytes (ERROR 1071). The child keeps VARCHAR(100),
    and the index renders whole and UNIQUE, as on main."""
    parent = _table(
        "p", [_pg("code", "character varying", max_length=700, nullable=False)],
        primary_key=["code"],
    )  # fmt: skip
    child = _table(
        "c",
        [
            _pg("id", "integer", nullable=False),
            _pg("code", "character varying", max_length=100),
            _pg("other", "character varying", max_length=600),
        ],
        primary_key=["id"],
        indexes=[{"index_name": "ux_c", "columns": ["code", "other"], "is_unique": True}],
        foreign_keys=[_fk("fk_c_p", ["code"], "p", ["code"])],
    )
    result = _generate(MY, [parent, child], "postgresql")
    types = _types(result)
    assert (types[("c", "code")], types[("c", "other")]) == ("VARCHAR(100)", "VARCHAR(600)")
    assert "CREATE UNIQUE INDEX `ux_c` ON `c` (`code`, `other`);" in result.full_ddl
    assert not [n for n in result.index_notes if n["index"] == "ux_c"]
    assert not [n for n in result.column_notes if n["kind"] == "foreign_key_type"]


@pytest.mark.parametrize(
    ("child_type", "parent_type", "says"),
    [
        ("TEXT", "VARCHAR(50)", "cannot put a TEXT or BLOB column in a foreign key"),
        ("VARBINARY(36)", "VARCHAR(36)", "strings of one kind"),
        ("NVARCHAR(36)", "VARCHAR(36)", "same character set"),
    ],
)
def test_a_child_of_another_string_kind_takes_the_parents_type(child_type, parent_type, says):
    """A model's override can give the child a type MySQL refuses next to the parent's: the
    child takes the parent's type, and the note says what MySQL requires."""
    from src.tools.schema.aurora_common.ddl_generator import TypeOverride

    parent = _table(
        "p", [_col("k", "nvarchar", "string", max_length=36, nullable=False)], primary_key=["k"]
    )
    child = _table(
        "c",
        [
            _col("id", "int", "integer", nullable=False),
            _col("k", "nvarchar", "string", max_length=36),
        ],
        primary_key=["id"],
        foreign_keys=[_fk("fk_c_p", ["k"], "p", ["k"])],
    )
    overrides = {("p", "k"): TypeOverride(parent_type), ("c", "k"): TypeOverride(child_type)}
    result = generate_mysql_ddl([parent, child], overrides, source_engine="sqlserver")
    assert _types(result)[("c", "k")] == parent_type
    (note,) = [n for n in result.column_notes if n["kind"] == "foreign_key_type"]
    assert (
        says in note["reason"] and "ERROR 3780" in note["reason"] and child_type in note["reason"]
    )


def test_numeric_child_columns_take_the_parents_precision():
    parent = _table(
        "account",
        [_col("no", "decimal", "decimal", numeric_precision=10, numeric_scale=0, nullable=False)],
        primary_key=["no"],
    )
    child = _table(
        "entry",
        [
            _col("id", "int", "integer", nullable=False),
            _col("account_no", "decimal", "decimal", numeric_precision=12, numeric_scale=0),
        ],
        primary_key=["id"],
        foreign_keys=[_fk("fk_entry_account", ["account_no"], "account", ["no"])],
    )
    types = _types(_generate(MY, [parent, child]))
    assert types[("account", "no")] == types[("entry", "account_no")] == "DECIMAL(10,0)"


def test_a_chain_of_keys_and_a_composite_key_align_per_column():
    """PostgreSQL integers of three sizes: each child column takes the size of the column
    it references, along a chain and per position of a composite key, whatever the order
    of the tables. The string column of the key keeps its own length."""
    region = _table(
        "region",
        [_pg("country", "smallint", nullable=False), _pg("code", "integer", nullable=False),
         _pg("name", "character varying", max_length=20, nullable=False)],
        primary_key=["country", "code", "name"],
    )  # fmt: skip
    store = _table(
        "store",
        [_pg("id", "integer", nullable=False), _pg("country", "integer"),
         _pg("region", "bigint"), _pg("name", "character varying", max_length=40)],
        primary_key=["id"],
        indexes=[{"index_name": "ux_store", "columns": ["country", "region", "name"],
                  "is_unique": True}],
        foreign_keys=[_fk("fk_store_region", ["country", "region", "name"], "region",
                          ["country", "code", "name"])],
    )  # fmt: skip
    # Listed before its parent: the order of the tables does not matter.
    shelf = _table(
        "shelf",
        [_pg("id", "integer", nullable=False), _pg("country", "bigint"),
         _pg("region", "smallint"), _pg("name", "character varying", max_length=60)],
        primary_key=["id"],
        foreign_keys=[_fk("fk_shelf_store", ["country", "region", "name"], "store",
                          ["country", "region", "name"])],
    )  # fmt: skip
    result = _generate(MY, [shelf, store, region], "postgresql")
    types = _types(result)
    for child in ("store", "shelf"):
        assert (types[(child, "country")], types[(child, "region")]) == ("SMALLINT", "INT")
    assert (types[("store", "name")], types[("shelf", "name")]) == ("VARCHAR(40)", "VARCHAR(60)")
    notes = [n for n in result.column_notes if n["kind"] == "foreign_key_type"]
    assert {(n["table"], n["column"]) for n in notes} == {
        ("store", "country"),
        ("store", "region"),
        ("shelf", "country"),
        ("shelf", "region"),
    }
    assert all("the same size and sign" in n["reason"] for n in notes)


def test_a_key_into_the_same_table_takes_its_own_tables_type():
    node = _table(
        "node",
        [_pg("id", "integer", nullable=False), _pg("parent_id", "bigint")],
        primary_key=["id"],
        foreign_keys=[_fk("fk_node_parent", ["parent_id"], "node", ["id"])],
    )
    types = _types(_generate(MY, [node], "postgresql"))
    assert types[("node", "parent_id")] == types[("node", "id")] == "INT"


def test_each_schemas_child_follows_the_table_its_key_names():
    """Two schemas with a table of one name (#2455): each child takes the type of the
    table in the schema its key references."""

    def parent(schema: str, data_type: str) -> AgentTable:
        return _table(
            "item",
            [_pg("sku", data_type, nullable=False)],
            primary_key=["sku"],
            schema_name=schema,
            table_id=f"db.{schema}.item",
        )

    child = _table(
        "line",
        [_pg("id", "integer", nullable=False), _pg("sales_sku", "bigint"),
         _pg("stock_sku", "integer")],
        primary_key=["id"],
        schema_name="sales",
        table_id="db.sales.line",
        foreign_keys=[
            _fk("fk_line_sales", ["sales_sku"], "item", ["sku"], referenced_schema="sales"),
            _fk("fk_line_stock", ["stock_sku"], "item", ["sku"], referenced_schema="stock"),
        ],
    )  # fmt: skip
    result = generate_mysql_ddl(
        [parent("sales", "integer"), parent("stock", "bigint"), child],
        source_engine="postgresql",
        qualified=True,
    )
    types = _types(result)
    assert types[("sales.line", "sales_sku")] == "INT"
    assert types[("sales.line", "stock_sku")] == "BIGINT"


@pytest.mark.passes_on_main
@pytest.mark.parametrize("target", [PG, MY])
def test_a_key_to_a_table_outside_the_design_and_matching_types_change_nothing(target):
    child = _table(
        "c",
        [
            _col("id", "int", "integer", nullable=False),
            _col("code", "nvarchar", "string", max_length=20),
            _col("other", "nvarchar", "string", max_length=20),
        ],
        primary_key=["id"],
        foreign_keys=[
            _fk("fk_c_missing", ["code"], "not_in_design", ["code"]),
            _fk("fk_c_self", ["other"], "c", ["other"]),
        ],
    )
    result = _generate(target, [child])
    types = _types(result)
    assert (types[("c", "code")], types[("c", "other")]) == ("VARCHAR(20)", "VARCHAR(20)")
    assert not [n for n in result.column_notes if n["kind"] == "foreign_key_type"]


@pytest.mark.passes_on_main
def test_the_postgresql_draft_keeps_the_sources_types():
    """A PostgreSQL bigint referencing an integer is legal on Aurora PostgreSQL, and a
    homogeneous source carries over as it is."""
    parent = _table("users", [_col("id", "integer", "integer", nullable=False)], primary_key=["id"])
    child = _table(
        "votes",
        [
            _col("id", "bigint", "integer", nullable=False),
            _col("user_id", "bigint", "integer"),
        ],
        primary_key=["id"],
        foreign_keys=[_fk("fk_votes_user", ["user_id"], "users", ["id"])],
    )
    result = _generate(PG, [parent, child], "postgresql")
    types = _types(result)
    assert (types[("users", "id")], types[("votes", "user_id")]) == ("INTEGER", "BIGINT")
    assert not [n for n in result.column_notes if n["kind"] == "foreign_key_type"]


def test_a_postgresql_bigint_referencing_an_integer_gets_int_on_aurora_mysql():
    """The discourse sample's case: on main its Aurora MySQL draft had nine such keys, and
    each failed with ERROR 3780."""
    parent = _table("users", [_col("id", "integer", "integer", nullable=False)], primary_key=["id"])
    child = _table(
        "votes",
        [
            _col("id", "bigint", "integer", nullable=False),
            _col("user_id", "bigint", "integer"),
        ],
        primary_key=["id"],
        foreign_keys=[_fk("fk_votes_user", ["user_id"], "users", ["id"])],
    )
    types = _types(_generate(MY, [parent, child], "postgresql"))
    assert types[("users", "id")] == types[("votes", "user_id")] == "INT"
