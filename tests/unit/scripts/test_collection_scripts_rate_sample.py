# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The offline collection scripts measure call rates over the load period (#1908).

Each script reads the statement counters, waits ``rate_sample_seconds`` (300 by
default, set the way its client sets variables), and reads them again: psql
``pg_sleep``, MySQL ``DO SLEEP``, SQL Server ``WAITFOR DELAY`` and Oracle
``DBMS_SESSION.SLEEP`` (else ``DBMS_LOCK.SLEEP``), all read-only. Each statement row
carries its count at the first read (``sample_first_count``) and the metadata the wait
and the seconds the server's clock measured (``metadata.rate_sample``).

The PostgreSQL script runs whole against a PostgreSQL started by ``pgserver`` on a Unix
socket in a temp folder (no TCP listener). Its bundle has no ``pg_stat_statements``, so
the test gives the script tables of that name and a ``pg_sleep`` of its own, found
first on the session's ``search_path``, that runs statements "during the wait". The
other scripts are read as text. The pgserver tests skip when it cannot start.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from src.shared import rate_window
from src.tools.database.offline_parser import parse_offline_collection

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / f"collect-{name}.sql").read_text()


def _code(text: str) -> str:
    """The script without its ``--`` comment lines (the header documents the option)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))


# -- every script: the option, the wait, the records -----------------------------------

WAITS = {
    "postgresql": "SELECT pg_sleep(CAST(:'rate_sample_seconds' AS integer))",
    "mysql": "DO SLEEP(@rate_sample_seconds);",
    "sqlserver": "WAITFOR DELAY @rate_sample_delay;",
    "oracle": "EXECUTE IMMEDIATE 'BEGIN DBMS_SESSION.SLEEP(:s); END;' USING v_sample_seconds;",
}
DEFAULTS = {
    "postgresql": "\\set rate_sample_seconds 300",
    "mysql": "CAST(@rate_sample_seconds AS UNSIGNED), 300);",
    "sqlserver": "DECLARE @rate_sample_seconds INT = 300;",
    "oracle": ":rate_sample_seconds := NVL(TO_NUMBER('&3'), 300);",
}
USAGE = {
    "postgresql": "-v rate_sample_seconds=300",
    "mysql": '--init-command="SET @rate_sample_seconds=300"',
    "sqlserver": "@rate_sample_seconds DECLARE line",
    "oracle": "@collect-oracle.sql none 10 300",
}
METADATA = {
    "postgresql": "'rate_sample', json_build_object(",
    "mysql": "'$.rate_sample', JSON_OBJECT('seconds', @rate_sample_seconds, 'elapsed_seconds'",
    "sqlserver": ")) AS rate_sample",
    "oracle": "'rate_sample' VALUE JSON_OBJECT(",
}
ROW = {
    "postgresql": "r.c AS sample_first_count",
    "mysql": "'sample_key', CONCAT(DIGEST, '/',",
    "sqlserver": "g.sample_first_count",
    "oracle": "',\"sample_first_count\":' || TO_CHAR(v_shape_first(rec.sample_shape))",
}


@pytest.mark.parametrize("engine", list(WAITS))
def test_each_script_waits_300_seconds_by_default_between_two_reads(engine):
    text = _script(engine)
    code = _code(text)
    assert DEFAULTS[engine] in code
    assert WAITS[engine] in code
    assert USAGE[engine] in text  # the header documents the option and its default
    assert "default: 300" in text
    assert METADATA[engine] in code
    assert ROW[engine] in code


# Main's scripts were read-only too: only SQL Server's (its table variable) is new here.
_MAIN_TOO = pytest.mark.passes_on_main


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("postgresql", marks=_MAIN_TOO),
        pytest.param("mysql", marks=_MAIN_TOO),
        "sqlserver",
        pytest.param("oracle", marks=_MAIN_TOO),
    ],
)
def test_each_script_stays_read_only(engine):
    """No setting changes and no resets: the first read is kept in the client's
    variables or the session's own table variable (SQL Server)."""
    code = _code(_script(engine)).lower()
    for forbidden in (
        r"pg_stat_statements_reset\s*\(",
        r"\btruncate\s+(table\s+)?[a-z_.]+\s*;",
        r"\bset\s+(global|persist)\b",
        r"\balter\s+(system|session|database)\b",
        r"\bdbcc\s",
        r"\bflush\s+(tables|status|privileges|shared_pool|buffer_cache)\b",
        r"\bsp_configure\b",
        r"\bcreate\s+(table|index|procedure|function)\b",
    ):
        assert not re.search(forbidden, code), forbidden
    if engine == "sqlserver":
        # The only INSERT fills this session's table variable.
        inserts = re.findall(r"insert into (\S+)", code)
        assert inserts == ["@rate_sample_start"]
    else:
        assert "insert " not in code and "update " not in code and "delete " not in code


