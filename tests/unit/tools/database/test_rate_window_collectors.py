# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collector divides an execution count by the window it covers (#1893).

The offline parser and the SQL Server, MySQL, MariaDB, PostgreSQL and Oracle live
collectors all go through ``src.shared.rate_window``: a statement's own first/last seen
window, else the server's uptime (or the time since its statistics reset), else 24
hours, recorded with the rate. Synthetic statements only.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.collector.mysql_collector import _build_queries, _dict_to_query_pattern
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, QueryPattern
from src.shared.contract_versioning import load_contract
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

REPO = Path(__file__).resolve().parents[4]
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 -- test fixture ARN, not a credential

FIRST, LAST = "2026-11-05 02:00:00.000", "2026-11-05 04:00:00.000"  # two hours
COUNT = 72_000  # 10 calls/s over the two hours; 0.833/s over 24 hours
UPTIME_S = 14_400  # four hours: 5 calls/s


def _collector(cls):
    return cls(ssm=MagicMock(), host="h", port=1, database="db", secret_arn=SECRET)


def _live(engine: str, *, seen: bool, uptime: object):
    """A live collector whose pattern query returns two statements (with or without their
    own window) and whose server-window read returns ``uptime`` (None: the read fails):
    the uptime for MySQL, MariaDB, SQL Server and Oracle, the stats reset age for
    PostgreSQL."""
    cls = {
        "mysql": MySQLRemoteCollector,
        "mariadb": MariaDBRemoteCollector,
        "postgresql": PostgreSQLRemoteCollector,
        "sqlserver": SQLServerRemoteCollector,
        "oracle": OracleRemoteCollector,
    }[engine]
    c = _collector(cls)
    c.get_version = lambda: "10.11.6-MariaDB" if engine == "mariadb" else "8.0.36"  # type: ignore[method-assign]
    c.get_current_schema = lambda: "APP"  # type: ignore[method-assign]
    text = "SELECT id FROM t WHERE id = 1"
    if engine in ("mysql", "mariadb"):
        row = {"digest": "d", "query_text": text, "execution_count": COUNT}
        if seen:
            row |= {"FIRST_SEEN": FIRST, "LAST_SEEN": LAST}
    elif engine == "postgresql":
        row = {"queryid": 1, "query": text, "calls": COUNT}
        if seen:  # PostgreSQL 17: now() - stats_since, computed by the server
            row |= {"stats_since_age_seconds": 7_200.0}
    elif engine == "sqlserver":
        row = {"query_id": "q", "query_text": text, "execution_count": COUNT}
        if seen:
            row |= {"creation_time": FIRST, "last_execution_time": LAST}
    else:
        row = {"query_id": "q", "query_text": text, "execution_count": COUNT}
        if seen:
            row |= {"first_seen": FIRST.replace(" ", "/"), "last_seen": LAST[:19]}
    sent: list[str] = []

    def query(sql: str) -> list[dict]:
        sent.append(sql)
        if _reads_the_server_window(sql):
            if uptime is None:
                raise RuntimeError("permission denied")
            if engine in ("mysql", "mariadb"):
                return [{"Variable_name": "Uptime", "Value": uptime}]
            if engine == "postgresql":
                return [{"stats_reset_seconds": uptime}]
            return [{"UPTIME_SECONDS" if engine == "oracle" else "uptime_seconds": uptime}]
        second = {
            k: (f"{v}2" if k in ("digest", "queryid", "query_id") else v) for k, v in row.items()
        }
        return [row, second]

    c._query = query  # type: ignore[method-assign]
    return c, sent


def _reads_the_server_window(sql: str) -> bool:
    lowered = sql.lower()
    return (
        "uptime" in lowered or "stats_reset" in lowered
    ) and "pg_stat_statements s" not in lowered


ENGINES = ["mysql", "mariadb", "postgresql", "sqlserver", "oracle"]
SERVER_BASIS = {e: "stats_reset" if e == "postgresql" else "uptime" for e in ENGINES}


@pytest.mark.parametrize("engine", ENGINES)
def test_a_live_statement_uses_its_own_observed_window(engine):
    c, sent = _live(engine, seen=True, uptime=UPTIME_S)
    p = c.collect_query_patterns()[0]
    # Every statement has its own window: the server window is never read.
    assert not [s for s in sent if _reads_the_server_window(s)]
    assert p["rate_window_basis"] == "observed"
    assert p["rate_window_seconds"] == 7_200
    assert p["calls_per_second"] == pytest.approx(10.0)
    assert p["frequency_per_hour"] == pytest.approx(36_000)


