# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aurora PostgreSQL index names are unique per schema (#2410).

PostgreSQL keeps indexes in the schema's namespace, next to its tables, so two tables of
one schema cannot each have an index of one name: the second ``CREATE INDEX`` fails with
``relation already exists``. MySQL scopes index names per table, so a MySQL source often
repeats one (WordPress's ``meta_key`` on four tables). The design base keeps every such
index and renames each later one to ``<table>_<columns>_idx`` (PostgreSQL's own pattern
for an unnamed index), cut to PostgreSQL's 63-byte limit, with an app-layer note naming
the old and the new name. Aurora MySQL keeps the names as collected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

JOB, DB = "job-2410", "app"


def _table(
    name: str, indexes: list[AgentIndex], schema: str | None = None, columns: tuple = ("a", "b")
) -> AgentTable:
    return AgentTable(
        table_id=f"{schema or DB}.{name}",
        table_name=name,
        schema_name=schema,
        row_count=10,
        columns=[AgentColumn(column_name=c, data_type="int", nullable=True) for c in columns],
        indexes=indexes,
    )


def _index(name: str, columns: list[str]) -> AgentIndex:
    return AgentIndex(index_name=name, columns=columns, is_unique=False)


def _base(engine: str, tables: list[AgentTable], source: str = "mysql") -> AuroraDesignBase:
    return AuroraDesignBase(engine=engine, job_id=JOB, source_database=DB,
                            source_engine=source, tables=tables)  # fmt: skip


def _merge(base: AuroraDesignBase, delta: dict | None = None):
    return merge_design_delta(base, delta or {"delta_version": "1.0"}, strict=False)


def _design(engine: str, tables: list[AgentTable], source: str = "mysql") -> dict:
    output: dict | None = _merge(_base(engine, tables, source)).output
    assert output is not None
    return output


_CREATE = re.compile(r'^CREATE (?:UNIQUE )?INDEX ("(?:[^"]|"")+"|`(?:[^`]|``)+`) ON (\S+)', re.M)


def _created(design: dict) -> list[tuple[str, str]]:
    """Each CREATE INDEX of the script: (index name as written, table as written)."""
    return _CREATE.findall(design["generated_ddl"])


def _renames(design: dict) -> list[dict]:
    return [n for n in design["app_layer_notes"] if n["feature"] == "index_renamed"]


def _two_meta_tables(schema: str | None = None) -> list[AgentTable]:
    return [
        _table("postmeta", [_index("meta_key", ["a"])], schema),
        _table("usermeta", [_index("meta_key", ["b"])], schema),
    ]


def test_two_tables_of_one_schema_get_two_index_names() -> None:
    design = _design("aurora_postgresql", _two_meta_tables())
    assert _created(design) == [('"meta_key"', '"postmeta"'), ('"usermeta_b_idx"', '"usermeta"')]
    assert design["table_definitions"][1]["indexes"] == [
        'CREATE INDEX "usermeta_b_idx" ON "usermeta" ("b");'
    ]
    [note] = _renames(design)
    assert note["source_object"] == "usermeta.meta_key"
    assert note["recommendation"] == (
        "Created as usermeta_b_idx: postmeta already has an index named meta_key in this "
        "schema, and PostgreSQL index names are unique within a schema; use the new name "
        "wherever a script names this index."
    )
    # Renamed, not left out: no "Not created" comment and no left-out note.
    assert "-- Not created:" not in design["generated_ddl"]
    assert not [n for n in design["app_layer_notes"] if n["feature"] == "duplicate_index_name"]


def test_every_later_table_of_the_schema_is_renamed_and_the_first_kept() -> None:
    tables = [_table(f"t{i}", [_index("ix", ["a"])]) for i in range(3)]
    design = _design("aurora_postgresql", tables)
    assert [n for n, _ in _created(design)] == ['"ix"', '"t1_a_idx"', '"t2_a_idx"']
    assert [n["source_object"] for n in _renames(design)] == ["t1.ix", "t2.ix"]


@pytest.mark.passes_on_main  # main already created both: two schemas never clash
def test_one_index_name_in_two_schemas_is_kept_in_both() -> None:
    tables = [
        _table("orders", [_index("ix", ["a"])], "sales"),
        _table("invoices", [_index("ix", ["a"])], "billing"),
    ]
    design = _design("aurora_postgresql", tables, source="postgresql")
    assert _created(design) == [('"ix"', '"sales"."orders"'), ('"ix"', '"billing"."invoices"')]
    assert _renames(design) == []


def test_in_a_design_of_several_schemas_each_schema_is_checked_on_its_own() -> None:
    tables = [
        _table("orders", [_index("ix", ["a"])], "sales"),
        _table("returns", [_index("ix", ["b"])], "sales"),
        _table("invoices", [_index("ix", ["a"])], "billing"),
    ]
    design = _design("aurora_postgresql", tables, source="postgresql")
    assert _created(design) == [
        ('"ix"', '"sales"."orders"'),
        ('"returns_b_idx"', '"sales"."returns"'),
        ('"ix"', '"billing"."invoices"'),
    ]
    assert [n["source_object"] for n in _renames(design)] == ["sales.returns.ix"]


@pytest.mark.passes_on_main  # Aurora MySQL scopes index names per table, as main does
def test_aurora_mysql_keeps_the_collected_names() -> None:
    design = _design("aurora_mysql", _two_meta_tables())
    assert _created(design) == [("`meta_key`", "`postmeta`"), ("`meta_key`", "`usermeta`")]
    assert _renames(design) == []


def test_an_index_named_like_a_table_of_the_schema_is_renamed() -> None:
    """Tables share the namespace, and the script creates every table first."""
    tables = [_table("orders", []), _table("lines", [_index("orders", ["a"])])]
    design = _design("aurora_postgresql", tables)
    assert _created(design) == [('"lines_a_idx"', '"lines"')]


def test_a_new_name_another_index_has_takes_a_number() -> None:
    tables = [
        _table("t", [_index("ix", ["a"])]),
        _table("u", [_index("u_a_idx", ["b"]), _index("ix", ["a"])]),
    ]
    design = _design("aurora_postgresql", tables)
    assert [n for n, _ in _created(design)] == ['"ix"', '"u_a_idx"', '"u_a_idx1"']


def test_a_new_name_fits_63_bytes_and_names_equal_in_63_bytes_clash() -> None:
    """PostgreSQL cuts every name to 63 bytes, so two names that differ only after that are
    one name; the new one is cut on a character boundary to fit."""
    long_table = "é" * 40  # 80 bytes
    long_ix = "x" * 63
    tables = [
        _table("t", [_index(long_ix + "1", ["a"])]),
        _table(long_table, [_index(long_ix + "2", ["a"])]),
    ]
    design = _design("aurora_postgresql", tables)
    [(first, _), (second, _)] = _created(design)
    assert first == f'"{long_ix}1"'
    renamed = second.strip('"')
    assert len(renamed.encode()) <= 63 and renamed.endswith("_idx")
    assert renamed == "é" * 28 + "_a_idx"


@pytest.mark.passes_on_main  # guards the renamed design's load; main loads its own too
@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_the_saved_design_loads_unchanged(tmp_path: Path, engine: str) -> None:
    design = _design(engine, _two_meta_tables())
    rel = f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(json.dumps(design), encoding="utf-8")
    assert LocalArtifactStore(str(tmp_path)).read_json(rel) == json.loads(json.dumps(design))


@pytest.mark.passes_on_main  # a design main saved, with the repeated name, still loads
def test_a_design_saved_with_a_repeated_name_still_loads(tmp_path: Path) -> None:
    design = _design("aurora_postgresql", _two_meta_tables())
    old = json.loads(json.dumps(design).replace("usermeta_b_idx", "meta_key"))
    old["app_layer_notes"] = [n for n in old["app_layer_notes"] if n["feature"] != "index_renamed"]
    rel = f"{DB}/{JOB}/schema-aurora_postgresql/v1/schema_output.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(json.dumps(old), encoding="utf-8")
    assert LocalArtifactStore(str(tmp_path)).read_json(rel) == old


def test_a_delta_index_with_another_tables_index_name_is_refused_on_postgresql() -> None:
    delta = {
        "delta_version": "1.0",
        "tables": [{"table_name": "usermeta",
                    "add_indexes": [{"index_name": "meta_key2", "columns": ["a"]},
                                    {"index_name": "meta_key", "columns": ["a"]}]}],
    }  # fmt: skip
    tables = [_table("postmeta", [_index("meta_key", ["a"])]), _table("usermeta", [])]
    result = _merge(_base("aurora_postgresql", tables), delta)
    assert result.errors == [
        "tables[0].add_indexes[1]: index name 'meta_key' is already used in the schema "
        "by table 'postmeta' (PostgreSQL index names are unique within a schema); "
        "choose another name"
    ]
    assert result.output is not None
    assert [n for n, _ in _created(result.output)] == ['"meta_key"', '"meta_key2"']


@pytest.mark.passes_on_main
def test_a_delta_index_with_another_tables_index_name_is_kept_on_mysql() -> None:
    delta = {"delta_version": "1.0",
             "tables": [{"table_name": "usermeta",
                         "add_indexes": [{"index_name": "meta_key", "columns": ["a"]}]}]}  # fmt: skip
    tables = [_table("postmeta", [_index("meta_key", ["a"])]), _table("usermeta", [])]
    result = _merge(_base("aurora_mysql", tables), delta)
    assert result.errors == []
    assert result.output is not None and len(_created(result.output)) == 2


def test_a_delta_index_named_like_a_table_is_refused_on_postgresql() -> None:
    delta = {"delta_version": "1.0",
             "tables": [{"table_name": "lines",
                         "add_indexes": [{"index_name": "orders", "columns": ["a"]}]}]}  # fmt: skip
    tables = [_table("orders", []), _table("lines", [])]
    result = _merge(_base("aurora_postgresql", tables), delta)
    assert result.errors == [
        "tables[0].add_indexes[0]: index name 'orders' is already used in the schema by "
        "a table of that name (PostgreSQL index names are unique within a schema); "
        "choose another name"
    ]


def test_a_note_for_a_name_a_table_has_says_so() -> None:
    tables = [_table("orders", []), _table("lines", [_index("orders", ["a"])])]
    [note] = _renames(_design("aurora_postgresql", tables))
    assert note["recommendation"].startswith(
        "Created as lines_a_idx: the schema already has a table named orders, and "
    )


def test_only_an_index_that_clashes_is_renamed() -> None:
    """Every collected name is reserved before a new one is chosen: ``v``'s own
    ``u_a_idx`` keeps its name, and ``u``'s clashing ``ix`` takes the next one."""
    tables = [
        _table("t", [_index("ix", ["a"])]),
        _table("u", [_index("ix", ["a"])]),
        _table("v", [_index("u_a_idx", ["b"])]),
    ]
    design = _design("aurora_postgresql", tables)
    assert [n for n, _ in _created(design)] == ['"ix"', '"u_a_idx1"', '"u_a_idx"']
    assert [n["source_object"] for n in _renames(design)] == ["u.ix"]


@pytest.mark.passes_on_main  # main refused no name; this keeps a freed name usable
@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "add-first"])
def test_a_freed_name_is_reusable_whatever_the_order_of_the_delta(remove_first: bool) -> None:
    remove = {"table_name": "postmeta", "remove_indexes": ["meta_key"]}
    add = {"table_name": "usermeta", "add_indexes": [{"index_name": "meta_key", "columns": ["a"]}]}
    delta = {"delta_version": "1.0", "tables": [remove, add] if remove_first else [add, remove]}
    tables = [_table("postmeta", [_index("meta_key", ["a"])]), _table("usermeta", [])]
    result = _merge(_base("aurora_postgresql", tables), delta)
    assert result.errors == []
    assert result.output is not None
    assert _created(result.output) == [('"meta_key"', '"usermeta"')]


