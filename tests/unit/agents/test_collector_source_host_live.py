"""Live and DDL collection fill ``metrics.source_host``'s host figures (#1224).

No real database runs here: every database call is a mocked cursor (an SSM
executor returning canned rows) or a canned checkpoint stage. The offline path
fills the figures from the collection script's ``os_stats`` (#1222); live mode now
reads the engine's own OS views where the connection can (SQL Server
``sys.dm_os_sys_info``, Oracle ``V$OSSTAT``), and live and DDL modes take an RDS
source's vCPU count and memory from ``rds_instance_metadata`` when the database gave
none. MySQL, MariaDB and PostgreSQL have no SQL view of the host's CPU or memory, so
only the RDS fallback fills theirs; a figure neither source has stays empty.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector import (
    mariadb_collector,
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.agents.collector.mysql_collector import _build_rds_metadata, _build_source_host
from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

RDS = {"available": True, "instance_class": "db.r6g.xlarge", "vcpu_count": 4, "memory_gb": 32.0}
TABLE = {
    "table_id": "app.t",
    "table_name": "t",
    "columns": [{"column_name": "id", "data_type": "int", "is_nullable": "NO"}],
}
STATS = {"buffer_pool_read_requests": 1000, "buffer_pool_reads_from_disk": 10}


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
DDL_ENGINES = ["mysql", "postgresql", "sqlserver", "oracle"]


def _input(engine: str, mode: str) -> CollectorInput:
    cfg = (
        {"ddl_config": {"s3_bucket": "b", "s3_key": "k"}}
        if mode == "ddl"
        else {"live_config": {"secret_arn": "s", "automation_instance_id": "i"}}
    )
    return CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": engine,
            "cluster_endpoint": "db.example",
            "port": ENGINES[engine][3],
            "database_name": "app",
            "mode": mode,
            **cfg,
        }
    )


def _run_live(engine: str, schema_extra: dict, aws_raw: dict) -> CollectorOutputContract:
    module, cls, entry, _ = ENGINES[engine]
    schema = {"tables": [TABLE], "global_stats": dict(STATS), **schema_extra}
    stages = {
        "metadata": {"version": "8.0.45", "db_size_gb": 1.0},
        "schema": schema,
        "queries": [],
        "aws_metrics": aws_raw,
    }
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, MagicMock()),
    ):
        result: CollectorOutputContract = getattr(module, entry)(
            _input(engine, "live"), _Checkpoints(stages)
        )
    return result


def _run_ddl(engine: str, aws_raw: dict) -> CollectorOutputContract:
    module = ENGINES[engine][0]
    stages = {"ddl_schema": [TABLE], "aws_metrics": aws_raw}
    with patch.object(module, "_build_cred_mgr", return_value=MagicMock()):
        result: CollectorOutputContract = module._collect_ddl(
            _input(engine, "ddl"), _Checkpoints(stages)
        )
    return result


# --- the database's own OS views (mocked cursors) ------------------------------------


def _sql_call(db: Any) -> Any:
    return db.ssm.run_sql_json.call_args


def _sqlserver(rows: list[dict]) -> SQLServerRemoteCollector:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = rows
    return SQLServerRemoteCollector(
        ssm=ssm, host="h", port=1433, database="d", secret_arn="s", region="us-east-1"
    )  # pragma: allowlist secret


def _oracle(rows: list[dict]) -> OracleRemoteCollector:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = rows
    return OracleRemoteCollector(
        ssm=ssm, host="h", port=1521, database="d", secret_arn="s", region="us-east-1"
    )  # pragma: allowlist secret


def test_sqlserver_reads_cpu_and_memory_from_sys_dm_os_sys_info() -> None:
    db = _sqlserver([{"cpu_count": 16, "physical_memory_kb": 67108864, "committed_kb": 8388608}])
    assert db.collect_os_stats() == {
        "cpu_count": 16,
        "physical_memory_gb": 64.0,
        "committed_memory_gb": 8.0,
    }
    sql = _sql_call(db).kwargs["sql"]
    assert "sys.dm_os_sys_info" in sql
    # Only the host view: no table is read.
    assert "FROM sys.dm_os_sys_info" in sql and sql.count("FROM") == 1


def test_sqlserver_empty_or_unreadable_rows_leave_figures_empty() -> None:
    assert _sqlserver([]).collect_os_stats() == {}
    stats = _sqlserver([{"cpu_count": None, "physical_memory_kb": "x"}]).collect_os_stats()
    assert stats == {"cpu_count": None, "physical_memory_gb": None, "committed_memory_gb": None}


def test_oracle_reads_cpu_and_memory_from_v_osstat() -> None:
    db = _oracle(
        [
            {"stat_name": "NUM_CPUS", "value": "8"},
            {"stat_name": "PHYSICAL_MEMORY_BYTES", "value": str(32 * 1024**3)},
        ]
    )
    assert db.collect_os_stats() == {"cpu_count": 8, "physical_memory_gb": 32.0}
    sql = _sql_call(db).kwargs["sql"]
    assert "V$OSSTAT" in sql and "NUM_CPUS" in sql and "PHYSICAL_MEMORY_BYTES" in sql


def test_oracle_a_missing_statistic_is_left_empty_not_guessed() -> None:
    db = _oracle([{"stat_name": "NUM_CPUS", "value": "8"}])
    assert db.collect_os_stats() == {"cpu_count": 8, "physical_memory_gb": None}
    assert _oracle([]).collect_os_stats() == {"cpu_count": None, "physical_memory_gb": None}


@pytest.mark.parametrize(("module", "cls"), [(sqlserver_collector, "db"), (oracle_collector, "db")])
def test_schema_stage_keeps_os_stats_and_survives_a_denied_view(module: Any, cls: str) -> None:
    db = MagicMock()
    db.collect_tables.return_value = []
    db.collect_os_stats.return_value = {"cpu_count": 2}
    opts = MagicMock(sample_data=False, collect_sample_data=False)
    ok = module._collect_schema_raw(db, opts)
    assert ok["os_stats"] == {"cpu_count": 2}
    db.collect_os_stats.side_effect = RuntimeError("VIEW SERVER STATE permission denied")
    assert module._collect_schema_raw(db, opts)["os_stats"] == {}


# --- the builder ------------------------------------------------------------------------


def test_the_database_figure_wins_over_the_instance_class() -> None:
    rds = _build_rds_metadata(RDS)
    host = _build_source_host(
        {"global_stats": STATS, "os_stats": {"cpu_count": 16, "physical_memory_gb": 64.0}}, rds
    )
    assert host is not None
    assert (host.cpu_count, host.physical_memory_gb) == (16, 64.0)


def test_the_instance_class_fills_what_the_database_did_not_report() -> None:
    host = _build_source_host(
        {"global_stats": STATS, "os_stats": {"cpu_count": 16}}, _build_rds_metadata(RDS)
    )
    assert host is not None
    assert (host.cpu_count, host.physical_memory_gb) == (16, 32.0)
    assert host.committed_memory_gb is None


def test_no_source_means_no_figure_and_none_without_a_ratio() -> None:
    host = _build_source_host({"global_stats": STATS})
    assert host is not None
    assert host.cpu_count is None and host.physical_memory_gb is None
    assert _build_source_host({}, _build_rds_metadata({"available": True})) is None


def test_an_rds_instance_with_unknown_class_leaves_the_figures_empty() -> None:
    rds = _build_rds_metadata({"available": True, "instance_class": "db.weird.huge"})
    host = _build_source_host({"global_stats": STATS}, rds)
    assert host is not None and host.cpu_count is None and host.physical_memory_gb is None


# --- every engine's live and DDL path -----------------------------------------------------


@pytest.mark.parametrize("engine", list(ENGINES))
def test_live_rds_source_gets_cpu_and_memory_from_the_instance_class(engine: str) -> None:
    host = _run_live(engine, {}, {"rds_metadata": RDS}).metrics.source_host
    assert host is not None
    assert (host.cpu_count, host.physical_memory_gb) == (4, 32.0)
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(99.0)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", list(ENGINES))
def test_live_self_managed_source_without_a_host_view_leaves_figures_empty(engine: str) -> None:
    host = _run_live(engine, {}, {}).metrics.source_host
    assert host is not None
    assert host.cpu_count is None and host.physical_memory_gb is None
    assert host.committed_memory_gb is None


@pytest.mark.parametrize("engine", ["sqlserver", "oracle"])
def test_live_reads_the_host_from_the_engines_own_view_before_the_instance_class(
    engine: str,
) -> None:
    os_stats = {"cpu_count": 16, "physical_memory_gb": 64.0}
    if engine == "sqlserver":
        os_stats["committed_memory_gb"] = 8.0
    host = _run_live(engine, {"os_stats": os_stats}, {"rds_metadata": RDS}).metrics.source_host
    assert host is not None
    assert (host.cpu_count, host.physical_memory_gb) == (16, 64.0)
    assert host.committed_memory_gb == (8.0 if engine == "sqlserver" else None)


@pytest.mark.parametrize("engine", DDL_ENGINES)
def test_ddl_rds_source_gets_cpu_and_memory_from_the_instance_class(engine: str) -> None:
    aws = {"rds_metadata": RDS, "pi_counters": {"cache_hit_ratio_pct": 98.5}}
    host = _run_ddl(engine, aws).metrics.source_host
    assert host is not None
    assert (host.cpu_count, host.physical_memory_gb) == (4, 32.0)
    assert host.buffer_cache_hit_ratio_pct == 98.5


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", DDL_ENGINES)
def test_ddl_without_rds_metadata_has_no_host_figures(engine: str) -> None:
    host = _run_ddl(engine, {"pi_counters": {"cache_hit_ratio_pct": 98.5}}).metrics.source_host
    assert host is not None
    assert host.cpu_count is None and host.physical_memory_gb is None


@pytest.mark.passes_on_main
def test_a_collector_output_saved_before_this_still_loads() -> None:
    old = _run_ddl("mysql", {"pi_counters": {"cache_hit_ratio_pct": 98.5}}).model_dump(mode="json")
    old["metrics"]["source_host"] = {"buffer_cache_hit_ratio_pct": 98.5}
    host = CollectorOutputContract.model_validate(old).metrics.source_host
    assert host is not None and host.cpu_count is None