@pytest.mark.parametrize("engine", ENGINES)
def test_a_live_statement_with_no_window_uses_the_server_window(engine):
    c, sent = _live(engine, seen=False, uptime=str(UPTIME_S))
    patterns = c.collect_query_patterns()
    assert len(patterns) == 2
    p = patterns[1]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == (SERVER_BASIS[engine], UPTIME_S)
    assert p["calls_per_second"] == pytest.approx(5.0)
    # Read once, however many statements need it.
    assert len([s for s in sent if _reads_the_server_window(s)]) == 1


@pytest.mark.parametrize("engine", ENGINES)
def test_a_live_collector_that_cannot_read_the_server_window_assumes_24_hours(engine):
    c, _ = _live(engine, seen=False, uptime=None)
    p = c.collect_query_patterns()[0]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("assumed_24h", 86_400)
    assert p["calls_per_second"] == COUNT / (24 * 3600)
    assert p["frequency_per_hour"] == COUNT / 24


@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_mysql_and_mariadb_read_the_uptime_with_show_global_status(engine):
    """``SHOW GLOBAL STATUS`` answers on MySQL 5.6 and MariaDB before 10.5, where
    ``performance_schema.global_status`` does not exist."""
    c, sent = _live(engine, seen=False, uptime=UPTIME_S)
    c.collect_query_patterns()
    assert "SHOW GLOBAL STATUS LIKE 'Uptime'" in sent
    assert not [s for s in sent if "performance_schema.global_status" in s and "Uptime" in s]


def test_live_postgres_never_reads_the_uptime():
    """The review's case: 30 days of counts at 1 call/s survive a restart 10 minutes ago
    (``pg_stat_statements.save``). The window is the 30 days since the reset."""
    c = _collector(PostgreSQLRemoteCollector)
    days = 30 * 86_400
    sent: list[str] = []

    def query(sql: str) -> list[dict]:
        sent.append(sql)
        if "pg_postmaster_start_time" in sql:
            return [{"uptime_seconds": 600}]
        if _reads_the_server_window(sql):
            return [{"stats_reset_seconds": days}]
        return [{"queryid": 1, "query": "SELECT 1 FROM t", "calls": days}]

    c._query = query  # type: ignore[method-assign]
    [p] = c.collect_query_patterns()
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("stats_reset", days)
    assert p["calls_per_second"] == pytest.approx(1.0)
    assert not [s for s in sent if "pg_postmaster_start_time" in s]


def test_live_postgres_asks_for_stats_since_without_naming_the_column():
    """``to_jsonb(s) ->> 'stats_since'`` is NULL before PostgreSQL 17, where naming the
    column would fail the query."""
    c, sent = _live("postgresql", seen=False, uptime=None)
    c.collect_query_patterns()
    [pattern_sql] = [s for s in sent if "pg_stat_statements s" in s]
    assert "to_jsonb(s) ->> 'stats_since'" in pattern_sql
    assert "AS stats_since_age_seconds" in pattern_sql


@pytest.mark.parametrize("engine", ENGINES)
def test_the_live_rate_fields_reach_the_contract(engine):
    c, _ = _live(engine, seen=True, uptime=UPTIME_S)
    p = c.collect_query_patterns()[0]
    pattern = _dict_to_query_pattern(p, source_engine=engine)
    assert pattern.rate_window_basis == p["rate_window_basis"]
    assert pattern.rate_window_seconds == p["rate_window_seconds"]


# ---------------------------------------------------------------------------
# Offline collections
# ---------------------------------------------------------------------------


