# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collection records the server's own counters (#1991).

The rate window check needs to know how long the server's counters could have covered
(its uptime, or the time since PostgreSQL's statement statistics were reset), when they
were read, and, for MySQL and MariaDB, the server's own statement count since it
started (``Questions``), which no digest reset touches. The offline parser reads them
from the collection scripts' sections; the live collectors read them with one read-only
query each. Synthetic collections only; no database runs here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.collector import (
    mariadb_collector,
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import load_contract
from src.shared.server_counters import live_server_counters, offline_server_counters
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 -- test fixture ARN, not a credential
AT = "2026-11-05T14:31:00Z"
UP_198_DAYS = 198 * 86_400


def _collection(engine: str, **sections) -> dict:
    return {
        "collected_at": AT,
        "metadata": {"database_name": "shop", "engine": engine, "version": "x"},
        "tables": [{"table_name": "cart", "table_rows": 1}],
        "columns": [
            {"table_name": "cart", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": [],
        **sections,
    }


def test_a_mysql_collection_records_its_uptime_and_questions():
    raw = _collection("mysql", global_stats={"uptime": str(UP_198_DAYS), "questions": "20142722"})
    assert offline_server_counters(raw, "mysql") == {
        "uptime_seconds": float(UP_198_DAYS),
        "stats_reset_seconds": None,
        "read_at": AT,
        "statements_since_start": 20_142_722,
    }


def test_a_postgresql_collection_records_its_uptime_and_reset_but_not_its_digest_sum():
    """PostgreSQL's ``questions`` is the sum of pg_stat_statements' own calls: the same
    counters the rates come from, so it is no independent rate."""
    raw = _collection(
        "postgresql",
        global_stats={"uptime": "864000", "stats_reset_age_seconds": "1680.5", "questions": "9"},
    )
    assert offline_server_counters(raw, "postgresql") == {
        "uptime_seconds": 864_000.0,
        "stats_reset_seconds": 1680.5,
        "read_at": AT,
        "statements_since_start": None,
    }


@pytest.mark.parametrize("engine", ["sqlserver", "oracle"])
def test_sql_server_and_oracle_record_their_uptime(engine):
    raw = _collection(engine, os_stats={"db_uptime_seconds": 7200, "db_uptime_hours": 2})
    counters = offline_server_counters(raw, engine)
    assert counters is not None
    assert counters["uptime_seconds"] == 7200.0 and counters["statements_since_start"] is None


def test_an_older_collection_with_uptime_in_hours_still_records_it():
    raw = _collection("sqlserver", os_stats={"db_uptime_hours": 3})
    counters = offline_server_counters(raw, "sqlserver")
    assert counters is not None and counters["uptime_seconds"] == 10_800.0


def test_a_collection_with_no_counters_records_none():
    raw = _collection("mysql")
    del raw["collected_at"]
    assert offline_server_counters(raw, "mysql") is None


@pytest.mark.parametrize("bad", ["", "x", "-5", "0", None, True, "nan"])
def test_an_unreadable_figure_is_left_out(bad):
    raw = _collection("mysql", global_stats={"uptime": bad, "questions": bad})
    counters = offline_server_counters(raw, "mysql")
    assert counters is not None
    assert counters["uptime_seconds"] is None and counters["statements_since_start"] in (None, 0)


def test_the_counters_reach_the_written_contract():
    raw = _collection("mysql", global_stats={"uptime": str(UP_198_DAYS), "questions": "100"})
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job-1", database_name="shop", s3_key="k"
    )
    loaded = load_contract(CollectorOutputContract, out, artifact="collector/output.json")
    counters = loaded.metadata.server_counters
    assert counters is not None
    assert counters.uptime_seconds == UP_198_DAYS
    assert counters.statements_since_start == 100
    assert counters.read_at is not None and counters.read_at.isoformat().startswith(
        "2026-11-05T14:31"
    )


def test_an_older_collector_output_loads_without_them():
    raw = _collection("mysql")
    del raw["collected_at"]
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job-1", database_name="shop", s3_key="k"
    )
    assert out["metadata"]["server_counters"] is None
    out["metadata"].pop("server_counters")
    assert CollectorOutputContract.model_validate(out).metadata.server_counters is None


# -- live -----------------------------------------------------------------------------


def _collector(cls, answer):
    c = cls(ssm=MagicMock(), host="h", port=1, database="db", secret_arn=SECRET)
    sent: list[str] = []

    def query(sql: str) -> list[dict]:
        sent.append(sql)
        rows: list[dict] = answer(sql.lower())
        return rows

    c._query = query  # type: ignore[method-assign]
    return c, sent


def _mysql_answer(sql: str) -> list[dict]:
    if "'uptime'" in sql:
        return [{"Variable_name": "Uptime", "Value": str(UP_198_DAYS)}]
    if "'questions'" in sql:
        return [{"Variable_name": "Questions", "Value": "500"}]
    raise AssertionError(sql)


@pytest.mark.parametrize("cls", [MySQLRemoteCollector, MariaDBRemoteCollector])
def test_a_live_mysql_collector_reads_its_uptime_and_questions(cls):
    c, sent = _collector(cls, _mysql_answer)
    counters = live_server_counters(c, read_at=AT)
    assert counters == {
        "uptime_seconds": float(UP_198_DAYS),
        "stats_reset_seconds": None,
        "read_at": AT,
        "statements_since_start": 500,
    }
    assert all(s.strip().upper().startswith("SHOW GLOBAL STATUS") for s in sent)


def test_a_live_postgresql_collector_reads_its_reset_age_and_its_uptime():
    def answer(sql: str) -> list[dict]:
        if "pg_postmaster_start_time()" in sql:
            return [{"uptime_seconds": 864_000.0}]
        return [{"stats_reset_seconds": 1680.0}]

    c, sent = _collector(PostgreSQLRemoteCollector, answer)
    counters = live_server_counters(c, read_at=AT)
    assert counters is not None
    assert (counters["uptime_seconds"], counters["stats_reset_seconds"]) == (864_000.0, 1680.0)
    assert all(s.strip().upper().startswith("SELECT") for s in sent)


@pytest.mark.parametrize("cls", [SQLServerRemoteCollector, OracleRemoteCollector])
def test_a_live_sql_server_or_oracle_collector_reads_its_uptime(cls):
    c, _ = _collector(cls, lambda sql: [{"uptime_seconds": 7200}])
    counters = live_server_counters(c, read_at=AT)
    assert counters is not None and counters["uptime_seconds"] == 7200.0


def test_a_live_read_that_fails_records_nothing():
    def fail(sql: str) -> list[dict]:
        raise RuntimeError("no access")

    c, _ = _collector(MySQLRemoteCollector, fail)
    counters = live_server_counters(c, read_at=AT)
    assert counters is not None
    assert counters["uptime_seconds"] is None and counters["statements_since_start"] is None


# -- the live collectors' wiring (review of #2045) ----------------------------------------


class _Checkpoints:
    """Canned stage results: no AWS, no database."""

    def __init__(self, stages: dict) -> None:
        self.stages = stages

    def load_or_run(self, name: str, fn):  # type: ignore[no-untyped-def]
        return self.stages[name] if name in self.stages else fn()


LIVE = {
    "mysql": (mysql_collector, "_collect_live", "MySQLRemoteCollector", "mysql", "8.0.36"),
    "mariadb": (
        mariadb_collector,
        "_collect_live_mariadb",
        "MariaDBRemoteCollector",
        "mariadb",
        "10.11.6-MariaDB",
    ),
    "postgresql": (
        postgres_collector,
        "_collect_live",
        "PostgreSQLRemoteCollector",
        "postgresql",
        "PostgreSQL 16.4 on x86_64",
    ),
    "sqlserver": (
        sqlserver_collector,
        "_collect_live",
        "SQLServerRemoteCollector",
        "sqlserver",
        "Microsoft SQL Server 2022 (RTM) - 16.0.1000.6",
    ),
    "oracle": (
        oracle_collector,
        "_collect_live",
        "OracleRemoteCollector",
        "oracle",
        "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0",
    ),
}


@pytest.mark.parametrize("engine", list(LIVE))
def test_every_live_collector_writes_the_server_counters(engine):
    """Each live collector passes the counters it read to ``metadata.server_counters``."""
    from unittest.mock import patch

    from src.contracts.collector_input import CollectorInput

    module, entry, remote, engine_id, version = LIVE[engine]
    inp = CollectorInput.model_validate(
        {
            "job_id": "j1991",
            "engine": engine_id,
            "cluster_endpoint": "db.example",
            "port": 1,
            "database_name": "app",
            "mode": "live",
            "live_config": {"secret_arn": "s", "automation_instance_id": "i"},
        }
    )
    table = {
        "table_id": "app.t",
        "table_name": "t",
        "columns": [{"column_name": "id", "data_type": "integer", "is_nullable": "NO"}],
    }
    stages = {
        "metadata": {"version": version, "db_size_gb": 1.0},
        "schema": {"tables": [table], "global_stats": {}},
        "queries": [],
        "aws_metrics": {},
    }
    counters = {
        "uptime_seconds": float(UP_198_DAYS),
        "stats_reset_seconds": None,
        "read_at": AT,
        "statements_since_start": 500,
    }
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, remote, MagicMock()),
        patch.object(module, "live_server_counters", return_value=counters) as read,
    ):
        contract = getattr(module, entry)(inp, _Checkpoints(stages))
    read.assert_called_once()
    written = contract.metadata.server_counters
    assert written is not None
    assert written.uptime_seconds == UP_198_DAYS and written.statements_since_start == 500
