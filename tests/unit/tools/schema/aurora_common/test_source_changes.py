# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2289: each Aurora design table compared with its collected table, at synthesis.

An index matched by name and key columns is the same; an extra one is added; one the
design creates under the name an ``index_renamed`` note gives it is renamed; a source
index it does not create is skipped. A column type is the same, changed or, in a
heterogeneous design, translated (never "type changed"). Nullable and default are read
back from the stored CREATE TABLE with the saved-design loader's own checks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    POSTGRES,
    Dialect,
    render_create_table,
    render_index,
)
from src.tools.schema.aurora_common.saved_design import column_clauses, index_shape
from src.tools.schema.aurora_common.source_changes import design_source_changes

REAL_RUN = Path(__file__).parents[4] / "fixtures" / "real_runs" / "wordpress-both" / "job"


def _design(
    dialect: Dialect,
    tables: list[dict[str, Any]],
    strategy: str = "carry_over",
    notes: list[dict] | None = None,
) -> dict[str, Any]:
    """A design as the generator writes it: each table's columns (name, type, clauses),
    primary key and index specs, rendered with the generator's own renderers."""
    creates, index_sql, defs = [], [], []
    for t in tables:
        fragments = [f"{dialect.q(c[0])} {c[1]}{c[2] if len(c) > 2 else ''}" for c in t["cols"]]
        creates.append(
            render_create_table(dialect, t["name"], fragments, [(k, None) for k in t["pk"]])
        )
        statements = [
            render_index(dialect, t["name"], name, cols, unique=unique)
            for name, cols, unique in t.get("indexes", [])
        ]
        index_sql += statements
        defs.append(
            {
                "table_name": t["name"],
                "columns": [{"name": c[0], "aurora_type": c[1]} for c in t["cols"]],
                "primary_key": t["pk"],
                "indexes": statements,
                "foreign_keys": [],
            }
        )
    return {
        "table_definitions": defs,
        "generated_ddl": "\n\n".join(creates + index_sql),
        "migration_strategy": strategy,
        "app_layer_notes": notes or [],
        "source_database": "shop",
    }


def _collector(engine: str, tables: list[dict[str, Any]], **index_extra: Any) -> dict[str, Any]:
    """A collector output that loads through its contract (the comparison drafts it with
    the DDL generator): the ecommerce fixture with ``tables`` in place of its own."""
    from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

    collector = get_ecommerce_collector_output()
    collector["metadata"]["source_database"].update(engine=engine, database_name="shop")
    collector["database_schema"]["tables"] = [
        {
            "table_id": f"shop.{t['name']}",
            "table_name": t["name"],
            "schema_name": "shop",
            "row_count": 0,
            "primary_key": t.get("pk", []),
            "columns": [
                {
                    "column_name": c[0],
                    "data_type": c[1],
                    "nullable": c[0] not in t.get("pk", []),
                    **({"normalized_data_type": c[2]} if len(c) > 2 else {}),
                }
                for c in t["cols"]
            ],
            "indexes": [
                {"index_name": n, "columns": cols, "is_unique": u, "is_primary": p, **index_extra}
                for n, cols, u, p in t.get("indexes", [])
            ]
            + list(t.get("raw_indexes", [])),
        }
        for t in tables
    ]
    collector["database_schema"]["views"] = []
    return collector


ORDERS_SOURCE = {
    "name": "orders",
    "pk": ["id"],
    "cols": [("id", "bigint unsigned"), ("status", "varchar(20)"), ("note", "text"),
             ("legacy", "int")],
    "indexes": [("PRIMARY", ["id"], True, True), ("status", ["status"], False, False),
                ("by_note", ["note"], False, False), ("old_name", ["status", "id"], True, False),
                ("dropped", ["legacy"], False, False)],
}  # fmt: skip
ORDERS_DESIGN = {
    "name": "orders",
    "pk": ["id"],
    "cols": [("id", "BIGINT UNSIGNED", " AUTO_INCREMENT NOT NULL"),
             ("status", "VARCHAR(20)", " NOT NULL DEFAULT 'new'"),
             ("note", "LONGTEXT"), ("created_at", "DATETIME", " DEFAULT CURRENT_TIMESTAMP")],
    "indexes": [("status", ["status"], False), ("by_note", ["status", "note"], False),
                ("new_name", ["status", "id"], True), ("extra", ["created_at"], False)],
}  # fmt: skip
RENAMED = {
    "feature": "index_renamed",
    "source_object": "orders.old_name",
    "recommendation": "Created as new_name: the name is taken; use the new name.",
}


