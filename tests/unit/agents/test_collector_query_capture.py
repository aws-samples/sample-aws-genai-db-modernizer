# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every live collector captures every statement by default (#384).

There is no default statement cap: the statement query has no ``LIMIT``, ``TOP`` or
``ROWNUM`` clause unless the collector input sets ``query_limit``. The minimum
execution count (``min_executions``, 10 on every engine) always applies. Each live
collector records both, with how many statements it captured, in
``metadata.query_capture``; a failed statement query records its error.

No real database runs here: the SQL clients are mocked and the other stages canned,
as in ``test_collector_source_host_live.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.agents.collector import (
    mariadb_collector,
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.contracts.collector_input import CollectionOptions, CollectorInput
from src.contracts.collector_output import CollectorOutputContract, QueryCapture
from src.shared.query_capture import DEFAULT_MIN_EXECUTIONS, live_query_capture
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

TOOLS = {
    "mysql": MySQLRemoteCollector,
    "mariadb": MariaDBRemoteCollector,
    "postgresql": PostgreSQLRemoteCollector,
    "sqlserver": SQLServerRemoteCollector,
    "oracle": OracleRemoteCollector,
}
# engine -> (the statement cap as the SQL spells it, the minimum count as it spells it)
CAP_WORDS = {"mysql": "LIMIT", "mariadb": "LIMIT", "postgresql": "LIMIT", "sqlserver": "TOP"}
SQL_FORMS = {
    "mysql": ("LIMIT {limit}", "COUNT_STAR >= {min}"),
    "mariadb": ("LIMIT {limit}", "COUNT_STAR >= {min}"),
    "postgresql": ("LIMIT {limit}", "calls >= {min}"),
    "sqlserver": ("TOP {limit}", "execution_count >= {min}"),
    "oracle": ("ROWNUM <= {limit}", "executions >= {min}"),
}


def test_the_defaults_are_no_cap_and_ten_executions():
    assert DEFAULT_MIN_EXECUTIONS == 10
    options = CollectionOptions()
    assert options.query_limit is None
    assert options.min_executions == 10


def test_both_settings_are_configurable():
    options = CollectionOptions(query_limit=20_000, min_executions=3)
    assert (options.query_limit, options.min_executions) == (20_000, 3)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("field", ["query_limit", "min_executions"])
@pytest.mark.parametrize("value", [0, -1, True, False, "10000", 10.0])
def test_a_setting_that_is_not_a_positive_whole_number_is_refused(field: str, value: object):
    with pytest.raises(ValidationError):
        CollectionOptions.model_validate({field: value})


def test_a_minimum_count_of_none_is_refused():
    with pytest.raises(ValidationError):
        CollectionOptions(min_executions=None)


def test_an_older_request_without_the_settings_reads_the_defaults():
    options = CollectionOptions.model_validate({"collect_query_patterns": True})
    assert (options.query_limit, options.min_executions) == (None, 10)


# -- the live SQL ------------------------------------------------------------------------


def _tools(engine: str) -> Any:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = []
    db: Any = TOOLS[engine](ssm=ssm, host="h", port=1, database="d", secret_arn="s", region="r")
    db.get_version = lambda: "8.0.45"
    db.get_current_schema = lambda: "APP"
    return db


def _statement_sql(db: Any) -> str:
    sql: str = db.ssm.run_sql_json.call_args.kwargs["sql"]
    return sql


@pytest.mark.parametrize("engine", list(TOOLS))
def test_each_live_collector_has_no_cap_and_ten_executions_by_default(engine):
    db = _tools(engine)
    db.collect_query_patterns()
    sql = _statement_sql(db)
    _, floor = SQL_FORMS[engine]
    assert floor.format(min=10) in sql
    if engine == "oracle":
        assert "ROWNUM" not in sql
    else:
        assert f"{CAP_WORDS[engine]} " not in sql.upper().replace("TOP 1 ", "")


@pytest.mark.parametrize("engine", list(TOOLS))
def test_each_live_collector_sends_the_configured_settings(engine):
    db = _tools(engine)
    db.collect_query_patterns(min_executions=3, limit=25000)
    sql = _statement_sql(db)
    cap, floor = SQL_FORMS[engine]
    assert cap.format(limit=25000) in sql
    assert floor.format(min=3) in sql


@pytest.mark.parametrize("engine", list(TOOLS))
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"min_executions": 0},
        {"min_executions": None},
        {"limit": "5; DROP"},
        {"limit": True},
    ],
)
def test_each_live_collector_refuses_a_setting_that_is_not_a_positive_count(engine, kwargs):
    db = _tools(engine)
    with pytest.raises(ValueError):
        db.collect_query_patterns(**kwargs)
    db.ssm.run_sql_json.assert_not_called()


