# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The names PostgreSQL chooses itself, checked on a real server (#2547).

``pg_implicit_names`` predicts the primary key index and identity sequence names each
CREATE TABLE of the draft creates. The draft's whole script runs on a PostgreSQL started
on a Unix socket in a temp folder (no TCP listener), and the server's own names must be
the predicted ones, long and multi-byte names included, and no CREATE INDEX may fail. The
tests skip when ``pgserver`` is not installed.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.tools.schema.aurora_common.ddl_generator import pg_implicit_names
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from tests.pg_server import PgServerUnavailable, start_pg_server

pgserver = pytest.importorskip("pgserver")


def _table(name: str, columns: list[str], *, auto: str | None = None, pk: bool = True,
           indexes: tuple[tuple[str, str], ...] = ()) -> AgentTable:  # fmt: skip
    return AgentTable(
        table_id=f"app.{name}",
        table_name=name,
        row_count=1,
        columns=[
            AgentColumn(column_name=c, data_type="integer", nullable=c != auto,
                        is_auto_increment=c == auto)  # fmt: skip
            for c in columns
        ],
        primary_key=[columns[0]] if pk else None,
        indexes=[AgentIndex(index_name=n, columns=[c], is_unique=False) for n, c in indexes],
    )


_LONG_TABLE = "é" * 40  # 80 bytes
_LONG_COLUMN = "c" * 50

# Every collected index name here is one PostgreSQL gives a key or a sequence.
_TABLES = [
    _table("orders_pkey", ["x"], pk=False),  # takes orders' key name: it becomes orders_pkey1
    _table("orders", ["id", "a"], auto="id"),
    _table(_LONG_TABLE, [_LONG_COLUMN, "a"], auto=_LONG_COLUMN),
    _table("lines", ["a", "b", "d"], pk=False,
           indexes=(("orders_pkey1", "a"), ("orders_id_seq", "b"),
                    ("é" * 29 + "_pkey", "d"))),  # fmt: skip
]


@pytest.fixture(scope="module")
def server():
    try:
        srv = start_pg_server()
    except PgServerUnavailable as exc:  # the one failure that skips
        pytest.skip(f"no pgserver PostgreSQL here: {exc}")
    yield srv
    srv.cleanup()


def _relations(server, kinds: str) -> set[str]:
    out = server.psql(
        "\\set QUIET on\n\\t on\n\\a\nSELECT relname FROM pg_class WHERE relnamespace = "
        f"'public'::regnamespace AND relkind IN ({kinds});\n"
    )
    return {ln for ln in out.splitlines() if ln.strip()}


def test_the_server_names_keys_and_sequences_as_predicted_and_every_index_is_created(server):
    base = AuroraDesignBase(engine="aurora_postgresql", job_id="j", source_database="app",
                            source_engine="mysql", tables=list(_TABLES))  # fmt: skip
    design = merge_design_delta(base, {"delta_version": "1.0"}).output
    assert design is not None
    # ON_ERROR_STOP: a CREATE INDEX that clashes fails the psql call, never passes.
    server.psql("\\set ON_ERROR_STOP on\n" + design["generated_ddl"])

    predicted = pg_implicit_names(base.tables)
    assert {n.name for n in predicted} == _relations(server, "'S'") | (
        _relations(server, "'i'") - {i.index_name for t in base.tables for i in t.indexes or []}
    )
    assert {n.name for n in predicted} >= {
        "orders_pkey1",
        "orders_id_seq",
        "é" * 29 + "_pkey",
        "é" * 14 + "_" + "c" * 29 + "_seq",
    }
    assert len(base.renamed) == 3