def _orders() -> list[dict[str, Any] | None]:
    design = _design(MYSQL, [ORDERS_DESIGN], notes=[RENAMED])
    changes = design_source_changes("aurora_mysql", design, _collector("mysql", [ORDERS_SOURCE]))
    assert changes is not None
    return changes


def test_each_column_carries_its_change_and_its_clauses_as_stored() -> None:
    (table,) = _orders()
    assert table is not None
    assert table["source_table"] == "shop.orders"
    assert [(c["name"], c["change"]) for c in table["columns"]] == [
        ("id", "same"),
        ("status", "same"),
        ("note", "type_changed"),
        ("created_at", "added"),
        ("legacy", "skipped"),
    ]
    by_name = {c["name"]: c for c in table["columns"]}
    assert by_name["id"] | {} == {
        "name": "id", "aurora_type": "BIGINT UNSIGNED", "source_type": "bigint unsigned",
        "nullable": False, "default": None, "identity": True, "change": "same",
    }  # fmt: skip
    assert (by_name["status"]["nullable"], by_name["status"]["default"]) == (False, "'new'")
    assert (by_name["note"]["nullable"], by_name["note"]["default"]) == (True, None)
    assert by_name["created_at"]["default"] == "CURRENT_TIMESTAMP"
    assert by_name["legacy"]["aurora_type"] is None and by_name["legacy"]["nullable"] is None


def test_each_index_is_matched_by_name_and_columns_and_the_rest_say_why() -> None:
    (table,) = _orders()
    assert table is not None
    rows = [(i["name"], i["change"], i["unique"]) for i in table["indexes"]]
    assert rows == [
        ("status", "same", False),
        ("by_note", "changed", False),
        ("new_name", "renamed", True),
        ("extra", "added", False),
        ("dropped", "skipped", False),
    ]
    by_name = {i["name"]: i for i in table["indexes"]}
    assert by_name["new_name"]["source_name"] == "old_name"
    assert by_name["by_note"]["source_columns"] == ["note"]
    # The primary key's own index is the PRIMARY KEY clause, never a skipped index.
    assert "PRIMARY" not in by_name


def test_a_renamed_index_with_no_note_is_an_added_one_and_its_source_is_skipped() -> None:
    design = _design(MYSQL, [ORDERS_DESIGN])
    (table,) = design_source_changes("aurora_mysql", design, _collector("mysql", [ORDERS_SOURCE]))
    assert table is not None
    changes = {i["name"]: i["change"] for i in table["indexes"]}
    assert changes["new_name"] == "added" and changes["old_name"] == "skipped"


def test_a_heterogeneous_design_translates_only_as_the_deterministic_translation_does() -> None:
    """A type is ``translated`` only when it is the draft's translation of the source type;
    a model's other choice (``NUMERIC(12,4)`` for ``decimal(10,2)``) is ``type_changed``."""
    source = {"name": "orders", "pk": ["id"],
              "cols": [("id", "int"), ("status", "varchar(20)"), ("total", "decimal(10,2)")]}  # fmt: skip
    design = _design(
        POSTGRES,
        [{"name": "orders", "pk": ["id"],
          "cols": [("id", "INTEGER", " NOT NULL"), ("status", "VARCHAR(20)"),
                   ("total", "NUMERIC(12,4)")]}],
        strategy="translate",
    )  # fmt: skip
    (table,) = design_source_changes("aurora_postgresql", design, _collector("mysql", [source]))
    assert [(c["name"], c["change"]) for c in table["columns"]] == [
        ("id", "translated"),
        ("status", "same"),
        ("total", "type_changed"),
    ]


