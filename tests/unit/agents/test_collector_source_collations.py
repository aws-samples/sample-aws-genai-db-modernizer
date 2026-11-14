# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MySQL family's character sets and collations, in every collection path (#2276).

The offline script (``scripts/collect-mysql.sql``) and the live collectors read the
collected database's default character set and collation from
``information_schema.SCHEMATA``, and each column's where its collation differs from its
table's. The collector output keeps them on ``metadata.source_database`` and on the
column, the same way from both paths; step 1 carries them into Aurora MySQL. A collection
without them loads with None. No real database runs here (the mysqld twin is
``tests/integration/test_collect_mysql_collations.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector import mariadb_collector, mysql_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "collect-mysql.sql"

DATABASE = {"default_character_set": "utf8mb4", "default_collation": "utf8mb4_unicode_ci"}
COLUMNS = [
    {"column_name": "id", "data_type": "int", "is_nullable": "NO", "ordinal_position": 1,
     "character_set_name": None, "collation_name": None},
    {"column_name": "code", "data_type": "varchar", "is_nullable": "YES", "ordinal_position": 2,
     "max_length": 10, "character_set_name": "utf8mb4", "collation_name": "utf8mb4_bin"},
]  # fmt: skip


def _kept(output: dict | CollectorOutputContract) -> tuple[dict, dict]:
    """What a collection kept: the database's two names, and each column's."""
    data = output if isinstance(output, dict) else output.model_dump(mode="json")
    source = data["metadata"]["source_database"]
    (table,) = data["database_schema"]["tables"]
    return (
        {k: source.get(k) for k in DATABASE},
        {c["column_name"]: (c.get("character_set"), c.get("collation")) for c in table["columns"]},
    )


EXPECTED = (DATABASE, {"id": (None, None), "code": ("utf8mb4", "utf8mb4_bin")})


# -- the collection script ---------------------------------------------------------------


def test_the_script_reads_the_databases_defaults_from_schemata():
    text = SCRIPT.read_text()
    assert ("'default_character_set', (SELECT DEFAULT_CHARACTER_SET_NAME FROM "
            "information_schema.SCHEMATA WHERE SCHEMA_NAME = @db)") in text  # fmt: skip
    assert ("'default_collation', (SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME = @db)") in text  # fmt: skip


def test_the_script_records_a_columns_collation_only_where_it_differs_from_its_tables():
    text = " ".join(SCRIPT.read_text().split())
    for key, column in (
        ("character_set_name", "character_set_name"),
        ("collation_name", "collation_name"),
    ):
        assert (f"'{key}', IF(collation_name <=> (SELECT t.table_collation FROM information_schema.tables t "
                "WHERE t.table_schema = c.table_schema AND t.table_name = c.table_name), NULL, "
                f"{column})") in text  # fmt: skip


# -- the offline builder -----------------------------------------------------------------


def _raw(database: dict | None, columns: list[dict]) -> dict:
    metadata: dict = {"version": "8.0.36", "database_name": "app", "database_size_gb": 1.0}
    metadata.update(database or {})
    return {
        "metadata": metadata,
        "tables": [{"table_name": "t", "row_count": 10, "table_collation": "utf8mb4_unicode_ci"}],
        "columns": [{"table_name": "t", **c} for c in columns],
    }


def _built(raw: dict) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="app", s3_key="k"
    )


def test_the_offline_builder_keeps_the_databases_and_the_columns_collations():
    assert _kept(_built(_raw(DATABASE, COLUMNS))) == EXPECTED


@pytest.mark.passes_on_main  # an older collection: nothing recorded, nothing kept
def test_an_older_collection_without_collations_builds_with_none():
    columns = [
        {k: v for k, v in c.items() if "_name" not in k or k == "column_name"} for c in COLUMNS
    ]
    database, by_column = _kept(_built(_raw(None, columns)))
    assert set(database.values()) == {None}
    assert set(by_column.values()) == {(None, None)}