def test_mysql_keys_each_digest_by_its_first_seen():
    """A digest emptied and counted again in between is a new entry, not a lower count."""
    code = _code(_script("mysql"))
    key = "CONCAT(DIGEST, '/', IFNULL(DATE_FORMAT(FIRST_SEEN, '%Y-%m-%d %H:%i:%s.%f'), ''))"
    assert code.count(key) == 2  # the first read's key and each row's sample_key
    # The first counts travel in the metadata (JSON_OBJECTAGG, MariaDB 10.5+): no
    # JSON_TABLE, which MariaDB has only from 10.6.
    assert "'first_counts', IFNULL(JSON_EXTRACT(@rate_sample_start, '$'), JSON_OBJECT())" in code
    assert "JSON_TABLE" not in code
    # An unset or unreadable value takes the default; the metadata records the value used.
    assert "REGEXP '^[0-9]+$'" in code


def test_sql_server_keys_each_cache_entry_by_its_plan_and_creation_time():
    code = _code(_script("sqlserver"))
    join = code[code.index("LEFT JOIN @rate_sample_start rs") :]
    for column in ("plan_handle", "sql_handle", "statement_start_offset", "creation_time"):
        assert f"rs.{column} = qs.{column}" in join
    assert "IF @rate_sample_seconds IS NULL OR @rate_sample_seconds < 0" in code
    assert "@rate_sample_seconds > 86399" in code  # WAITFOR DELAY waits under a day
    assert "DATEDIFF_BIG(MICROSECOND, @rate_sample_t0, @rate_sample_t1)" in code


def test_oracle_falls_back_to_dbms_lock_and_then_to_one_read():
    code = _code(_script("oracle"))
    sample = code[code.index("IF v_sample_seconds > 0 THEN") :]
    sample = sample[: sample.index('"queries": [')]
    assert "DBMS_SESSION.SLEEP" in sample and "DBMS_LOCK.SLEEP" in sample
    assert sample.index("DBMS_SESSION.SLEEP") < sample.index("DBMS_LOCK.SLEEP")
    # Neither ran: the first read is dropped, and no elapsed time is recorded.
    assert "v_first_counts.DELETE;" in sample and "v_sample_t0 := NULL;" in sample
    assert "SQL_ID || '/' || CHILD_NUMBER || '/' || RAWTOHEX(CHILD_ADDRESS)" in code
    assert "COLUMN 3 NEW_VALUE 3 NOPRINT" in code


def test_oracle_drops_to_one_read_on_any_sampling_error():
    """The review on #1980: an error in the first read or in pairing the reads must not
    end the collection. Each is in a block of its own that drops the sample; the reads
    are kept in PL/SQL tables, filled once, so no JSON is parsed per cursor."""
    code = _code(_script("oracle"))
    first = code[code.index("IF v_sample_seconds > 0 THEN") :]
    first = first[: first.index("IF v_sample_t0 IS NOT NULL THEN")]
    assert "BEGIN" in first and "EXCEPTION WHEN OTHERS THEN" in first
    assert "v_first_counts(r.k) := r.c;" in first
    pair = code[code.index("v_sample_span := SYSTIMESTAMP - v_sample_t0;") :]
    pair = pair[: pair.index('"queries": [')]
    assert "EXCEPTION WHEN OTHERS THEN" in pair and "v_sample_elapsed := NULL;" in pair
    assert "TYPE t_counts IS TABLE OF NUMBER INDEX BY VARCHAR2(400);" in code
    assert "JSON_TABLE" not in code and "JSON_ARRAYAGG" not in code