def test_an_override_away_from_the_translation_is_a_type_change() -> None:
    """SQL Server ``money`` translates to ``NUMERIC(19,4)``; a design that makes it TEXT
    changes the type, and never reads as translated."""
    source = {"name": "pay", "pk": ["id"],
              "cols": [("id", "int", "integer"), ("amount", "money", "decimal")]}  # fmt: skip
    collector = _collector("sqlserver", [source])
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.schema_design_input import project_schema_design_tables
    from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl

    drafted = generate_pg_ddl(
        project_schema_design_tables(CollectorOutputContract.model_validate(collector)),
        source_engine="sqlserver",
    ).tables[0]
    id_type, amount_type = (c.aurora_type for c in drafted.columns)
    assert amount_type != "TEXT"
    for aurora, expected in ((amount_type, "translated"), ("TEXT", "type_changed")):
        design = _design(
            POSTGRES,
            [{"name": "pay", "pk": ["id"],
              "cols": [("id", id_type, " NOT NULL"), ("amount", aurora)]}],
            strategy="translate",
        )  # fmt: skip
        (table,) = design_source_changes("aurora_postgresql", design, collector)
        assert table["columns"][1]["change"] == expected


def test_a_carried_over_type_written_the_targets_way_is_the_same() -> None:
    """MySQL 8 drops an integer's display width: ``bigint(20) unsigned`` is carried over as
    ``BIGINT UNSIGNED``, the same type."""
    source = {"name": "t", "pk": ["id"], "cols": [("id", "bigint(20) unsigned")]}
    design = _design(MYSQL, [{"name": "t", "pk": ["id"], "cols": [("id", "BIGINT UNSIGNED")]}])
    (table,) = design_source_changes("aurora_mysql", design, _collector("mysql", [source]))
    assert table is not None and table["columns"][0]["change"] == "same"


def test_same_compares_uniqueness_method_predicate_include_and_prefixes() -> None:
    """An index of the source's name is the same only when it is the draft's statement for
    the source index: a dropped UNIQUE, another access method or INCLUDE list, a lost
    partial-index predicate or another MySQL key prefix is a change, named."""
    source = {"name": "t", "pk": ["id"], "cols": [("id", "bigint"), ("a", "text"), ("b", "text")],
              "indexes": [("u", ["a"], True, False)]}  # fmt: skip
    pg = {"name": "t", "pk": ["id"], "cols": [("id", "BIGINT", " NOT NULL"), ("a", "TEXT"), ("b", "TEXT")]}  # fmt: skip

    def change(engine: str, dialect: Dialect, src: dict, statement: str) -> tuple[str, list[str]]:
        design = _design(dialect, [{**pg, "cols": pg["cols"] if dialect is POSTGRES else
                                    [("id", "BIGINT", " NOT NULL"), ("a", "VARCHAR(255)"), ("b", "TEXT")]}])  # fmt: skip
        design["table_definitions"][0]["indexes"] = [statement]
        design["generated_ddl"] += "\n\n" + statement
        fam = "postgresql" if dialect is POSTGRES else "mysql"
        (table,) = design_source_changes(engine, design, _collector(fam, [src]))
        (row,) = [i for i in table["indexes"] if i["name"] == "u"]
        return row["change"], row["differences"]

    assert change("aurora_postgresql", POSTGRES, source,
                  render_index(POSTGRES, "t", "u", ["a"], unique=True)) == ("same", [])  # fmt: skip
    assert change("aurora_postgresql", POSTGRES, source,
                  render_index(POSTGRES, "t", "u", ["a"])) == ("changed", ["unique"])  # fmt: skip
    gin = {**source, "indexes": [], "raw_indexes": [{"index_name": "u", "columns": ["a"],
           "is_unique": False, "index_type": "gin", "included_columns": None}]}  # fmt: skip
    assert change("aurora_postgresql", POSTGRES, gin,
                  render_index(POSTGRES, "t", "u", ["a"]))[1] == ["method"]  # fmt: skip
    partial = {**source, "indexes": [], "raw_indexes": [{"index_name": "u", "columns": ["a"],
               "is_unique": False, "predicate": "(b IS NOT NULL)"}]}  # fmt: skip
    assert change("aurora_postgresql", POSTGRES, partial,
                  render_index(POSTGRES, "t", "u", ["a"]))[1] == ["where"]  # fmt: skip
    include = {**source, "indexes": [], "raw_indexes": [{"index_name": "u", "columns": ["a"],
               "is_unique": False, "included_columns": ["b"]}]}  # fmt: skip
    assert change("aurora_postgresql", POSTGRES, include,
                  render_index(POSTGRES, "t", "u", ["a"]))[1] == ["include"]  # fmt: skip
    mysql = {"name": "t", "pk": ["id"], "cols": [("id", "bigint"), ("a", "varchar(255)"), ("b", "text")],
             "indexes": [], "raw_indexes": [{"index_name": "u", "columns": ["a"], "is_unique": False,
             "prefix_lengths": [10]}]}  # fmt: skip
    assert change("aurora_mysql", MYSQL, mysql,
                  render_index(MYSQL, "t", "u", ["a"], prefix_lengths=[20]))[1] == ["prefixes"]  # fmt: skip
    assert change("aurora_mysql", MYSQL, mysql,
                  render_index(MYSQL, "t", "u", ["a"], prefix_lengths=[10]))[0] == "same"  # fmt: skip