def test_a_client_null_or_an_overlong_name_is_not_kept():
    columns = [{**COLUMNS[1], "character_set_name": "NULL", "collation_name": "x" * 65},
               {**COLUMNS[1], "column_name": "kept"}]  # fmt: skip
    database = {"default_character_set": "utf8mb4", "default_collation": "NULL"}
    got = _kept(_built(_raw(database, columns)))
    assert got == (
        {"default_character_set": "utf8mb4", "default_collation": None},
        {"code": (None, None), "kept": ("utf8mb4", "utf8mb4_bin")},
    )


@pytest.mark.passes_on_main  # the fields are optional: an older output still loads
def test_an_older_collector_output_loads_without_them():
    output = _built(_raw(None, COLUMNS[:1]))
    for key in DATABASE:
        output["metadata"]["source_database"].pop(key, None)
    loaded = CollectorOutputContract.model_validate(output)
    assert getattr(loaded.metadata.source_database, "default_collation", None) is None


# -- the live collectors -----------------------------------------------------------------


class _Checkpoints:
    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages

    def load_or_run(self, name: str, fn: Any) -> Any:
        return self.stages[name] if name in self.stages else fn()


ENGINES = {
    "mysql": (mysql_collector, "MySQLRemoteCollector", "_collect_live"),
    "mariadb": (mariadb_collector, "MariaDBRemoteCollector", "_collect_live_mariadb"),
}


def _run_live(engine: str, metadata_charset: dict | None) -> CollectorOutputContract:
    module, cls, entry = ENGINES[engine]
    inp = CollectorInput.model_validate({
        "job_id": "j", "engine": engine, "cluster_endpoint": "db.example", "port": 3306,
        "database_name": "app", "mode": "live",
        "live_config": {"secret_arn": "s", "automation_instance_id": "i"},
    })  # fmt: skip
    metadata: dict = {"version": "8.0.45", "db_size_gb": 1.0}
    if metadata_charset is not None:
        metadata["charset"] = metadata_charset
    table = {"table_id": "app.t", "table_name": "t", "table_collation": "utf8mb4_unicode_ci",
             "columns": COLUMNS}  # fmt: skip
    stages = {
        "metadata": metadata,
        "schema": {"tables": [table], "global_stats": {}},
        "queries": [],
        "aws_metrics": {},
    }
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, MagicMock()),
    ):
        result: CollectorOutputContract = getattr(module, entry)(inp, _Checkpoints(stages))
    return result


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_keeps_what_the_offline_one_keeps(engine):
    live = _kept(_run_live(engine, DATABASE))
    assert live == EXPECTED == _kept(_built(_raw(DATABASE, COLUMNS)))


@pytest.mark.passes_on_main  # an older checkpoint: nothing read, nothing kept
@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_from_an_older_checkpoint_keeps_none(engine):
    database, _ = _kept(_run_live(engine, None))
    assert set(database.values()) == {None}


def _tools(rows: Any) -> MySQLRemoteCollector:
    ssm = MagicMock()
    if isinstance(rows, Exception):
        ssm.run_sql_json.side_effect = rows
    else:
        ssm.run_sql_json.return_value = rows
    return MySQLRemoteCollector(ssm=ssm, host="h", port=1, database="d", secret_arn="s", region="r")


def test_the_live_tools_read_the_databases_defaults_from_schemata():
    db = _tools([DATABASE])
    assert db.get_database_charset() == DATABASE
    sql = " ".join(db.ssm.run_sql_json.call_args.kwargs["sql"].split())
    assert "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()" in sql
    assert _tools(RuntimeError("denied")).get_database_charset() == {}
    assert _tools([]).get_database_charset() == {}


def test_the_live_tools_read_a_columns_collation_only_where_it_differs_from_its_tables():
    db = _tools([])
    db.collect_columns("t")
    sql = " ".join(db.ssm.run_sql_json.call_args.kwargs["sql"].split())
    assert (
        "IF(c.collation_name <=> t.table_collation, NULL, c.collation_name) AS collation_name"
        in sql
    )
    assert (
        "IF(c.collation_name <=> t.table_collation, NULL, c.character_set_name) AS character_set_name"
        in sql
    )
