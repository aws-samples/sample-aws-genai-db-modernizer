# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 reads each foreign key's referenced schema and each database's collation (#2531).

The collection scripts report the schema of a foreign key's referenced table
(``ForeignKey.referenced_schema``) and, on MySQL, each database's default collation
(``SourceDatabase.schemas``). Step 1 resolves a key to the table of that schema, and gives
each Aurora MySQL database of a step of several databases its own collation. A collection
without them keeps the bare-name rule and the one collation (#2494, #2276): those tests
are ``test_dms_step_keeps_schemas.py`` and ``test_dms_step_collations.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import DDL_FILE, load_step1, render_files
from src.tools.data_movement.plan import write_plans
from src.tools.transformation_contract.write import write_step1
from tests.unit.tools.transformation_contract.conftest import col, collector, table

_AURORA = {"mysql": "aurora_mysql", "postgresql": "aurora_postgresql"}
LATIN1 = "CHARACTER SET latin1 COLLATE latin1_swedish_ci"
UNICODE = "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"


def _job(
    root: Path, engine: str, tables: list[dict], source: dict[str, Any] | None = None
) -> LocalArtifactStore:
    coll = collector(engine, tables)
    coll["metadata"]["source_database"].update(database_name="sales", version="8.0.32")
    coll["metadata"]["source_database"].update(source or {})
    wave = {"wave": 1, "title": "Move to Aurora", "engines": [_AURORA[engine]],
            "moves_from": [engine], "homogeneity": "homogeneous",
            "tables": [t["table_id"] for t in tables], "gate": "Aurora serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("sales/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "sales/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("sales/job1/collector/output.json", coll)
    write_step1(store, "sales", "job1")
    write_plans(store, "sales", "job1")
    return store


def _items(referenced_schema: str | None) -> Any:
    """``sales.items``, whose key ``fk_items_order`` references ``orders`` of
    ``referenced_schema`` (None: a collection written before #2531)."""
    items = table("sales", "items", [col("id", "bigint", 1), col("order_id", "bigint", 2)],
                  pk=["id"])  # fmt: skip
    fk = {"constraint_name": "fk_items_order", "columns": ["order_id"],
          "referenced_table": "orders", "referenced_columns": ["id"]}  # fmt: skip
    if referenced_schema is not None:
        fk["referenced_schema"] = referenced_schema
    items["foreign_keys"] = [fk]
    return items


def _orders(schema: str) -> Any:
    return table(schema, "orders", [col("id", "bigint", 1)], pk=["id"])


def _left_out(inputs) -> list[tuple[str, str, str]]:
    return [(o.table_id, o.kind, o.name) for o in inputs.left_out_objects]


@pytest.mark.parametrize("engine,q", [("postgresql", '"'), ("mysql", "`")])
def test_a_key_reaches_the_schema_the_collection_reports(tmp_path, engine, q):
    """``sales.items`` references ``archive.orders``. Both ``orders`` move, and the bare-name
    rule would take ``sales.orders``, the key's own schema's."""
    tables = [_items("archive"), _orders("sales"), _orders("archive")]
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert f"REFERENCES {q}archive{q}.{q}orders{q}" in ddl
    assert f"REFERENCES {q}sales{q}.{q}orders{q}" not in ddl
    assert _left_out(inputs) == []


@pytest.mark.parametrize("engine,q", [("postgresql", '"'), ("mysql", "`")])
def test_a_key_whose_table_two_other_schemas_have_is_kept(tmp_path, engine, q):
    """``archive`` and ``backup`` both have ``orders`` and ``sales`` has none: the bare-name
    rule leaves the key out (#2479 review). With its schema reported it reaches ``backup``."""
    tables = [_items("backup"), _orders("archive"), _orders("backup")]
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert f"REFERENCES {q}backup{q}.{q}orders{q}" in ddl
    assert _left_out(inputs) == []


@pytest.mark.parametrize("engine", ["postgresql", "mysql"])
def test_a_key_onto_a_table_of_a_schema_not_collected_is_left_out(tmp_path, engine):
    """A step of one schema: ``sales.items`` references ``orders`` of ``other``, a schema
    (MySQL: a database) the collection does not hold. ``sales.orders`` shares the name, and
    the key would point at it; it is left out with its reason instead."""
    tables = [_items("other"), _orders("sales")]
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert "REFERENCES" not in ddl
    assert _left_out(inputs) == [("sales.items", "foreign key", "fk_items_order")]
    header = " ".join(line.removeprefix("--").strip() for line in ddl.splitlines())
    assert (
        "it references orders in schema other, which this design of schema sales does not "
        "hold, so the key would point at sales's own table of that name"
    ) in header


@pytest.mark.passes_on_main  # a collection that reports the key's own schema: as before
@pytest.mark.parametrize("engine,q", [("postgresql", '"'), ("mysql", "`")])
def test_a_key_onto_its_own_schema_keeps_its_one_schema_form(tmp_path, engine, q):
    tables = [_items("sales"), _orders("sales")]
    inputs = load_step1(_job(tmp_path, engine, tables), "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert f"REFERENCES {q}orders{q} ({q}id{q})" in ddl
    assert _left_out(inputs) == []


@pytest.mark.passes_on_main  # main's bare-name rule keeps it; guards the #2531 review bug
@pytest.mark.parametrize("several", [False, True], ids=["one-database", "two-databases"])
def test_a_mysql_key_whose_database_differs_only_by_case_is_kept(tmp_path, several):
    """#2531 review: with lower_case_table_names=2 MySQL reports the key's database as its DDL
    spelled it (``SALES``) and the table's as stored (``sales``): one database, so the key is
    kept, in a step of one database and of two."""
    tables = [_items("SALES"), _orders("sales")] + ([_orders("archive")] if several else [])
    inputs = load_step1(_job(tmp_path, "mysql", tables), "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    target = "`sales`.`orders`" if several else "`orders`"
    assert f"REFERENCES {target} (`id`)" in ddl
    assert _left_out(inputs) == []


@pytest.mark.parametrize("several", [False, True], ids=["one-schema", "two-schemas"])
def test_a_postgresql_key_onto_a_schema_of_another_case_is_another_schema(tmp_path, several):
    """PostgreSQL's ``SALES`` and ``sales`` are two schemas: the key onto ``SALES.orders``
    never lands on ``sales.orders``."""
    tables = [_items("SALES"), _orders("sales")] + ([_orders("archive")] if several else [])
    inputs = load_step1(_job(tmp_path, "postgresql", tables), "sales", "job1")
    assert "REFERENCES" not in render_files(inputs)[DDL_FILE]
    assert _left_out(inputs) == [("sales.items", "foreign key", "fk_items_order")]


def _two_databases(collation: str = "latin1_swedish_ci") -> list[Any]:
    sales = _orders("sales")
    sales["collation"] = "utf8mb4_unicode_ci"
    archive = _orders("archive")
    archive["collation"] = collation
    return [sales, archive]


_SCHEMAS = [
    {"schema_name": "archive", "default_character_set": "latin1",
     "default_collation": "latin1_swedish_ci"},
    {"schema_name": "sales", "default_character_set": "utf8mb4",
     "default_collation": "utf8mb4_unicode_ci"},
]  # fmt: skip
_SOURCE = {"default_character_set": "utf8mb4", "default_collation": "utf8mb4_unicode_ci"}


def test_each_database_gets_its_own_collation(tmp_path):
    """``archive`` is on latin1_swedish_ci and ``sales`` on utf8mb4_unicode_ci. Each Aurora
    MySQL database is created on its own, and ``archive.orders``, on its database's
    collation, keeps no table collation of its own."""
    store = _job(tmp_path, "mysql", _two_databases(), {**_SOURCE, "schemas": _SCHEMAS})
    inputs = load_step1(store, "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert f"CREATE DATABASE IF NOT EXISTS `archive` {LATIN1};" in ddl
    assert f"CREATE DATABASE IF NOT EXISTS `sales` {UNICODE};" in ddl
    assert f"-- ALTER DATABASE `archive` {LATIN1};" in ddl
    assert "DEFAULT CHARSET" not in ddl
    header = " ".join(line.removeprefix("--").strip() for line in ddl.splitlines())
    assert "Each database gets its own source database's collation. archive: It creates" in header
    names = [d.name for d in inputs.contract.target.databases or []]
    assert names == ["archive", "sales"]


def test_a_database_the_collection_does_not_list_gets_the_collected_ones(tmp_path):
    """``archive`` is not in ``schemas``: it gets the collected database's collation, and its
    table, on latin1, keeps its own. ``sales`` is listed on utf8mb4_bin and gets that."""
    listed = [{"schema_name": "sales", "default_character_set": "utf8mb4",
               "default_collation": "utf8mb4_bin"}]  # fmt: skip
    store = _job(tmp_path, "mysql", _two_databases(), {**_SOURCE, "schemas": listed})
    ddl = render_files(load_step1(store, "sales", "job1"))[DDL_FILE]
    assert f"CREATE DATABASE IF NOT EXISTS `archive` {UNICODE};" in ddl
    assert "CREATE DATABASE IF NOT EXISTS `sales` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;" in ddl
    assert "DEFAULT CHARSET=latin1 COLLATE=latin1_swedish_ci" in ddl


@pytest.mark.passes_on_main  # a collection written before #2531: one collation for all (#2276)
def test_an_older_collection_gives_every_database_the_one_collation(tmp_path):
    store = _job(tmp_path, "mysql", _two_databases(), _SOURCE)
    inputs = load_step1(store, "sales", "job1")
    ddl = render_files(inputs)[DDL_FILE]
    assert f"CREATE DATABASE IF NOT EXISTS `archive` {UNICODE};" in ddl
    assert f"CREATE DATABASE IF NOT EXISTS `sales` {UNICODE};" in ddl
    assert "databases" not in inputs.contract.target.model_dump(mode="json")


@pytest.mark.passes_on_main  # one database: the contract keeps its bytes
def test_a_step_of_one_database_writes_no_databases_list(tmp_path):
    sales = _orders("sales")
    sales["collation"] = "utf8mb4_unicode_ci"
    store = _job(tmp_path, "mysql", [sales], {**_SOURCE, "schemas": _SCHEMAS})
    target = load_step1(store, "sales", "job1").contract.target
    assert "databases" not in target.model_dump(mode="json")