def test_the_postgresql_first_read_failing_gives_one_read():
    code = _code(_script("postgresql"))
    guarded = code[code.index("\\if :{?rate_sample_t0}") :]
    assert guarded.index("pg_sleep") < guarded.index("\\else")
    assert "\\set rate_sample_elapsed 'NULL::numeric'" in guarded


def test_mysql_measures_the_wait_on_the_utc_clock():
    """UTC_TIMESTAMP(6), so a daylight-saving change in the session's zone during the
    wait does not move the measured time by an hour."""
    code = _code(_script("mysql"))
    assert "SET @rate_sample_t0 = IF(@rate_sample_seconds > 0, UTC_TIMESTAMP(6), NULL);" in code
    assert "TIMESTAMPDIFF(MICROSECOND, @rate_sample_t0, UTC_TIMESTAMP(6))" in code
    assert "UNIX_TIMESTAMP(NOW(6))" not in code


def test_the_sql_server_first_read_keeps_only_this_databases_keys_and_counts():
    code = _code(_script("sqlserver"))
    insert = code[code.index("INSERT INTO @rate_sample_start") :]
    insert = insert[: insert.index(";")]
    assert "ISNULL(NULLIF(st.dbid, 0), DB_ID()) = DB_ID()" in insert
    assert "text" not in insert.replace("dm_exec_sql_text", "")


# -- PostgreSQL, run whole ---------------------------------------------------------------

pgserver = pytest.importorskip("pgserver")

