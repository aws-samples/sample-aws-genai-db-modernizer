# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PostgreSQL rate window after a reset scoped to one database (#2389).

``pg_stat_statements_reset(0, <dbid>, 0)`` empties one database's entries and leaves
``pg_stat_statements_info.stats_reset`` where the last full reset put it. PostgreSQL 16
has no per-entry start, so the collector divided 2.4 hours of counts by a 214-day
window. The collection now reads two more figures: the oldest entry's age (PostgreSQL
17's ``stats_since``), which bounds the window every count can cover, and the calls of
``pg_stat_statements_reset`` with arguments on the server, the one trace a scoped reset
leaves before 17. With the first the window is the shorter of the two; with only the
second the rates keep the window the server records and the check flags them, so no
rate rests on a wrong window silently. Synthetic statements only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.contracts.collector_output import ServerCounters
from src.shared.rate_window import rate_window_facts
from src.shared.server_counters import live_server_counters
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

REPO = Path(__file__).resolve().parents[4]
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 -- test fixture ARN, not a credential

DAY = 86_400.0
OLD_RESET = 214 * DAY  # the last full reset, as pg_stat_statements_info records it
SINCE_SCOPED = 2.4 * 3600  # the real time since the database-scoped reset


def _collection(queries: list[dict], **global_stats: object) -> dict:
    return {
        "metadata": {
            "database_name": "shop",
            "engine": "postgresql",
            "version": "PostgreSQL 16.10",
        },
        "tables": [{"table_name": "cart", "table_rows": 1}],
        "columns": [
            {"table_name": "cart", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": queries,
        "global_stats": {"uptime": str(int(300 * DAY)), **global_stats},
    }


def _q(digest: str, count: int, **extra: object) -> dict:
    return {
        "digest": digest,
        "query_text": f"SELECT * FROM cart WHERE id = {len(digest)}",
        "execution_count": count,
        "total_time_ms": 1.0,
        **extra,
    }


def _facts(parsed: dict) -> dict:
    return rate_window_facts(parsed["queries"], parsed["server_counters"])


# ---------------------------------------------------------------------------
# PostgreSQL 16: the database-scoped reset of the issue
# ---------------------------------------------------------------------------


def test_a_postgres_16_scoped_reset_flags_the_stats_reset_window() -> None:
    """The issue's case: 2.4 hours of counts, a 214-day reset age, and the reset call
    itself among the server's statements. The rates keep the window the server records
    (nothing on 16 says when the scoped reset ran) and the check says they may read
    too low."""
    raw = _collection(
        [_q("d1", 8_640), _q("d22", 864)],
        stats_reset_age_seconds=str(OLD_RESET),
        scoped_reset_calls="1",
    )
    parsed = parse_offline_collection(raw)
    assert {p["rate_window_basis"] for p in parsed["queries"]} == {"stats_reset"}
    assert {p["rate_window_seconds"] for p in parsed["queries"]} == {OLD_RESET}
    assert parsed["server_counters"]["scoped_reset_calls"] == 1
    facts = _facts(parsed)
    assert "scoped_reset" in facts["flags"]
    assert facts["representative"] is False
    check = facts["check_statement"]
    assert "1 call of pg_stat_statements_reset with arguments" in check
    assert "may read too low" in check
    assert "scoped reset" in facts["scenario_label"]


def test_more_than_one_reset_call_is_counted_in_the_reason() -> None:
    raw = _collection(
        [_q("d1", 100)], stats_reset_age_seconds=str(OLD_RESET), scoped_reset_calls="3"
    )
    assert "3 calls of pg_stat_statements_reset" in str(
        _facts(parse_offline_collection(raw))["check_statement"]
    )


def test_a_cluster_wide_reset_is_still_fine() -> None:
    """A full reset moves stats_reset: no reset call with arguments, a long window on a
    long-running server, nothing flagged, and the check says what it looked at."""
    raw = _collection(
        [_q("d1", 864_000)],
        stats_reset_age_seconds=str(10 * DAY),
        scoped_reset_calls="0",
    )
    parsed = parse_offline_collection(raw)
    [p] = parsed["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("stats_reset", 10 * DAY)
    assert p["calls_per_second"] == pytest.approx(1.0)
    facts = _facts(parsed)
    assert facts["flags"] == []
    assert facts["representative"] is True
    assert "pg_stat_statements_reset calls with arguments" in facts["check_statement"]
    assert "a reset scoped" not in facts["check_statement"]


def test_a_collection_without_the_reset_calls_says_the_scope_was_not_checked() -> None:
    """A collection written before #2389 has no reset-call count: nothing is flagged,
    and the check names what it could not look at."""
    raw = _collection([_q("d1", 864_000)], stats_reset_age_seconds=str(10 * DAY))
    facts = _facts(parse_offline_collection(raw))
    assert facts["flags"] == []
    assert (
        "a reset scoped to one database, user or statement (not collected)"
        in facts["check_statement"]
    )


@pytest.mark.passes_on_main  # guards what must not change
def test_no_reset_info_assumes_24_hours_and_raises_no_scope_flag() -> None:
    """PostgreSQL 12 and 13 have no pg_stat_statements_info: the rates assume 24 hours,
    which no reset moves, so a reset call raises no flag."""
    raw = _collection([_q("d1", 1_440)], stats_reset_age_seconds=None, scoped_reset_calls="2")
    parsed = parse_offline_collection(raw)
    [p] = parsed["queries"]
    assert p["rate_window_basis"] == "assumed_24h"
    assert "scoped_reset" not in _facts(parsed)["flags"]


@pytest.mark.passes_on_main  # guards what must not change
def test_sampled_rates_need_no_reset_scope() -> None:
    """Rates over two snapshots rest on no reset time: the reset call flags nothing."""
    raw = _collection(
        [_q("d1", 1_300, sample_first_count=1_000)],
        stats_reset_age_seconds=str(OLD_RESET),
        scoped_reset_calls="1",
    )
    raw["metadata"]["rate_sample"] = {"seconds": 300, "elapsed_seconds": 300.0}
    parsed = parse_offline_collection(raw)
    [p] = parsed["queries"]
    assert p["rate_window_basis"] == "sampled"
    assert p["calls_per_second"] == pytest.approx(1.0)
    assert "scoped_reset" not in _facts(parsed)["flags"]


# ---------------------------------------------------------------------------
# PostgreSQL 17: the oldest entry bounds the window
# ---------------------------------------------------------------------------


def test_a_postgres_17_oldest_entry_bounds_the_server_window() -> None:
    """No entry in the database is older than the scoped reset, so no count covers more
    than the oldest entry's age: a statement too new for its own window falls back on
    that, not on the 214 days."""
    raw = _collection(
        [
            _q("d1", 8_640, stats_since_age_seconds=SINCE_SCOPED),
            _q("d22", 30, stats_since_age_seconds=0.0),
        ],
        stats_reset_age_seconds=str(OLD_RESET),
        oldest_entry_age_seconds=str(SINCE_SCOPED),
        scoped_reset_calls="1",
    )
    parsed = parse_offline_collection(raw)
    by_count = {p["execution_count"]: p for p in parsed["queries"]}
    assert by_count[8_640]["rate_window_basis"] == "observed"
    young = by_count[30]
    assert (young["rate_window_basis"], young["rate_window_seconds"]) == (
        "stats_reset",
        SINCE_SCOPED,
    )
    counters = parsed["server_counters"]
    assert counters["stats_reset_seconds"] == OLD_RESET
    assert counters["oldest_entry_age_seconds"] == SINCE_SCOPED
    # The window is known, so the reset call is no reason to doubt it.
    assert "scoped_reset" not in _facts(parsed)["flags"]


def test_a_postgres_17_oldest_entry_names_the_reset_under_a_day() -> None:
    rows = [
        {
            "query_id": "q1",
            "rate_window_basis": "stats_reset",
            "rate_window_seconds": SINCE_SCOPED,
            "calls_per_second": 1.0,
        }
    ]
    facts = rate_window_facts(
        rows, {"stats_reset_seconds": OLD_RESET, "oldest_entry_age_seconds": SINCE_SCOPED}
    )
    assert "after_reset" in facts["flags"]
    assert "reset 2.4 hours before" in facts["check_statement"]


@pytest.mark.passes_on_main  # guards what must not change
def test_an_oldest_entry_older_than_the_reset_changes_nothing() -> None:
    raw = _collection(
        [_q("d1", 864_000)],
        stats_reset_age_seconds=str(10 * DAY),
        oldest_entry_age_seconds=str(10 * DAY + 5),
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("stats_reset", 10 * DAY)


@pytest.mark.passes_on_main  # guards what must not change
@pytest.mark.parametrize("age", ["0", "-5", "nan", None, "x"])
def test_a_zero_or_unreadable_oldest_entry_age_is_ignored(age) -> None:
    raw = _collection(
        [_q("d1", 864_000)],
        stats_reset_age_seconds=str(10 * DAY),
        oldest_entry_age_seconds=age,
    )
    [p] = parse_offline_collection(raw)["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("stats_reset", 10 * DAY)


@pytest.mark.passes_on_main  # guards what must not change
def test_a_zero_length_reset_window_assumes_24_hours() -> None:
    raw = _collection([_q("d1", 1_440)], stats_reset_age_seconds="0", scoped_reset_calls="1")
    parsed = parse_offline_collection(raw)
    [p] = parsed["queries"]
    assert (p["rate_window_basis"], p["rate_window_seconds"]) == ("assumed_24h", DAY)
    assert "scoped_reset" not in _facts(parsed)["flags"]


def test_an_old_server_counters_record_still_loads() -> None:
    old = {"uptime_seconds": 600.0, "stats_reset_seconds": OLD_RESET}
    loaded = ServerCounters.model_validate(old)
    assert loaded.oldest_entry_age_seconds is None
    assert loaded.scoped_reset_calls is None


# ---------------------------------------------------------------------------
# The live collector and the script read the same figures
# ---------------------------------------------------------------------------


def _live(reset: object, oldest: object, calls: object) -> tuple[PostgreSQLRemoteCollector, list]:
    c = PostgreSQLRemoteCollector(
        ssm=MagicMock(), host="h", port=1, database="db", secret_arn=SECRET
    )
    sent: list[str] = []

    def query(sql: str) -> list[dict]:
        sent.append(sql)
        if "pg_postmaster_start_time" in sql:
            return [{"uptime_seconds": 300 * DAY}]
        if "pg_stat_statements_info" in sql:
            return [{"stats_reset_seconds": reset, "oldest_entry_age_seconds": oldest}]
        if "pg_stat_statements_reset" in sql:
            return [{"scoped_reset_calls": calls}]
        return [{"queryid": 1, "query": "SELECT 1 FROM t", "calls": 30}]

    c._query = query  # type: ignore[method-assign]
    return c, sent


def test_the_live_collector_bounds_the_window_by_the_oldest_entry() -> None:
    c, _ = _live(OLD_RESET, SINCE_SCOPED, 1)
    window = c.stats_window()
    assert window is not None
    assert (window.seconds, window.basis) == (SINCE_SCOPED, "stats_reset")
    [p] = c.collect_query_patterns()
    assert p["rate_window_seconds"] == SINCE_SCOPED


def test_the_live_collector_records_the_reset_figures() -> None:
    c, _ = _live(OLD_RESET, None, 2)
    counters = live_server_counters(c)
    assert counters is not None
    assert counters["stats_reset_seconds"] == OLD_RESET
    assert counters.get("oldest_entry_age_seconds") is None
    assert counters["scoped_reset_calls"] == 2
    assert ServerCounters.model_validate(counters).scoped_reset_calls == 2


def test_a_live_reset_call_read_that_fails_is_left_out() -> None:
    c, _ = _live(OLD_RESET, None, 2)
    original = c._query

    def query(sql: str) -> list[dict]:
        if "pg_stat_statements_reset" in sql:
            raise RuntimeError("permission denied")
        return original(sql)

    c._query = query  # type: ignore[method-assign]
    # The other figures are still read.
    assert c.reset_scope() == {
        "stats_reset_seconds": OLD_RESET,
        "oldest_entry_age_seconds": None,
        "scoped_reset_calls": None,
    }
    counters = live_server_counters(c)
    assert counters is not None
    assert counters.get("scoped_reset_calls") is None
    assert counters["stats_reset_seconds"] == OLD_RESET


def test_the_live_reads_name_no_version_specific_column() -> None:
    """``stats_since`` is read through ``to_jsonb`` (NULL before 17), and the reset
    calls are counted over every database (a reset may run from another one)."""
    c, sent = _live(OLD_RESET, None, 0)
    live_server_counters(c)
    window_sqls = {s for s in sent if "pg_stat_statements_info" in s}
    assert window_sqls
    assert all("to_jsonb(p) ->> 'stats_since'" in s for s in window_sqls)
    [calls_sql] = [s for s in sent if "pg_stat_statements_reset" in s]
    assert "dbid" not in calls_sql


def test_the_collection_script_writes_both_figures() -> None:
    pg = (REPO / "scripts" / "collect-postgresql.sql").read_text()
    assert "'oldest_entry_age_seconds', (" in pg
    assert "'scoped_reset_calls', (" in pg
    # The stats_since read stays version-agnostic, and the pattern needs no backslash
    # (standard_conforming_strings may be off).
    assert "min((to_jsonb(p) ->> 'stats_since')::timestamptz)" in pg
    assert "pg_stat_statements_reset[[:space:]]*[(][[:space:]]*[^)[:space:]]" in pg