def _collection(engine: str, queries: list[dict], **sections) -> dict:
    meta = {"database_name": "shop", "engine": engine, "version": "x"}
    return {
        "metadata": meta,
        "tables": [{"table_name": "cart", "table_rows": 1}],
        "columns": [
            {"table_name": "cart", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": queries,
        **sections,
    }


def _q(digest: str, count: int, first=None, last=None) -> dict:
    row = {
        "digest": digest,
        "query_text": "SELECT * FROM cart WHERE id = 1",
        "execution_count": count,
        "total_time_ms": 1.0,
    }
    if first is not None:
        row["first_seen"] = first
    if last is not None:
        row["last_seen"] = last
    return row


def _by_id(parsed: dict) -> dict[str, dict]:
    return {q["query_id"]: q for q in parsed["queries"]}


def test_an_offline_sql_server_collection_reads_each_window_and_the_uptime_hours():
    raw = _collection(
        "sqlserver",
        [
            _q("0x01", COUNT, FIRST, LAST),  # observed: 10/s
            _q("0x02", 1, LAST, LAST),  # one execution: the uptime
            _q("0x03", 500, LAST, FIRST),  # clock skew: the uptime
            _q("0x04", 600),  # no times: the uptime
        ],
        os_stats={"cpu_count": 4, "db_uptime_hours": 4},
    )
    got = _by_id(parse_offline_collection(raw))
    assert (got["0x01"]["rate_window_basis"], got["0x01"]["calls_per_second"]) == (
        "observed",
        pytest.approx(10.0),
    )
    for qid in ("0x02", "0x03", "0x04"):
        assert got[qid]["rate_window_basis"] == "uptime"
        assert got[qid]["rate_window_seconds"] == UPTIME_S
    assert got["0x04"]["calls_per_second"] == pytest.approx(600 / UPTIME_S)


def test_an_offline_sql_server_collection_prefers_the_uptime_in_seconds():
    """``DATEDIFF(HOUR, ...)`` counts hour boundaries; the script also writes seconds."""
    raw = _collection(
        "sqlserver", [_q("0x04", 600)], os_stats={"db_uptime_hours": 2, "db_uptime_seconds": 3_660}
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("uptime", 3_660)


def test_an_offline_mysql_collection_reads_the_global_uptime():
    raw = _collection("mysql", [_q("d1", 1_440)], global_stats={"uptime": "14400"})
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("uptime", UPTIME_S)
    assert p["calls_per_second"] == pytest.approx(0.1)


def test_an_offline_postgres_collection_reads_the_stats_reset_never_the_uptime():
    """The review's case: 30 days of counts at 1 call/s since the reset, a restart 10
    minutes ago (``pg_stat_statements.save`` keeps the counts)."""
    days = 30 * 86_400
    raw = _collection(
        "postgresql",
        [_q("d1", days)],
        global_stats={"uptime": "600", "stats_reset_age_seconds": str(days)},
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("stats_reset", days)
    assert p["calls_per_second"] == pytest.approx(1.0)


def test_an_offline_postgres_collection_with_only_an_uptime_assumes_24_hours():
    """Before PostgreSQL 14 (no ``pg_stat_statements_info``), or a collection written
    before #1893: the uptime is not the counters' window, so the rates assume 24 hours."""
    raw = _collection(
        "postgresql",
        [_q("d1", 1_440)],
        global_stats={"uptime": "14400", "stats_reset_age_seconds": None},
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("assumed_24h", 86_400)
    assert p["calls_per_second"] == 1_440 / (24 * 3600)


def test_an_offline_postgres_17_collection_uses_each_entrys_stats_since():
    row = _q("d1", 7_200) | {"stats_since_age_seconds": 3_600.0}
    variant = _q("d1", 3_600) | {"stats_since_age_seconds": 7_200.0, "total_time_ms": 0.5}
    raw = _collection("postgresql", [row, variant], global_stats={"stats_reset_age_seconds": "9e6"})
    [p] = parse_offline_collection(raw)["queries"]
    # The merged entry counts since its oldest variant started.
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("observed", 7_200)
    assert p["calls_per_second"] == pytest.approx(10_800 / 7_200)


def test_an_offline_collection_with_no_window_keeps_the_24_hour_rates():
    raw = _collection("mysql", [_q("d1", 1_440)], os_stats={"cpu_count": 4})
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("assumed_24h", 86_400)
    assert p["calls_per_second"] == 1_440 / (24 * 3600)
    assert p["frequency_per_hour"] == 1_440 / 24


def test_merged_variants_take_the_widest_window():
    """Rows of one shape merge first (#386): the summed count over the earliest first
    seen to the latest last seen."""
    raw = _collection(
        "sqlserver",
        [
            _q("0x01", 3_600, "2026-11-05 02:00:00.000", "2026-11-05 03:00:00.000"),
            _q("0x01", 3_600, "2026-11-05 03:30:00.000", "2026-11-05 04:00:00.000"),
        ],
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("observed", 7_200)
    assert p["calls_per_second"] == pytest.approx(1.0)


def test_the_offline_rate_fields_reach_the_written_contract_and_an_old_output_loads():
    raw = _collection("sqlserver", [_q("0x01", COUNT, FIRST, LAST)])
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job-1", database_name="shop", s3_key="k"
    )
    [q] = out["queries"]["query_patterns"]
    assert (q["rate_window_basis"], q["rate_window_seconds"]) == ("observed", 7_200)
    assert q["calls_per_second"] == pytest.approx(10.0)
    # An output written before #1893 has neither field and loads with None for both.
    old = copy.deepcopy(out)
    for pattern in old["queries"]["query_patterns"]:
        del pattern["rate_window_basis"], pattern["rate_window_seconds"]
    loaded = load_contract(CollectorOutputContract, old, artifact="collector/output.json")
    [p] = loaded.queries.query_patterns
    assert (p.rate_window_basis, p.rate_window_seconds) == (None, None)


def test_the_contract_refuses_an_unknown_basis_and_a_zero_window():
    base = {"query_id": "q", "query_text": "SELECT 1", "frequency_per_hour": 1}
    base["tables_accessed"] = ["t"]
    with pytest.raises(ValueError):
        QueryPattern.model_validate({**base, "rate_window_basis": "guessed"})
    with pytest.raises(ValueError):
        QueryPattern.model_validate({**base, "rate_window_seconds": 0})


def test_the_offline_builder_keeps_the_fields():
    [p] = _build_queries(
        [
            {
                "query_id": "q",
                "query_text": "SELECT 1",
                "frequency_per_hour": 1.0,
                "tables_accessed": ["t"],
                "rate_window_seconds": 61.0,
                "rate_window_basis": "observed",
            }
        ]
    ).query_patterns
    assert (p.rate_window_seconds, p.rate_window_basis) == (61.0, "observed")


# ---------------------------------------------------------------------------
# One helper, no copies
# ---------------------------------------------------------------------------

COLLECTORS = [
    "src/tools/database/offline_parser.py",
    "src/tools/database/mysql_tools.py",
    "src/tools/database/mariadb_tools.py",
    "src/tools/database/postgres_tools.py",
    "src/tools/database/sqlserver_tools.py",
    "src/tools/database/oracle_tools.py",
]


@pytest.mark.parametrize("path", COLLECTORS)
def test_no_collector_computes_a_rate_itself(path):
    """Every collector takes its rates from ``statement_rate_fields``; none divides a
    count by a window of its own (the ``/ 24`` and ``/ (24 * 3600)`` of before #1893)."""
    text = (REPO / path).read_text()
    assert "statement_rate_fields(" in text
    assert not re.search(r'"(calls_per_second|frequency_per_hour)":\s*[^*\s]', text)
    assert not re.search(r"/\s*\(?\s*24\b", text)


def test_the_live_postgres_builder_keeps_the_fields():
    from src.agents.collector.postgres_collector import _build_queries as build_postgres

    c, _ = _live("postgresql", seen=False, uptime=UPTIME_S)
    p = build_postgres(c.collect_query_patterns()).query_patterns[0]
    assert (p.rate_window_seconds, p.rate_window_basis) == (UPTIME_S, "stats_reset")


def test_the_collection_scripts_write_each_engines_server_window():
    """The offline scripts write what ``_collection_server_window`` reads (#1893):
    PostgreSQL the stats reset age, behind a version check, and each entry's
    ``stats_since`` without naming the column (PostgreSQL 17); SQL Server and Oracle the
    uptime in seconds."""
    pg = (REPO / "scripts" / "collect-postgresql.sql").read_text()
    assert "to_regclass('pg_stat_statements_info') IS NOT NULL AS has_pgss_info \\gset" in pg
    assert "'stats_reset_age_seconds', (:pgss_reset_age)::text" in pg
    assert "(to_jsonb(s) ->> 'stats_since')::timestamptz" in pg
    assert "AS stats_since_age_seconds" in pg
    sqlserver = (REPO / "scripts" / "collect-sqlserver.sql").read_text()
    assert "DATEDIFF(SECOND, sqlserver_start_time, GETDATE()) AS db_uptime_seconds" in sqlserver
    oracle = (REPO / "scripts" / "collect-oracle.sql").read_text()
    assert "'db_uptime_seconds' VALUE (SELECT ROUND((SYSDATE - STARTUP_TIME) * 86400)" in oracle
