# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A collected name holding a control, format, surrogate or line separator character is
never quoted into SQL (#2381 review 2).

Doubling the quote mark keeps a name inside its quotes for the server, but ``psql -f``
ends a line at a NUL: a quoted name holding one stays open, and the next statement's
collector text runs as SQL. ``Dialect.q`` (both dialects) and every other helper that
quotes a collected name raise ``UnsafeIdentifierError`` for such a name, and the Aurora
DDL generator leaves its object out with a fixed reason (``DdlResult.left_out``). This
covers main's own plain table, column, index and foreign key paths too.
"""

from __future__ import annotations

import unicodedata

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentForeignKey, AgentIndex, AgentTable
from src.shared.sql_identifiers import UNSAFE_NAME_REASON, UnsafeIdentifierError
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    POSTGRES,
    generate_mysql_ddl,
    generate_pg_ddl,
)

# One of each refused category: Cc (NUL, VT, NEL), Cf (bidi override, zero width, BOM,
# Arabic letter mark), Cs (a lone surrogate, which JSON can carry), Zl, Zp.
UNSAFE = [
    "\x00",
    "\x0b",
    "\x85",
    "\u202e",
    "\u2066",
    "\u200b",
    "\ufeff",
    "\u061c",
    "\ud800",
    "\u2028",
    "\u2029",
]
IDS = [f"U+{ord(c):04X}" for c in UNSAFE]
GENERATORS = [generate_pg_ddl, generate_mysql_ddl]


def _unprintable(text: str) -> list[str]:
    return [
        ch
        for ch in text
        if ch != "\n" and unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
    ]


def _table(name="orders", columns=("id", "note"), pk=("id",), indexes=(), fks=()):
    return AgentTable(
        table_id=f"app.{name}",
        table_name=name,
        row_count=10,
        columns=[AgentColumn(column_name=c, data_type="integer", nullable=True) for c in columns],
        primary_key=list(pk),
        indexes=list(indexes),
        foreign_keys=list(fks) or None,
    )


def _index(name, columns):
    return AgentIndex(index_name=name, columns=list(columns), is_unique=False)


@pytest.mark.parametrize("dialect", [POSTGRES, MYSQL], ids=["postgres", "mysql"])
@pytest.mark.parametrize("bad", UNSAFE, ids=IDS)
def test_quoting_refuses_an_unsafe_name_in_both_dialects(dialect, bad):
    with pytest.raises(UnsafeIdentifierError):
        dialect.q(f"a{bad} tail")


@pytest.mark.passes_on_main  # the doubled quote mark main already writes must stay
def test_quoting_still_doubles_the_mark_and_keeps_a_printable_name():
    assert POSTGRES.q('we"ird caf\u00e9\u00a0x') == '"we""ird caf\u00e9\u00a0x"'
    assert MYSQL.q("we`ird") == "`we``ird`"


@pytest.mark.parametrize("generate", GENERATORS, ids=["postgres", "mysql"])
@pytest.mark.parametrize("bad", UNSAFE, ids=IDS)
def test_a_table_with_an_unsafe_name_is_left_out(generate, bad):
    result = generate([_table(f"or{bad}ders", indexes=[_index("ix", ["note"])]), _table("ok")])

    assert not _unprintable(result.full_ddl)
    assert "CREATE TABLE" in result.full_ddl  # the other table is still created
    assert {"table": f"or{bad}ders", "kind": "table", "name": f"or{bad}ders",
            "reason": UNSAFE_NAME_REASON} in result.left_out  # fmt: skip


@pytest.mark.parametrize("generate", GENERATORS, ids=["postgres", "mysql"])
@pytest.mark.parametrize("bad", UNSAFE, ids=IDS)
def test_a_column_with_an_unsafe_name_is_left_out_and_the_rest_created(generate, bad):
    result = generate([_table(columns=("id", f"no{bad}te", "body"))])

    assert not _unprintable(result.full_ddl)
    [table] = result.tables
    assert [c.name for c in table.columns] == ["id", "body"]
    assert [(o["kind"], o["name"]) for o in result.left_out] == [("column", f"no{bad}te")]


@pytest.mark.parametrize("generate", GENERATORS, ids=["postgres", "mysql"])
def test_a_primary_key_on_an_unsafe_column_is_left_out_too(generate):
    result = generate([_table(columns=("i\x00d", "note"), pk=("i\x00d",))])

    assert not _unprintable(result.full_ddl)
    assert "PRIMARY KEY" not in result.full_ddl
    assert ("primary key", "PRIMARY KEY") in [(o["kind"], o["name"]) for o in result.left_out]


@pytest.mark.parametrize("generate", GENERATORS, ids=["postgres", "mysql"])
@pytest.mark.parametrize("bad", UNSAFE, ids=IDS)
def test_a_plain_index_with_an_unsafe_name_is_left_out_with_one_comment(generate, bad):
    result = generate(
        [_table(indexes=[_index(f"a{bad} tail", ["note"]), _index("SELECT 1", ["note"])])]
    )

    assert not _unprintable(result.full_ddl)
    [table] = result.tables
    assert len(table.index_sql) == 2  # one statement per carried index, as delta_merge zips
    assert table.index_sql[0].startswith("-- Index orders.a tail not created: ")
    assert "SELECT 1" in table.index_sql[1] and table.index_sql[1].startswith("CREATE INDEX")
    assert [(o["kind"], o["name"]) for o in result.left_out] == [("index", f"a{bad} tail")]


@pytest.mark.parametrize("generate", GENERATORS, ids=["postgres", "mysql"])
def test_a_foreign_key_with_an_unsafe_name_is_left_out(generate):
    fk = AgentForeignKey(constraint_name="fk\x00x", columns=["note"],
                         referenced_table="ok", referenced_columns=["id"])  # fmt: skip
    result = generate([_table(fks=[fk]), _table("ok")])

    assert not _unprintable(result.full_ddl)
    assert "FOREIGN KEY" not in result.full_ddl
    assert [(o["kind"], o["name"]) for o in result.left_out] == [("foreign key", "fk\x00x")]


# ---------------------------------------------------------------------------
# Every other helper that quotes a collected name into SQL
# ---------------------------------------------------------------------------


def _helpers():
    from src.agents.collector import mysql_routine_create, view_sources
    from src.tools.data_movement import dms_step, lob_sizes, routine_scripts, verify_scripts
    from src.tools.database import mysql_grants
    from src.tools.database.disposable_engine import _quote_table, pg_cast_type
    from src.tools.routines import rewrites
    from src.tools.transformation_contract import duckdb_backend

    return {
        "dms_step._pg_ident": dms_step._pg_ident,
        "lob_sizes._ident": lob_sizes._ident,
        "routine_scripts._pg_ident": routine_scripts._pg_ident,
        "routine_scripts._mysql_ident": routine_scripts._mysql_ident,
        "verify_scripts._pg_ident": verify_scripts._pg_ident,
        "verify_scripts._mysql_ident": verify_scripts._mysql_ident,
        "mysql_grants.grant_database": mysql_grants.grant_database,
        "rewrites._backticked": rewrites._backticked,
        "rewrites._quoted_identifier": rewrites._quoted_identifier,
        "view_sources._pg_ident": view_sources._pg_ident,
        "view_sources._mysql_ident": view_sources._mysql_ident,
        "mysql_routine_create.quote_ident": mysql_routine_create.quote_ident,
        "duckdb_backend.quote_ident": duckdb_backend.quote_ident,
        "disposable_engine._quote_table": lambda n: _quote_table(n, '"'),
        "disposable_engine.pg_cast_type": lambda n: pg_cast_type(f'"{n}"'),
    }


HELPERS = sorted(_helpers())


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize(
    "bad", ["\x00", "\u202e", "\ud800", "\u2028"], ids=["U+0000", "U+202E", "U+D800", "U+2028"]
)
def test_every_quoting_helper_refuses_an_unsafe_name(helper, bad):
    with pytest.raises(ValueError):
        _helpers()[helper](f"a{bad} tail")