_FAKE = r"""
\set ON_ERROR_STOP on
CREATE TABLE public.pg_stat_statements (
  userid oid, dbid oid, queryid bigint, toplevel bool, query text, calls bigint,
  total_exec_time float8, min_exec_time float8, max_exec_time float8, rows bigint,
  shared_blks_hit bigint, shared_blks_read bigint, blk_read_time float8,
  blk_write_time float8, temp_blks_read bigint, temp_blks_written bigint
);
CREATE TABLE public.pg_stat_statements_info (stats_reset timestamptz);
"""
# The statements "run during the wait": q1 ran 900 more times, q2 not at all, q3 was reset
# (its count went down) and q4 appeared.
_WAIT = r"""
CREATE OR REPLACE FUNCTION public.pg_sleep(double precision) RETURNS void
LANGUAGE sql AS $$
  UPDATE public.pg_stat_statements SET calls = calls + 900 WHERE queryid = 1;
  UPDATE public.pg_stat_statements SET calls = 15 WHERE queryid = 3;
  INSERT INTO public.pg_stat_statements
    SELECT userid, dbid, 4, toplevel, 'SELECT 4', 60, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0
    FROM public.pg_stat_statements WHERE queryid = 1;
$$;
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql(_FAKE)
    yield srv
    srv.cleanup()


def _load(server, stats_reset_days: float) -> None:
    server.psql(
        "\\set ON_ERROR_STOP on\n"
        "TRUNCATE public.pg_stat_statements, public.pg_stat_statements_info;\n"
        f"INSERT INTO public.pg_stat_statements_info VALUES "
        f"(now() - interval '{stats_reset_days * 86400} seconds');\n"
        "INSERT INTO public.pg_stat_statements SELECT 10, d.oid, q, true, 'SELECT ' || q, c,"
        " 1, 0, 1, 1, 0, 0, 0, 0, 0, 0 FROM pg_database d,"
        " (VALUES (1, 100), (2, 50), (3, 20)) v(q, c) WHERE d.datname = current_database();\n"
        + _WAIT
    )


def _collect(server, setting: str | None) -> dict:
    head = "SET search_path = public, pg_catalog;\n\\t on\n\\a\n"
    if setting is not None:
        head += f"\\set rate_sample_seconds {setting}\n"
    out = server.psql(head + _script("postgresql"))
    start = out.find('{"collection_version"')
    assert start >= 0, out[-2000:]
    data: dict = json.loads(out[start:])
    return data


def _patterns(data: dict) -> dict[str, dict]:
    """The parsed statements. This test's pg_sleep returns at once, so the interval's
    floor (10 seconds, half the one asked, tested in tests/unit/shared) is lifted here."""
    with (
        patch.object(rate_window, "MIN_SAMPLE_SECONDS", 0.0),
        patch.object(rate_window, "MIN_SAMPLE_SHARE", 0.0),
    ):
        parsed = parse_offline_collection(data)
    return {p["query_id"]: p for p in parsed["queries"]}


def test_the_postgresql_scripts_instant_wait_is_too_short_to_rate(server):
    """Without the lifted floor the same run (a wait cut short, as a statement_timeout
    cancelling pg_sleep would) keeps every single-read rate."""
    _load(server, stats_reset_days=3)
    parsed = parse_offline_collection(_collect(server, "2"))
    assert {p["rate_sample"]["status"] for p in parsed["queries"]} == {"interval_too_short"}


def test_the_postgresql_script_rates_each_statement_over_its_two_reads(server):
    _load(server, stats_reset_days=3)
    data = _collect(server, "2")
    sample = data["metadata"]["rate_sample"]
    assert sample["seconds"] == 2
    assert sample["elapsed_seconds"] is not None and sample["elapsed_seconds"] > 0
    rows = {q["digest"]: q for q in data["queries"]}
    assert {d: (r["execution_count"], r["sample_first_count"]) for d, r in rows.items()} == {
        "1": (1_000, 100),
        "2": (50, 50),
        "3": (15, 20),
        "4": (60, None),
    }
    got = _patterns(data)
    elapsed = sample["elapsed_seconds"]
    assert got["1"]["rate_window_basis"] == "sampled"
    assert got["1"]["calls_per_second"] == pytest.approx(900 / elapsed)
    assert got["4"]["rate_sample"]["status"] == "second_snapshot_only"
    assert got["2"]["rate_sample"]["status"] == "no_calls_in_interval"
    assert got["3"]["rate_sample"]["status"] == "count_decreased"
    assert got["2"]["rate_window_basis"] == got["3"]["rate_window_basis"] == "stats_reset"


def test_the_postgresql_script_takes_300_seconds_by_default(server):
    """Unset, the wait asks for 300 seconds (this pg_sleep returns at once)."""
    _load(server, stats_reset_days=3)
    data = _collect(server, None)
    assert data["metadata"]["rate_sample"]["seconds"] == 300


def test_a_postgresql_reset_inside_the_wait_keeps_the_single_read_rates(server):
    """pg_stat_statements was reset after the first read, and the wait went on (the real
    pg_sleep, briefly): its reset age is shorter than the interval."""
    _load(server, stats_reset_days=3)
    server.psql(
        _WAIT.replace(
            "$$;",
            "  UPDATE public.pg_stat_statements_info SET stats_reset = clock_timestamp();\n"
            "  SELECT pg_catalog.pg_sleep(0.3);\n$$;",
        )
    )
    got = _patterns(_collect(server, "2"))
    assert {p["rate_sample"]["status"] for p in got.values()} == {"counters_reset"}
    assert not [p for p in got.values() if p["rate_window_basis"] == "sampled"]


def test_the_postgresql_script_at_0_reads_once_as_before(server):
    _load(server, stats_reset_days=3)
    data = _collect(server, "0")
    assert data["metadata"]["rate_sample"] == {"seconds": 0, "elapsed_seconds": None}
    rows = {q["digest"]: q for q in data["queries"]}
    # One read: nothing ran "during the wait", and no row has a first count.
    assert {d: r["execution_count"] for d, r in rows.items()} == {"1": 100, "2": 50, "3": 20}
    assert all(r["sample_first_count"] is None for r in rows.values())
    assert all("rate_sample" not in p for p in _patterns(data).values())


@pytest.mark.parametrize("bad", ["-5", "five", "2.5"])
def test_a_postgresql_interval_that_is_not_a_whole_number_stops_the_script(server, bad):
    _load(server, stats_reset_days=3)
    head = f"SET search_path = public, pg_catalog;\n\\t on\n\\a\n\\set rate_sample_seconds {bad}\n"
    out = server.psql(head + _script("postgresql"))
    assert '{"collection_version"' not in out