def test_a_reordered_key_is_a_change_only_when_the_collection_gives_key_positions() -> None:
    """A collection written before #1927 lists a key's columns in no known order, so a
    design key in another order is not claimed as a change; one that gives the positions
    (``key_order_known``) is."""
    source = {"name": "t", "pk": ["id"], "cols": [("id", "bigint"), ("a", "text"), ("b", "text")],
              "indexes": [("k", ["a", "b"], False, False)]}  # fmt: skip
    design = _design(POSTGRES, [{"name": "t", "pk": ["id"],
                     "cols": [("id", "BIGINT", " NOT NULL"), ("a", "TEXT"), ("b", "TEXT")],
                     "indexes": [("k", ["b", "a"], False)]}])  # fmt: skip
    for extra, expected in (({}, "same"), ({"key_order_known": False}, "same"),
                            ({"key_order_known": True}, "changed")):  # fmt: skip
        (table,) = design_source_changes(
            "aurora_postgresql", design, _collector("postgresql", [source], **extra)
        )
        assert table["indexes"][0]["change"] == expected, extra


def test_an_expression_index_step1_recreates_says_so_and_a_dropped_one_gives_its_reason() -> None:
    """The draft leaves every expression index out with a note; step 1 recreates the ones
    its grammar accepts on Aurora PostgreSQL from PostgreSQL (#2381). Neither is a bare
    "not created": the first reads ``step1``, the other carries the draft's reason."""
    expr = {"index_name": "lower_a", "columns": [], "is_unique": False,
            "has_expressions": True, "index_expressions": "lower(a)",
            "index_def": "CREATE INDEX lower_a ON shop.t USING btree (lower(a))"}  # fmt: skip
    source = {"name": "t", "pk": ["id"], "cols": [("id", "bigint"), ("a", "text")],
              "indexes": [], "raw_indexes": [expr]}  # fmt: skip
    design = _design(POSTGRES, [{"name": "t", "pk": ["id"],
                     "cols": [("id", "BIGINT", " NOT NULL"), ("a", "TEXT")]}])  # fmt: skip
    (table,) = design_source_changes(
        "aurora_postgresql", design, _collector("postgresql", [source])
    )
    (row,) = table["indexes"]
    assert (row["name"], row["change"], row["reason"]) == ("lower_a", "step1", None)
    # From MySQL, step 1 recreates none: the row says why the draft left it out.
    mysql = _design(MYSQL, [{"name": "t", "pk": ["id"],
                    "cols": [("id", "BIGINT", " NOT NULL"), ("a", "TEXT")]}])  # fmt: skip
    (table,) = design_source_changes("aurora_mysql", mysql, _collector("mysql", [source]))
    (row,) = table["indexes"]
    assert row["change"] == "skipped"
    assert row["reason"] and row["reason"].startswith("Source index has expression keys")


