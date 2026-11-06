# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collector measures call rates over the load period from two snapshots (#1908).

The offline parser reads the collection scripts' two reads (``metadata.rate_sample`` and
each row's ``sample_first_count``); the SQL Server, MySQL, MariaDB, PostgreSQL and
Oracle live collectors read a snapshot, wait, run their statement query and read a
second snapshot. Each goes through ``src.shared.rate_window``. Synthetic statements
only; no database runs here.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector import handler
from src.agents.collector.mysql_collector import _build_queries
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_input import CollectionOptions
from src.contracts.collector_output import CollectorOutputContract
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


# -- offline: the scripts' two reads ----------------------------------------------------


def _collection(engine: str, queries: list[dict], sample: dict | None, **sections) -> dict:
    meta: dict = {"database_name": "shop", "engine": engine, "version": "x"}
    if sample is not None:
        meta["rate_sample"] = sample
    return {
        "metadata": meta,
        "tables": [{"table_name": "cart", "table_rows": 1}],
        "columns": [
            {"table_name": "cart", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": queries,
        **sections,
    }


def _q(digest: str, count: int, first_count: int | None, **extra) -> dict:
    row = {
        "digest": digest,
        "query_text": "SELECT * FROM cart WHERE id = 1",
        "execution_count": count,
        "total_time_ms": 1.0,
        "first_seen": FIRST,
        "last_seen": LAST,
        **extra,
    }
    if first_count is not None:
        row["sample_first_count"] = first_count
    return row


def _by_id(raw: dict) -> dict[str, dict]:
    return {q["query_id"]: q for q in parse_offline_collection(raw)["queries"]}


SAMPLE = {"seconds": 300, "elapsed_seconds": 300.0}
UPTIME_DAY = {"os_stats": {"db_uptime_seconds": 86_400}}


def test_an_offline_collection_rates_each_statement_over_the_interval():
    got = _by_id(
        _collection(
            "sqlserver",
            [
                _q("0x01", 81_000, 72_000),  # 9,000 calls in 300 s: 30/s
                _q("0x02", 600, None),  # only in the second read: 2/s
                _q("0x03", 500, 500),  # no calls in the interval
                _q("0x04", 400, 900),  # its count went down
            ],
            SAMPLE,
            **UPTIME_DAY,
        )
    )
    assert (got["0x01"]["rate_window_basis"], got["0x01"]["calls_per_second"]) == ("sampled", 30)
    assert got["0x01"]["rate_window_seconds"] == 300.0
    assert got["0x01"]["rate_sample"]["status"] == "both_snapshots"
    assert (got["0x02"]["calls_per_second"], got["0x02"]["rate_sample"]["status"]) == (
        2,
        "second_snapshot_only",
    )
    # No calls: at most one call over the interval (500 over two hours is above that).
    assert got["0x03"]["rate_sample"]["status"] == "no_calls_in_interval"
    assert (got["0x03"]["rate_window_basis"], got["0x03"]["calls_per_second"]) == (
        "sampled",
        pytest.approx(1 / 300),
    )
    assert got["0x04"]["rate_window_basis"] == "observed"
    assert got["0x04"]["rate_window_seconds"] == 7_200
    assert got["0x04"]["rate_sample"]["status"] == "count_decreased"


@pytest.mark.parametrize(
    ("engine", "sections"),
    [
        ("sqlserver", {"os_stats": {"db_uptime_seconds": 120}}),
        ("oracle", {"os_stats": {"db_uptime_seconds": 120}}),
        ("mysql", {"global_stats": {"uptime": "120"}}),
        ("postgresql", {"global_stats": {"stats_reset_age_seconds": "120.5"}}),
    ],
)
def test_a_reset_inside_the_interval_keeps_every_single_snapshot_rate(engine, sections):
    """The server's window (uptime; PostgreSQL's stats reset age) is shorter than the
    interval: its counters started again inside it, so no difference means anything."""
    [p] = parse_offline_collection(_collection(engine, [_q("q1", 9_000, 100)], SAMPLE, **sections))[
        "queries"
    ]
    assert p["rate_sample"]["status"] == "counters_reset"
    assert p["rate_window_basis"] != "sampled"


@pytest.mark.parametrize(
    "sample",
    [
        {"seconds": 300, "elapsed_seconds": None},
        {"seconds": 300},
        {"seconds": 300, "elapsed_seconds": 0},
    ],
    ids=["no-wait", "no-elapsed", "zero-elapsed"],
)
def test_an_offline_interval_the_clock_did_not_measure_is_recorded(sample):
    """Oracle's script records no elapsed time when neither sleep could run."""
    [p] = parse_offline_collection(_collection("oracle", [_q("q1", 9_000, 100)], sample))["queries"]
    assert p["rate_window_basis"] == "observed"
    assert p["rate_sample"]["status"] == "unavailable"


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "sample", [None, {"seconds": 0, "elapsed_seconds": None}], ids=["older-script", "zero"]
)
def test_an_offline_single_snapshot_is_todays_collection_bit_for_bit(sample):
    rows = [_q("q1", 9_000, None), _q("q2", 50, None)]
    today = parse_offline_collection(_collection("mysql", rows, None))["queries"]
    got = parse_offline_collection(_collection("mysql", rows, sample))["queries"]
    assert got == today
    assert all("rate_sample" not in p for p in got)


def test_the_mysql_scripts_first_counts_pair_with_each_rows_sample_key():
    """The MySQL script writes its first read in ``metadata.rate_sample.first_counts``
    (no JSON_TABLE, so MariaDB 10.5 runs it), keyed as each row's ``sample_key``."""
    key = "d1/2026-11-05 02:00:00.000000"
    sample = SAMPLE | {"first_counts": {key: 72_000, "gone/x": 5}}
    rows = [_q("d1", 81_000, None, sample_key=key), _q("d2", 30, None, sample_key="d2/new")]
    got = _by_id(_collection("mysql", rows, sample, global_stats={"uptime": "86400"}))
    assert got["d1"]["rate_sample"]["first_count"] == 72_000
    assert got["d1"]["calls_per_second"] == pytest.approx(30)
    assert got["d2"]["rate_sample"]["status"] == "second_snapshot_only"


def test_merged_variants_sum_the_first_counts_they_have():
    """Literal variants of one shape: a variant only in the second read added every call
    it counts in the interval."""
    raw = _collection(
        "sqlserver",
        [_q("0x01", 1_000, 400), _q("0x01", 300, None), _q("0x01", 200, 200)],
        SAMPLE,
        **UPTIME_DAY,
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert p["rate_sample"]["first_count"] == 600
    assert p["rate_sample"]["second_count"] == 1_500
    assert p["calls_per_second"] == pytest.approx(900 / 300)
    none = _collection("sqlserver", [_q("0x01", 10, None), _q("0x01", 20, None)], SAMPLE)
    [p] = parse_offline_collection(none)["queries"]
    assert p["rate_sample"]["status"] == "second_snapshot_only"


def test_the_sampled_fields_reach_the_written_contract():
    raw = _collection("sqlserver", [_q("0x01", 81_000, 72_000)], SAMPLE, **UPTIME_DAY)
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job-1", database_name="shop", s3_key="k"
    )
    loaded = load_contract(CollectorOutputContract, out, artifact="collector/output.json")
    [p] = loaded.queries.query_patterns
    assert p.rate_window_basis == "sampled" and p.calls_per_second == pytest.approx(30)
    assert p.rate_sample is not None
    assert (p.rate_sample.first_count, p.rate_sample.second_count) == (72_000, 81_000)
    assert p.rate_sample.elapsed_seconds == 300.0


def test_the_builders_keep_the_sample():
    from src.agents.collector.postgres_collector import _build_queries as build_postgres

    row = {
        "query_id": "q",
        "query_text": "SELECT 1",
        "frequency_per_hour": 3600.0,
        "tables_accessed": ["t"],
        "rate_window_seconds": 300.0,
        "rate_window_basis": "sampled",
        "rate_sample": {
            "status": "both_snapshots",
            "first_count": 1,
            "second_count": 301,
            "elapsed_seconds": 300.0,
        },
    }
    for build in (_build_queries, build_postgres):
        [p] = build([row]).query_patterns
        assert p.rate_window_basis == "sampled"
        assert p.rate_sample is not None and p.rate_sample.status == "both_snapshots"


# -- live: two snapshots around the statement query -------------------------------------


def _collector(cls):
    return cls(ssm=MagicMock(), host="h", port=1, database="db", secret_arn=SECRET)


ENGINES = {
    "mysql": MySQLRemoteCollector,
    "mariadb": MariaDBRemoteCollector,
    "postgresql": PostgreSQLRemoteCollector,
    "sqlserver": SQLServerRemoteCollector,
    "oracle": OracleRemoteCollector,
}


def _statement_row(engine: str, count: int) -> dict:
    text = "SELECT id FROM t WHERE id = 1"
    if engine in ("mysql", "mariadb"):
        return {"digest": "d1", "query_text": text, "execution_count": count}
    if engine == "postgresql":
        return {"queryid": 1, "query": text, "calls": count, "rate_sample_key": "10/1/t"}
    return {"query_id": "d1", "query_text": text, "execution_count": count}


def _group(engine: str) -> str:
    return "10/1/t" if engine == "postgresql" else "d1"


def _live(engine: str, snapshots: list[list[dict]], uptime: float = 86_400.0):
    c = _collector(ENGINES[engine])
    c.get_version = lambda: "10.11.6-MariaDB" if engine == "mariadb" else "8.0.36"  # type: ignore[method-assign]
    c.get_current_schema = lambda: "APP"  # type: ignore[method-assign]
    sent: list[str] = []
    remaining = iter(snapshots)

    def query(sql: str) -> list[dict]:
        lowered = sql.lower()
        if "sampled_at" in lowered:
            sent.append("snapshot")
            return next(remaining)
        if ("uptime" in lowered or "stats_reset" in lowered) and "sampled_at" not in lowered:
            sent.append("server window")
            if engine in ("mysql", "mariadb"):
                return [{"Variable_name": "Uptime", "Value": uptime}]
            if engine == "postgresql":
                return [{"stats_reset_seconds": uptime}]
            return [{"uptime_seconds": uptime}]
        if "dm_exec_sql_text(qs.sql_handle)" in lowered and "objectid" in lowered:
            # SQL Server's read of each statement's routine (#1891), before the sample.
            sent.append("routines")
            return []
        if "program_id" in lowered:
            # Oracle's read of each statement's PL/SQL unit (#1652), after the statements.
            sent.append("routines")
            return []
        sent.append("statements")
        return [_statement_row(engine, 1_300)]

    c._query = query  # type: ignore[method-assign]
    return c, sent


def _snap(engine: str, clock, *entries) -> list[dict]:
    rows = [{"group_key": _group(engine), "entry_key": e, "execution_count": n} for e, n in entries]
    return [{"sampled_at": clock, "entry_count": len(rows)}] + rows


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_collector_rates_each_statement_between_its_two_snapshots(engine):
    snapshots = [
        _snap(engine, "2026-11-05T10:00:00.000000", ("e1", 1_000)),
        _snap(engine, "2026-11-05T10:05:00.000000", ("e1", 1_300), ("e2", 60)),
    ]
    c, sent = _live(engine, snapshots)
    with patch("src.shared.rate_window.time.sleep") as sleep:
        [p] = c.collect_query_patterns(rate_sample_seconds=300)
    sleep.assert_called_once_with(300)
    routines = ["routines"] if engine == "sqlserver" else []
    after = ["routines"] if engine == "oracle" else []
    assert sent == routines + ["snapshot", "statements", "snapshot", "server window"] + after
    assert p["rate_window_basis"] == "sampled"
    assert p["rate_window_seconds"] == 300.0
    assert p["calls_per_second"] == pytest.approx(360 / 300)
    assert p["rate_sample"] == {
        "status": "both_snapshots",
        "first_count": 1_000,
        "second_count": 1_360,
        "elapsed_seconds": 300.0,
    }


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_restart_inside_the_interval_keeps_the_single_snapshot_rate(engine):
    snapshots = [_snap(engine, 1_000.0, ("e1", 1_000)), _snap(engine, 1_300.0, ("e1", 1_300))]
    c, _ = _live(engine, snapshots, uptime=90.0)
    with patch("src.shared.rate_window.time.sleep"):
        [p] = c.collect_query_patterns(rate_sample_seconds=300)
    assert p["rate_sample"]["status"] == "counters_reset"
    assert p["rate_window_basis"] != "sampled"


@pytest.mark.parametrize("engine", list(ENGINES))
def test_a_live_interval_of_0_reads_one_snapshot_as_before(engine):
    c, sent = _live(engine, [])
    with patch("src.shared.rate_window.time.sleep") as sleep:
        [p] = c.collect_query_patterns(rate_sample_seconds=0)
        [default] = c.collect_query_patterns()
    sleep.assert_not_called()
    assert "snapshot" not in sent
    assert "rate_sample" not in p and p == default


@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("bad", [-1, 2.5, "300", True])
def test_a_live_interval_that_is_not_a_whole_number_is_refused(engine, bad):
    c, _ = _live(engine, [])
    with pytest.raises(ValueError, match="sampling interval"):
        c.collect_query_patterns(rate_sample_seconds=bad)


SNAPSHOT_READS = {
    "mysql": ("performance_schema.events_statements_summary_by_digest", "UTC_TIMESTAMP(6)"),
    "postgresql": ("FROM pg_stat_statements s", "clock_timestamp()"),
    "sqlserver": ("FROM sys.dm_exec_query_stats qs", "SYSUTCDATETIME()"),
    "oracle": ("FROM V$SQL", "SYSTIMESTAMP"),
}
WRITES = ("insert ", "update ", "delete ", "truncate", "reset", "set global", "alter ", "create ")


@pytest.mark.parametrize("engine", list(SNAPSHOT_READS))
def test_each_live_snapshot_is_a_read_with_the_servers_clock(engine):
    c = _collector(ENGINES[engine])
    sql = {
        "mysql": lambda: c.rate_sample_snapshot_sql(True),
        "postgresql": c.rate_sample_snapshot_sql,
        "sqlserver": c.rate_sample_snapshot_sql,
        "oracle": lambda: c.rate_sample_snapshot_sql("APP"),
    }[engine]()
    source, clock = SNAPSHOT_READS[engine]
    assert source in sql and clock in sql
    assert sql.strip().upper().startswith(("SELECT", "WITH"))
    # The clock row comes first, with the count that tells a cut snapshot (#1980 review).
    for column in ("entry_count", "entry_age_seconds", "row_kind"):
        assert column in sql
    assert "ORDER BY" in sql
    assert not [w for w in WRITES if w in sql.lower()]
    for column in ("group_key", "entry_key", "execution_count", "sampled_at"):
        assert column in sql


# -- the option ------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "value"), [("0", 0), ("600", 600), (" 30 ", 30)])
def test_the_live_option_is_read_from_rate_sample_seconds(text, value):
    with patch.dict(os.environ, {"RATE_SAMPLE_SECONDS": text}, clear=False):
        assert handler._capture_options()["rate_sample_seconds"] == value


def test_an_unset_live_option_takes_the_default():
    env = {k: v for k, v in os.environ.items() if k != "RATE_SAMPLE_SECONDS"}
    with patch.dict(os.environ, env, clear=True):
        options = handler._capture_options()
    assert "rate_sample_seconds" not in options
    assert CollectionOptions(**options).rate_sample_seconds == 300


@pytest.mark.parametrize("text", ["-1", "five", "2.5"])
def test_a_live_option_that_is_not_a_whole_number_stops_the_run(text):
    with (
        patch.dict(os.environ, {"RATE_SAMPLE_SECONDS": text}, clear=False),
        pytest.raises(ValueError, match="RATE_SAMPLE_SECONDS must be a whole number, 0 or more"),
    ):
        handler._capture_options()