@pytest.mark.parametrize("change", ["remove_indexes", "modify_indexes"])
def test_a_renamed_index_named_by_its_old_name_gets_the_new_name(change: str) -> None:
    entry = ["meta_key"] if change == "remove_indexes" else [
        {"index_name": "meta_key", "columns": ["a"]}]  # fmt: skip
    delta = {"delta_version": "1.0", "tables": [{"table_name": "usermeta", change: entry}]}
    result = _merge(_base("aurora_postgresql", _two_meta_tables()), delta)
    [error] = result.errors
    assert error.endswith(
        "; the design creates it as 'usermeta_b_idx', because another object of the schema "
        "has the name, so use that name"
    )


@pytest.mark.passes_on_main
def test_the_carried_indexes_are_the_tables_own_objects() -> None:
    """``unique_index_names`` marks the indexes to rename by object, reading them from
    ``carried_indexes`` and renaming them in ``table.indexes`` (#2545 review): the two
    must hand back the same objects, or no clashing index is ever renamed."""
    from src.tools.schema.aurora_common.ddl_generator import POSTGRES, carried_indexes

    t = _table("postmeta", [_index("meta_key", ["a"]), _index("b_idx", ["b"])])
    carried = carried_indexes(t, POSTGRES)
    assert carried and all(any(c is i for i in t.indexes or []) for c in carried)
