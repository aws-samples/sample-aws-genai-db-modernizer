# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collection path reads and keeps the source's string settings (#1482).

The collection scripts read ``@@GLOBAL.sql_mode`` (MySQL, MariaDB) and
``current_setting('standard_conforming_strings')`` (PostgreSQL) into
``metadata.settings``; the live collectors read the same in their metadata stage. The
collector output keeps them on ``metadata.source_database.settings``, and every text
is redacted with them. A collection without them reads each engine's defaults.

No real database runs here: the live collectors' database calls are mocked and their
stages canned, as in ``test_collector_source_host_live.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector import mariadb_collector, mysql_collector, postgres_collector
from src.agents.collector.mysql_collector import source_settings
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract, SourceSettings
from src.shared.sql_spans import (
    DEFAULT_SPAN_SETTINGS,
    SpanSettings,
    active_span_settings,
    span_settings_of,
)
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

# A view whose first string ends in a backslash: with NO_BACKSLASH_ESCAPES the value
# ``Jane`` is a string of its own; read with the defaults it is left in the code.
MYSQL_VIEW = "SELECT id FROM t WHERE p = 'C:\\' AND name = 'Jane'"
MYSQL_QUERY = "SELECT id FROM t WHERE path = 'C:\\' AND name = 'Jane Doe'"
POSTGRES_VIEW = "SELECT id FROM t WHERE note = 'it\\'s' AND name = 'Jane'"


# -- the collection scripts ------------------------------------------------------------


def test_the_mysql_script_reads_the_servers_sql_mode():
    text = (SCRIPTS / "collect-mysql.sql").read_text()
    assert "'settings', JSON_OBJECT('sql_mode', @@GLOBAL.sql_mode)" in text


def test_the_postgresql_script_reads_standard_conforming_strings():
    text = (SCRIPTS / "collect-postgresql.sql").read_text()
    assert "'standard_conforming_strings', current_setting('standard_conforming_strings')" in (text)


# -- the offline builder ---------------------------------------------------------------


def _raw(settings: dict | None) -> dict:
    metadata: dict = {"version": "8.0.36", "database_name": "app", "database_size_gb": 1.0}
    if settings is not None:
        metadata["settings"] = settings
    return {
        "metadata": metadata,
        "tables": [{"table_name": "t", "row_count": 10}],
        "columns": [
            {
                "table_name": "t",
                "column_name": "id",
                "data_type": "int",
                "is_nullable": "NO",
                "ordinal_position": 1,
            }
        ],
        "views": [{"view_name": "v", "definition": MYSQL_VIEW}],
        "queries": [{"query_text": MYSQL_QUERY, "digest": "d1", "exec_count": 5}],
    }


def _built(raw: dict) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="app", s3_key="k"
    )


def test_the_offline_builder_keeps_the_settings_and_redacts_with_them():
    output = _built(_raw({"sql_mode": "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"}))
    source = output["metadata"]["source_database"]
    assert source["settings"] == {
        "sql_mode": "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES",
        "standard_conforming_strings": None,
    }
    assert span_settings_of(output) == SpanSettings(no_backslash_escapes=True)
    query = output["queries"]["query_patterns"][0]["query_text"]
    assert "Jane" not in query and query.startswith("SELECT id FROM t WHERE path = ?")
    view = output["database_schema"]["views"][0]["definition"]
    assert "Jane" not in view
    # Read back: the contract loads it unchanged (redaction is idempotent there too).
    again = CollectorOutputContract.model_validate(output).model_dump(mode="json")
    assert again["queries"]["query_patterns"][0]["query_text"] == query
    assert again["database_schema"]["views"][0]["definition"] == view


def test_an_older_collection_without_settings_builds_as_before():
    output = _built(_raw(None))
    assert output["metadata"]["source_database"]["settings"] is None
    assert span_settings_of(output) == DEFAULT_SPAN_SETTINGS


def test_an_older_collector_output_loads_without_settings():
    output = _built(_raw(None))
    del output["metadata"]["source_database"]["settings"]
    loaded = CollectorOutputContract.model_validate(output)
    assert loaded.metadata.source_database.settings is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"sql_mode": "ANSI_QUOTES"}, SourceSettings(sql_mode="ANSI_QUOTES")),
        (
            {"standard_conforming_strings": "off", "other": "x"},
            SourceSettings(standard_conforming_strings="off"),
        ),
        ({}, None),
        (None, None),
        ("sql_mode=x", None),
    ],
)
def test_only_the_settings_the_scanners_read_are_kept(raw, expected):
    assert source_settings(raw) == expected


# -- the live collectors ---------------------------------------------------------------


class _Checkpoints:
    """Canned stages; records the settings active when each stage is loaded."""

    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages
        self.seen: dict[str, SpanSettings] = {}

    def load_or_run(self, name: str, fn: Any) -> Any:
        self.seen[name] = active_span_settings()
        return self.stages[name] if name in self.stages else fn()


ENGINES = {
    "mysql": (mysql_collector, "MySQLRemoteCollector", "_collect_live", 3306),
    "mariadb": (mariadb_collector, "MariaDBRemoteCollector", "_collect_live_mariadb", 3306),
    "postgresql": (postgres_collector, "PostgreSQLRemoteCollector", "_collect_live", 5432),
}
SETTINGS = {
    "mysql": ({"sql_mode": "NO_BACKSLASH_ESCAPES"}, SpanSettings(no_backslash_escapes=True)),
    "mariadb": ({"sql_mode": "NO_BACKSLASH_ESCAPES"}, SpanSettings(no_backslash_escapes=True)),
    "postgresql": (
        {"standard_conforming_strings": "off"},
        SpanSettings(standard_conforming_strings=False),
    ),
}