def test_an_index_the_design_drops_with_a_note_carries_the_notes_reason() -> None:
    note = {"feature": "duplicate_index_name", "source_object": "orders.dropped",
            "recommendation": "Index not created: the table already has an index named dropped."}  # fmt: skip
    design = _design(MYSQL, [ORDERS_DESIGN], notes=[RENAMED, note])
    (table,) = design_source_changes("aurora_mysql", design, _collector("mysql", [ORDERS_SOURCE]))
    (row,) = [i for i in table["indexes"] if i["name"] == "dropped"]
    assert (row["change"], row["reason"]) == ("skipped", note["recommendation"])


def test_a_table_definition_with_no_name_is_not_compared_and_raises_nothing() -> None:
    design = _design(MYSQL, [ORDERS_DESIGN])
    design["table_definitions"].append({"columns": []})
    changes = design_source_changes("aurora_mysql", design, _collector("mysql", [ORDERS_SOURCE]))
    assert changes is not None and changes[-1] == {"uncompared": "no_name"}


def test_a_design_table_with_no_source_table_is_added_with_all_it_holds() -> None:
    design = _design(
        MYSQL, [{"name": "audit", "pk": ["id"], "cols": [("id", "INT")],
                 "indexes": [("by_id", ["id"], False)]}]
    )  # fmt: skip
    (table,) = design_source_changes("aurora_mysql", design, _collector("mysql", [ORDERS_SOURCE]))
    assert table is not None
    assert table["source_table"] is None
    assert [c["change"] for c in table["columns"]] == ["added"]
    assert [i["change"] for i in table["indexes"]] == ["added"]


def test_two_schemas_tables_of_one_name_and_no_schema_named_are_not_compared() -> None:
    collector = _collector("postgresql", [ORDERS_SOURCE, ORDERS_SOURCE])
    collector["database_schema"]["tables"][0]["schema_name"] = "sales"
    collector["database_schema"]["tables"][1]["schema_name"] = "archive"
    design = _design(POSTGRES, [{"name": "orders", "pk": ["id"], "cols": [("id", "BIGINT")]}])
    assert design_source_changes("aurora_postgresql", design, collector) == [
        {"uncompared": "ambiguous_source"}
    ]


def test_nothing_is_compared_without_collected_tables() -> None:
    design = _design(MYSQL, [ORDERS_DESIGN])
    assert design_source_changes("aurora_mysql", design, None) is None
    assert (
        design_source_changes("aurora_mysql", design, {"database_schema": {"tables": []}}) is None
    )


def test_clauses_are_read_only_from_a_script_the_loader_accepts() -> None:
    design = _design(MYSQL, [ORDERS_DESIGN])
    assert column_clauses(design, "aurora_mysql") is not None
    edited = copy.deepcopy(design)
    edited["generated_ddl"] = edited["generated_ddl"].replace("DEFAULT 'new'", "DEFAULT (SELECT 1)")
    assert column_clauses(edited, "aurora_mysql") is None
    # So the comparison still runs, with no clause read.
    (table,) = design_source_changes("aurora_mysql", edited, _collector("mysql", [ORDERS_SOURCE]))
    assert table is not None
    assert {c["nullable"] for c in table["columns"]} == {None}
    assert (
        column_clauses({"table_definitions": design["table_definitions"]}, "aurora_mysql") is None
    )


def test_a_primary_key_column_with_no_not_null_is_still_not_nullable() -> None:
    design = _design(POSTGRES, [{"name": "t", "pk": ["id"], "cols": [("id", "BIGINT")]}])
    assert column_clauses(design, "aurora_postgresql") == [
        [{"nullable": False, "default": None, "identity": False}]
    ]