# -- the recorded capture ----------------------------------------------------------------


def test_a_capture_with_no_cap_is_not_truncated():
    capture = live_query_capture(42, CollectionOptions())
    assert capture == QueryCapture(
        query_limit=None, min_executions=10, captured=42, available=None, truncated=False
    )


def test_a_capture_under_an_explicit_cap_is_not_truncated():
    capture = live_query_capture(42, CollectionOptions(query_limit=100))
    assert capture is not None
    assert (capture.query_limit, capture.truncated) == (100, False)


def test_a_failed_statement_query_records_an_error_not_an_empty_capture():
    capture = live_query_capture(0, CollectionOptions(), "the statement query failed (X)")
    assert capture is not None
    assert capture.error == "the statement query failed (X)"
    assert capture.truncated is None and capture.captured == 0


def test_a_capture_that_reaches_the_cap_is_truncated():
    capture = live_query_capture(500, CollectionOptions(query_limit=500, min_executions=2))
    assert capture.truncated is True
    assert (capture.query_limit, capture.min_executions, capture.captured) == (500, 2, 500)
    assert capture.available is None


def test_no_capture_is_recorded_when_query_patterns_are_not_collected():
    assert live_query_capture(0, CollectionOptions(collect_query_patterns=False)) is None


class _Checkpoints:
    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages

    def load_or_run(self, name: str, fn: Any) -> Any:
        return self.stages[name] if name in self.stages else fn()


# engine -> (module, remote collector class name, live entry point, port)
ENGINES = {
    "mysql": (mysql_collector, "MySQLRemoteCollector", "_collect_live", 3306),
    "mariadb": (mariadb_collector, "MariaDBRemoteCollector", "_collect_live_mariadb", 3306),
    "postgresql": (postgres_collector, "PostgreSQLRemoteCollector", "_collect_live", 5432),
    "sqlserver": (sqlserver_collector, "SQLServerRemoteCollector", "_collect_live", 1433),
    "oracle": (oracle_collector, "OracleRemoteCollector", "_collect_live", 1521),
}
TABLE = {
    "table_id": "app.t",
    "table_name": "t",
    "columns": [{"column_name": "id", "data_type": "int", "is_nullable": "NO"}],
}


def _run_live(
    engine: str, options: dict, failure: Exception | None = None
) -> tuple[CollectorOutputContract, MagicMock]:
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
            "collection_options": options,
        }
    )
    stages = {
        "metadata": {"version": "8.0.45", "db_size_gb": 1.0},
        "schema": {"tables": [TABLE], "global_stats": {}},
        "aws_metrics": {},
    }
    remote = MagicMock()
    remote.return_value.collect_query_patterns.return_value = []
    if failure is not None:
        remote.return_value.collect_query_patterns.side_effect = failure
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, remote),
    ):
        result: CollectorOutputContract = getattr(module, entry)(inp, _Checkpoints(stages))
    return result, remote.return_value.collect_query_patterns


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_records_the_default_capture(engine):
    result, collect = _run_live(engine, {})
    # #1908: two snapshots 300 seconds apart by default.
    collect.assert_called_once_with(min_executions=10, limit=None, rate_sample_seconds=300)
    assert result.metadata.query_capture == QueryCapture(
        query_limit=None, min_executions=10, captured=0, available=None, truncated=False
    )


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_whose_statement_query_fails_records_the_error(engine):
    result, _ = _run_live(engine, {}, failure=RuntimeError("password=hunter2"))
    capture = result.metadata.query_capture
    assert capture is not None
    assert capture.error == "the statement query failed (RuntimeError)"
    assert capture.truncated is None and capture.captured == 0
    # The error's own message, which can carry connection details, is not kept.
    assert "hunter2" not in result.model_dump_json()


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_sends_and_records_the_configured_capture(engine):
    result, collect = _run_live(
        engine, {"query_limit": 20_000, "min_executions": 5, "rate_sample_seconds": 0}
    )
    collect.assert_called_once_with(min_executions=5, limit=20_000, rate_sample_seconds=0)
    capture = result.metadata.query_capture
    assert capture is not None
    assert (capture.query_limit, capture.min_executions) == (20_000, 5)


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collection_without_query_patterns_records_no_capture(engine):
    result, collect = _run_live(engine, {"collect_query_patterns": False})
    collect.assert_not_called()
    assert result.metadata.query_capture is None


def test_an_older_collector_output_loads_without_a_capture():
    result, _ = _run_live("mysql", {})
    dumped = result.model_dump(mode="json")
    del dumped["metadata"]["query_capture"]
    loaded = CollectorOutputContract.model_validate(dumped)
    assert loaded.metadata.query_capture is None