def _run_live(engine: str, settings: dict | None) -> tuple[CollectorOutputContract, _Checkpoints]:
    module, cls, entry, port = ENGINES[engine]
    inp = CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": engine,
            "cluster_endpoint": "db.example",
            "port": port,
            "database_name": "app",
            "mode": "live",
            "live_config": {"secret_arn": "s", "automation_instance_id": "i"},
        }
    )
    metadata: dict = {"version": "8.0.45", "db_size_gb": 1.0}
    if settings is not None:
        metadata["settings"] = settings
    view = POSTGRES_VIEW if engine == "postgresql" else MYSQL_VIEW
    table = {
        "table_id": "app.t",
        "table_name": "t",
        "columns": [{"column_name": "id", "data_type": "int", "is_nullable": "NO"}],
    }
    view_row = (
        {"view_name": "v", "view_definition": view}
        if engine == "postgresql"
        else {"view_name": "v", "definition": view}
    )
    stages = {
        "metadata": metadata,
        "schema": {"tables": [table], "global_stats": {}, "views": [view_row]},
        "queries": [],
        "aws_metrics": {},
    }
    checkpoints = _Checkpoints(stages)
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, MagicMock()),
    ):
        result: CollectorOutputContract = getattr(module, entry)(inp, checkpoints)
    return result, checkpoints


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_keeps_and_reads_the_settings_it_read(engine):
    reported, expected = SETTINGS[engine]
    result, checkpoints = _run_live(engine, reported)
    assert result.metadata.source_database.settings == SourceSettings(**reported)
    # Every stage after the metadata is read with them, and they end with the collection.
    assert checkpoints.seen["metadata"] == DEFAULT_SPAN_SETTINGS
    assert checkpoints.seen["schema"] == expected
    assert checkpoints.seen["queries"] == expected
    assert active_span_settings() == DEFAULT_SPAN_SETTINGS
    views = result.database_schema.views or []
    assert views and all("Jane" not in v.definition for v in views)


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_from_an_older_checkpoint_reads_the_defaults(engine):
    result, checkpoints = _run_live(engine, None)
    assert result.metadata.source_database.settings is None
    assert checkpoints.seen["schema"] == DEFAULT_SPAN_SETTINGS


def _tools(cls: type, rows: Any) -> Any:
    ssm = MagicMock()
    if isinstance(rows, Exception):
        ssm.run_sql_json.side_effect = rows
    else:
        ssm.run_sql_json.return_value = rows
    return cls(ssm=ssm, host="h", port=1, database="d", secret_arn="s", region="r")


def test_the_mysql_tools_read_the_global_sql_mode():
    db = _tools(MySQLRemoteCollector, [{"sql_mode": "NO_BACKSLASH_ESCAPES"}])
    assert db.get_settings() == {"sql_mode": "NO_BACKSLASH_ESCAPES"}
    assert "@@GLOBAL.sql_mode" in db.ssm.run_sql_json.call_args.kwargs["sql"]


def test_the_postgresql_tools_read_standard_conforming_strings():
    db = _tools(PostgreSQLRemoteCollector, [{"standard_conforming_strings": "off"}])
    assert db.get_settings() == {"standard_conforming_strings": "off"}


@pytest.mark.parametrize("cls", [MySQLRemoteCollector, PostgreSQLRemoteCollector])
def test_a_setting_that_cannot_be_read_is_the_default(cls):
    assert _tools(cls, RuntimeError("denied")).get_settings() == {}
    assert _tools(cls, []).get_settings() == {}


# -- each routine's and trigger's own sql_mode (review of #1640) -----------------------


def test_the_mysql_script_reads_each_routines_and_triggers_sql_mode():
    text = (SCRIPTS / "collect-mysql.sql").read_text()
    procedures = text[text.index("8. Procedures") : text.index("9. Triggers")]
    triggers = text[text.index("9. Triggers") : text.index("10. Query Patterns")]
    assert "'sql_mode', sql_mode" in procedures
    assert "'sql_mode', sql_mode" in triggers


def test_the_mysql_tools_read_each_routines_and_triggers_sql_mode():
    db = _tools(MySQLRemoteCollector, [])
    db.collect_procedures()
    assert "sql_mode" in db.ssm.run_sql_json.call_args.kwargs["sql"]
    db.collect_triggers()
    assert "sql_mode" in db.ssm.run_sql_json.call_args.kwargs["sql"]


def test_the_builder_keeps_each_routines_and_triggers_sql_mode():
    from src.agents.collector.mysql_collector import _build_procedures, _build_triggers

    procedures = _build_procedures(
        [
            {"routine_name": "p", "routine_type": "PROCEDURE", "definition": "BEGIN END"},
            {
                "routine_name": "q",
                "routine_type": "FUNCTION",
                "definition": "RETURN 1",
                "sql_mode": "NO_BACKSLASH_ESCAPES",
            },
        ]
    )
    assert [p.sql_mode for p in procedures or []] == [None, "NO_BACKSLASH_ESCAPES"]
    triggers = _build_triggers(
        [
            {
                "trigger_name": "tr",
                "event_type": "INSERT",
                "table_name": "t",
                "timing": "BEFORE",
                "definition": "SET NEW.a = 1",
                "sql_mode": "ANSI_QUOTES",
            }
        ]
    )
    assert [t.sql_mode for t in triggers or []] == ["ANSI_QUOTES"]