def test_index_shape_reads_every_part_on_both_engines() -> None:
    mysql = render_index(MYSQL, "t", "u", ["a", "b"], unique=True, prefix_lengths=[10, None])
    assert index_shape("aurora_mysql", mysql) == {
        "name": "u", "columns": ["a", "b"], "prefixes": [10, None], "unique": True,
        "method": "btree", "include": [], "where": None,
    }  # fmt: skip
    pg = render_index(POSTGRES, "t", "i", ["a"], method="gin", include=["b"])
    shape = index_shape("aurora_postgresql", pg)
    assert shape is not None
    assert (shape["method"], shape["include"], shape["unique"]) == ("gin", ["b"], False)
    assert index_shape("aurora_postgresql", "DROP TABLE t;") is None
    assert index_shape("dynamodb", pg) is None


def test_the_real_wordpress_design_reads_every_table() -> None:
    """The saved real run's Aurora MySQL design: every table is compared and every column
    has its clauses, and the carried-over types all read as the same."""
    design = json.loads(
        (REAL_RUN / "schema-aurora_mysql" / "v2" / "schema_output.json").read_text()
    )
    collector = json.loads((REAL_RUN / "collector" / "output.json").read_text())
    changes = design_source_changes("aurora_mysql", design, collector)
    assert changes is not None and all(t is not None for t in changes)
    columns = [c for t in changes if t for c in t["columns"]]
    assert columns and all(c["nullable"] is not None for c in columns)
    assert {c["change"] for c in columns} == {"same"}


def test_a_translate_design_with_no_draft_is_not_compared_rather_than_changed() -> None:
    """With no deterministic draft (a collection that does not load through its contract),
    no type can be told translated from changed: each table is not compared, with that
    reason, and never reads as a list of type changes."""
    source = {"name": "orders", "pk": ["id"], "cols": [("id", "int"), ("total", "decimal(10,2)")]}
    design = _design(
        POSTGRES,
        [{"name": "orders", "pk": ["id"],
          "cols": [("id", "INTEGER", " NOT NULL"), ("total", "NUMERIC(10,2)")]}],
        strategy="translate",
    )  # fmt: skip
    collector = _collector("mysql", [source])
    collector["contract_version"] = "99.0"  # a MAJOR this code does not read
    assert design_source_changes("aurora_postgresql", design, collector) == [
        {"uncompared": "no_draft"}
    ]
    # A carry-over design needs no translation, so it is still compared.
    carry = _design(MYSQL, [{"name": "orders", "pk": ["id"],
                    "cols": [("id", "INT", " NOT NULL"), ("total", "DECIMAL(10,2)")]}])  # fmt: skip
    (table,) = design_source_changes("aurora_mysql", carry, collector)
    assert [c["change"] for c in table["columns"]] == ["same", "same"]


def test_step1_is_checked_over_the_types_step1_declares() -> None:
    """Step 1 declares each column as the saved design types it; an expression index on a
    column the design makes text is checked over that type, as step 1 checks it: a text
    cast on a text column (``lower((code)::text)``) is one the grammar drops, and on an
    integer column one it refuses."""
    expr = {"index_name": "lower_code", "columns": [], "is_unique": False,
            "has_expressions": True, "index_expressions": "lower((code)::text)",
            "index_def": "CREATE INDEX lower_code ON shop.t USING btree (lower((code)::text))"}  # fmt: skip
    source = {"name": "t", "pk": ["id"], "cols": [("id", "bigint"), ("code", "integer")],
              "indexes": [], "raw_indexes": [expr]}  # fmt: skip

    def change(code_type: str) -> str:
        design = _design(POSTGRES, [{"name": "t", "pk": ["id"],
                         "cols": [("id", "BIGINT", " NOT NULL"), ("code", code_type)]}])  # fmt: skip
        (table,) = design_source_changes(
            "aurora_postgresql", design, _collector("postgresql", [source])
        )
        return str(table["indexes"][0]["change"])

    assert change("TEXT") == "step1"
    assert change("INTEGER") == "skipped"
