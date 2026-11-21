# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aurora PostgreSQL reserves the names PostgreSQL chooses itself (#2547).

A CREATE TABLE with a PRIMARY KEY creates an index PostgreSQL names ``<table>_pkey``, and
an identity column a sequence it names ``<table>_<column>_seq`` (``ChooseRelationName``:
each part cut so the name fits 63 bytes, ``pkey1``, ``seq1`` when the name is taken). They
share the schema's namespace with the indexes, and the script creates every table before
any index, so a collected index of one of those names fails with ``relation already
exists``. The design base reserves them before it chooses index names (#2410) and renames
a clashing index with an ``index_renamed`` note. On a PostgreSQL to Aurora PostgreSQL move
it reserves the names of the expression indexes step 1 recreates too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.ddl_generator import pg_implicit_names, pg_relation_name
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

JOB, DB = "job-2547", "app"
SEQ = "nextval('orders_id_seq'::regclass)"


def _col(
    name: str, *, auto: bool = False, default: str | None = None, nullable: bool = True
) -> AgentColumn:
    return AgentColumn(column_name=name, data_type="integer", nullable=nullable,
                       default_value=default, is_auto_increment=auto)  # fmt: skip


def _table(
    name: str,
    columns: list[AgentColumn] | None = None,
    *,
    pk: list[str] | None = None,
    indexes: list[AgentIndex] | None = None,
) -> AgentTable:
    return AgentTable(
        table_id=f"{DB}.{name}",
        table_name=name,
        row_count=10,
        columns=columns or [_col("a"), _col("b")],
        primary_key=pk,
        indexes=indexes or [],
    )


def _index(name: str, columns: list[str]) -> AgentIndex:
    return AgentIndex(index_name=name, columns=columns, is_unique=False)


def _expression(name: str, table: str) -> AgentIndex:
    return AgentIndex(index_name=name, columns=[], is_unique=False, has_expressions=True,
                      index_expressions="lower(a)",
                      index_def=f"CREATE INDEX {name} ON public.{table} USING btree (lower(a))")  # fmt: skip


def _orders(**kw) -> AgentTable:
    """``orders`` with an identity key ``id``: PostgreSQL creates ``orders_id_seq`` and
    ``orders_pkey``."""
    return _table("orders", [_col("id", auto=True, nullable=False), _col("a")], pk=["id"], **kw)


def _base(tables: list[AgentTable], source: str = "mysql", engine: str = "aurora_postgresql"):
    return AuroraDesignBase(engine=engine, job_id=JOB, source_database=DB,
                            source_engine=source, tables=tables)  # fmt: skip


def _design(tables: list[AgentTable], source: str = "mysql", engine: str = "aurora_postgresql"):
    output = merge_design_delta(_base(tables, source, engine), {"delta_version": "1.0"}).output
    assert output is not None
    return output


_CREATE = re.compile(r'^CREATE (?:UNIQUE )?INDEX "((?:[^"]|"")+)" ON (\S+)', re.M)


def _created(design: dict) -> list[str]:
    return [n for n, _ in _CREATE.findall(design["generated_ddl"])]


def _renames(design: dict) -> list[dict]:
    return [n for n in design["app_layer_notes"] if n["feature"] == "index_renamed"]


# ---------------------------------------------------------------------------
# The names PostgreSQL chooses
# ---------------------------------------------------------------------------


def test_the_names_follow_postgresqls_pattern_and_63_byte_cut() -> None:
    assert pg_relation_name("orders", None, "pkey") == "orders_pkey"
    assert pg_relation_name("orders", "id", "seq") == "orders_id_seq"
    # One part: cut to 63 bytes with its label.
    assert pg_relation_name("t" * 70, None, "pkey") == "t" * 58 + "_pkey"
    # Two parts: the longer is cut first, and the second part on a tie.
    assert pg_relation_name("t" * 40, "c" * 40, "seq") == "t" * 29 + "_" + "c" * 29 + "_seq"
    assert pg_relation_name("t" * 60, "c" * 5, "seq") == "t" * 53 + "_ccccc_seq"
    # A multi-byte character is never split.
    name = pg_relation_name("é" * 40, None, "pkey")
    assert name == "é" * 29 + "_pkey" and len(name.encode()) <= 63


def test_a_primary_key_and_an_identity_column_reserve_their_names() -> None:
    names = pg_implicit_names([_orders(), _table("lines")])
    assert [(n.name, n.table, n.kind, n.column) for n in names] == [
        ("orders_id_seq", "orders", "identity sequence", "id"),
        ("orders_pkey", "orders", "primary key index", None),
    ]


def test_a_taken_name_takes_a_number_as_postgresql_does() -> None:
    """A table of the name exists, so PostgreSQL names the key ``orders_pkey1``."""
    names = pg_implicit_names([_table("orders_pkey"), _orders()])
    assert [n.name for n in names] == ["orders_id_seq", "orders_pkey1"]


def test_a_nullable_or_plain_column_reserves_no_sequence() -> None:
    tables = [_table("orders", [_col("id", default=SEQ), _col("a")], pk=["a"])]
    assert [n.name for n in pg_implicit_names(tables, source_engine="postgresql")] == [
        "orders_pkey"
    ]


def test_a_not_null_sequence_default_reserves_the_sequence_name() -> None:
    """The draft turns a NOT NULL column whose default draws from a sequence into an
    identity column (#156), so PostgreSQL creates a sequence for it."""
    tables = [_table("orders", [_col("id", default=SEQ, nullable=False)])]
    assert [n.name for n in pg_implicit_names(tables, source_engine="postgresql")] == [
        "orders_id_seq"
    ]


# ---------------------------------------------------------------------------
# A collected index of one of those names is renamed
# ---------------------------------------------------------------------------


def test_an_index_named_like_another_tables_primary_key_is_renamed() -> None:
    """The issue's shape: ``lines`` has an index named ``orders_pkey``."""
    design = _design([_orders(), _table("lines", indexes=[_index("orders_pkey", ["a"])])])
    assert _created(design) == ["lines_a_idx"]
    [note] = _renames(design)
    assert note["source_object"] == "lines.orders_pkey"
    assert note["recommendation"] == (
        "Created as lines_a_idx: PostgreSQL names the primary key index of orders "
        "orders_pkey, and an index cannot take the name of another index or sequence of "
        "the schema; use the new name wherever a script names this index."
    )


def test_an_index_named_like_an_identity_sequence_is_renamed() -> None:
    design = _design([_orders(), _table("lines", indexes=[_index("orders_id_seq", ["b"])])])
    assert _created(design) == ["lines_b_idx"]
    [note] = _renames(design)
    assert note["recommendation"].startswith(
        "Created as lines_b_idx: PostgreSQL names the identity sequence of orders.id "
        "orders_id_seq, and "
    )


def test_a_tables_own_index_named_like_its_primary_key_is_renamed() -> None:
    """Not on the key columns, so the draft carries it, and the key's index has the name."""
    design = _design([_orders(indexes=[_index("orders_pkey", ["a"])])])
    assert _created(design) == ["orders_a_idx"]


def test_a_numbered_key_name_is_reserved_too() -> None:
    lines = _table("lines", indexes=[_index("orders_pkey1", ["a"])])
    design = _design([_table("orders_pkey"), _orders(), lines])
    assert _created(design) == ["lines_a_idx"]


def test_an_index_that_clashes_with_no_reserved_name_keeps_it() -> None:
    design = _design([_orders(), _table("lines", indexes=[_index("orders_key", ["a"])])])
    assert _created(design) == ["orders_key"]
    assert _renames(design) == []


@pytest.mark.passes_on_main  # Aurora MySQL keeps index names per table, as main does
def test_aurora_mysql_keeps_the_collected_name() -> None:
    design = _design(
        [_orders(), _table("lines", indexes=[_index("orders_pkey", ["a"])])], engine="aurora_mysql"
    )
    assert "CREATE INDEX `orders_pkey` ON `lines`" in design["generated_ddl"]
    assert _renames(design) == []


# ---------------------------------------------------------------------------
# Expression indexes step 1 recreates
# ---------------------------------------------------------------------------


def test_a_renamed_index_skips_an_expression_index_name_step_1_recreates() -> None:
    """On a PostgreSQL to Aurora PostgreSQL move step 1 recreates ``u_a_idx`` under its
    source name, so ``u``'s clashing ``ix`` takes the next name."""
    tables = [
        _table("t", indexes=[_index("ix", ["a"])]),
        _table("u", indexes=[_index("ix", ["a"]), _expression("u_a_idx", "u")]),
    ]
    base = _base(tables, source="postgresql")
    [_, u] = base.tables
    assert [i.index_name for i in u.indexes or []] == ["u_a_idx1", "u_a_idx"]


def test_an_expression_index_named_like_a_primary_key_is_renamed() -> None:
    base = _base([_orders(), _table("lines", indexes=[_expression("orders_pkey", "lines")])],
                 source="postgresql")  # fmt: skip
    [_, lines] = base.tables
    assert [i.index_name for i in lines.indexes or []] == ["lines_idx"]
    [record] = base.renamed
    assert (record["table"], record["name"], record["new_name"]) == (
        "lines",
        "orders_pkey",
        "lines_idx",
    )
    # The design creates no expression index, so its note says step 1 does.
    [note] = _renames(merge_design_delta(base, {"delta_version": "1.0"}).output or {})
    assert note["recommendation"].startswith(
        "Step 1 recreates it as lines_idx: PostgreSQL names the primary key index of orders "
    )


def test_a_mysql_sources_expression_index_reserves_nothing() -> None:
    """Step 1 recreates only a PostgreSQL source's expression indexes."""
    tables = [
        _table("t", indexes=[_index("ix", ["a"])]),
        _table("u", indexes=[_index("ix", ["a"]), _expression("u_a_idx", "u")]),
    ]
    [_, u] = _base(tables, source="mysql").tables
    assert [i.index_name for i in u.indexes or []] == ["u_a_idx", "u_a_idx"]


# ---------------------------------------------------------------------------
# The delta
# ---------------------------------------------------------------------------


def test_a_delta_index_named_like_a_primary_key_is_refused() -> None:
    delta = {"delta_version": "1.0",
             "tables": [{"table_name": "lines",
                         "add_indexes": [{"index_name": "orders_pkey", "columns": ["a"]}]}]}  # fmt: skip
    result = merge_design_delta(_base([_orders(), _table("lines")]), delta, strict=False)
    assert result.errors == [
        "tables[0].add_indexes[0]: index name 'orders_pkey' is already used in the schema by "
        "the primary key index of table 'orders' (PostgreSQL index names are unique within "
        "a schema); choose another name"
    ]


def test_a_serial_type_whose_sequence_name_an_index_has_is_refused() -> None:
    """A SERIAL column makes PostgreSQL create ``<table>_<column>_seq`` too."""
    delta = {"delta_version": "1.0",
             "tables": [{"table_name": "orders",
                         "column_types": [{"column": "a", "aurora_type": "SERIAL"}]}]}  # fmt: skip
    tables = [_table("orders"), _table("lines", indexes=[_index("orders_a_seq", ["a"])])]
    result = merge_design_delta(_base(tables), delta, strict=False)
    assert result.errors == [
        "tables[0].column_types: orders.a as SERIAL makes PostgreSQL create the sequence "
        "'orders_a_seq', and table 'lines' has an index of that name (PostgreSQL index "
        "and sequence names are unique within a schema); choose another type or rename "
        "the index"
    ]


# ---------------------------------------------------------------------------
# Saved designs
# ---------------------------------------------------------------------------


def _save(tmp_path: Path, design: dict) -> dict:
    rel = f"{DB}/{JOB}/schema-aurora_postgresql/v1/schema_output.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(json.dumps(design), encoding="utf-8")
    loaded: dict = LocalArtifactStore(str(tmp_path)).read_json(rel)
    return loaded


@pytest.mark.passes_on_main  # guards the renamed design's load
def test_the_renamed_design_loads_unchanged(tmp_path: Path) -> None:
    design = _design([_orders(), _table("lines", indexes=[_index("orders_pkey", ["a"])])])
    assert _save(tmp_path, design) == json.loads(json.dumps(design))


@pytest.mark.passes_on_main  # a design main saved, with the clashing name, still loads
def test_a_design_saved_with_the_clashing_name_still_loads(tmp_path: Path) -> None:
    design = _design([_orders(), _table("lines", indexes=[_index("orders_pkey", ["a"])])])
    old = json.loads(json.dumps(design).replace("lines_a_idx", "orders_pkey"))
    old["app_layer_notes"] = [n for n in old["app_layer_notes"] if n["feature"] != "index_renamed"]
    assert _save(tmp_path, old) == old
