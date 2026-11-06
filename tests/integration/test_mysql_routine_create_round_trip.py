# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A MySQL CREATE rebuilt from information_schema means what the original means (#1652).

Set MYSQL_ROUTINE_TEST_DSN ("host;port;user;password"; a host starting with "/" is a
socket path) to a disposable MySQL 8.0.22+ server whose user may create and drop
schemas, routines, triggers and events, and run
``uv run pytest -m integration tests/integration/test_mysql_routine_create_round_trip.py``.
A local server with no network listener works: conda-forge ``mysql-server``,
``mysqld --initialize-insecure --datadir=<dir>`` then ``mysqld --datadir=<dir>
--skip-networking --socket=<dir>/mysql.sock``, and the DSN ``<dir>/mysql.sock;0;root;``.

For every routine, trigger and event of a corpus that uses each clause the rebuild
writes, the test (1) creates it in ``rt_src_<id>``; (2) reads it with the live
collector's own queries and rebuilds its CREATE; (3) checks that SHOW CREATE's text
ends with the body information_schema returned, byte for byte; (4) loads each rebuilt
CREATE into ``rt_dst_<id>`` under the session the sidecar records; (5) requires every
information_schema column of the two to match, less the schema name and timestamps;
(6) rebuilds from ``rt_dst_<id>`` and requires the same text (a fixed point). The
catalog rows are everything SHOW CREATE and the server read the object from, so equal
rows mean equal meaning.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 -- runs the mysql client with fixed argv
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from src.agents.collector import mysql_collector
from src.agents.collector.mysql_routine_create import quote_ident
from src.agents.collector.routine_sources import (
    build_routine_sources,
    routine_sources_from_collection,
)
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.collection_text import extract_json_object
from src.tools.database.mysql_tools import MySQLRemoteCollector

mysql_connector = pytest.importorskip("mysql.connector")

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-mysql.sql"

_DROPPED = {
    "ROUTINES": {"ROUTINE_SCHEMA", "CREATED", "LAST_ALTERED"},
    "PARAMETERS": {"SPECIFIC_SCHEMA"},
    "TRIGGERS": {"TRIGGER_SCHEMA", "EVENT_OBJECT_SCHEMA", "CREATED"},
    "EVENTS": {"EVENT_SCHEMA", "CREATED", "LAST_ALTERED", "LAST_EXECUTED", "ORIGINATOR"},
}
_SCHEMA_COLUMN = {
    "ROUTINES": "ROUTINE_SCHEMA",
    "PARAMETERS": "SPECIFIC_SCHEMA",
    "TRIGGERS": "TRIGGER_SCHEMA",
    "EVENTS": "EVENT_SCHEMA",
}
_ORDER = {
    "ROUTINES": "ROUTINE_TYPE, ROUTINE_NAME",
    "PARAMETERS": "ROUTINE_TYPE, SPECIFIC_NAME, ORDINAL_POSITION",
    "TRIGGERS": "EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER",
    "EVENTS": "EVENT_NAME",
}

# (sql_mode, statement): each runs under its own sql_mode, as a routine keeps the one
# it was created with.
CORPUS: list[tuple[str, str]] = [
    ("STRICT_TRANS_TABLES", "CREATE TABLE orders (id INT PRIMARY KEY, status VARCHAR(20), amount DECIMAL(10,2))"),
    ("STRICT_TRANS_TABLES", r"""CREATE PROCEDURE place_order(IN p_id INT, OUT p_total DECIMAL(10,2), INOUT p_note VARCHAR(40) CHARSET latin1)
    MODIFIES SQL DATA
    COMMENT 'it''s a \\ test'
BEGIN
  INSERT INTO orders (id, status) VALUES (p_id, 'placed');
  SELECT SUM(amount) INTO p_total FROM orders WHERE id = p_id;
  SET p_note = CONCAT(p_note, ' done');
END"""),
    ("STRICT_TRANS_TABLES", "CREATE FUNCTION label(p_code INT) RETURNS VARCHAR(20) CHARSET utf8mb4 DETERMINISTIC NO SQL SQL SECURITY INVOKER RETURN CASE p_code WHEN 1 THEN _utf8mb4'café' ELSE 'ñandú' END"),
    ("NO_BACKSLASH_ESCAPES,STRICT_TRANS_TABLES", r"CREATE FUNCTION path_sep() RETURNS CHAR(1) DETERMINISTIC NO SQL COMMENT 'C:\dir' RETURN '\'"),
    ("ANSI_QUOTES", 'CREATE PROCEDURE read_status() READS SQL DATA SELECT "status" FROM orders'),
    ("STRICT_TRANS_TABLES", "CREATE PROCEDURE dup() SELECT 1"),
    ("STRICT_TRANS_TABLES", "CREATE FUNCTION dup() RETURNS INT DETERMINISTIC RETURN 1"),
    ("STRICT_TRANS_TABLES", "CREATE PROCEDURE `odd``name`() SELECT 2"),
    ("STRICT_TRANS_TABLES", "CREATE TRIGGER tg_a BEFORE INSERT ON orders FOR EACH ROW SET NEW.status = COALESCE(NEW.status, 'new')"),
    ("STRICT_TRANS_TABLES", "CREATE TRIGGER tg_b BEFORE INSERT ON orders FOR EACH ROW FOLLOWS tg_a SET NEW.amount = COALESCE(NEW.amount, 0)"),
    ("STRICT_TRANS_TABLES", "CREATE TRIGGER tg_c BEFORE INSERT ON orders FOR EACH ROW PRECEDES tg_a SET NEW.id = NEW.id"),
    ("STRICT_TRANS_TABLES", "CREATE EVENT purge_daily ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00' ENDS '2031-01-01 00:00:00' ON COMPLETION PRESERVE DISABLE COMMENT 'nightly' DO DELETE FROM orders WHERE status = 'void'"),
    ("STRICT_TRANS_TABLES", "CREATE EVENT tick ON SCHEDULE EVERY '1:30' HOUR_MINUTE STARTS '2030-01-01 00:00:00' DO UPDATE orders SET amount = amount"),
    ("STRICT_TRANS_TABLES", "CREATE EVENT once_job ON SCHEDULE AT '2030-06-01 12:00:00' ON COMPLETION PRESERVE DO DELETE FROM orders WHERE id < 0"),
]  # fmt: skip
ROUTINES, TRIGGERS, EVENTS = 7, 3, 3
_INTRODUCER = re.compile(r"_[a-z0-9]+'")


class LocalSqlExecutor:
    batch_escaped = False  # the connector returns each value as the server holds it

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def run_sql_json(self, **kwargs: Any) -> list[dict]:
        cursor = self.connection.cursor(dictionary=True)
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            kwargs["sql"]
        )
        rows = [dict(r) for r in cursor.fetchall()]
        cursor.close()
        return rows


def _dsn(variable: str) -> tuple[str, int, str, str]:
    dsn = os.environ.get(variable)
    if not dsn:
        pytest.skip(f"Set {variable} to a disposable server")
    host, port, user, password = dsn.split(";", 3)
    return host, int(port or 0), user, password


def _connect(variable: str, database: str | None = None) -> Any:
    host, port, user, password = _dsn(variable)
    kwargs: dict[str, Any] = {
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "autocommit": True,
    }
    if host.startswith("/"):
        kwargs["unix_socket"] = host
    else:
        kwargs.update(host=host, port=port)
    if database:
        kwargs["database"] = database
    return mysql_connector.connect(**kwargs)


def _run(conn: Any, sql_mode: str, statement: str, time_zone: str = "+00:00") -> None:
    cursor = conn.cursor()
    cursor.execute("SET SESSION sql_mode = %s, time_zone = %s", (sql_mode, time_zone))
    cursor.execute(statement)  # nosemgrep: sqlalchemy-execute-raw-query -- test corpus
    cursor.close()


def _collect(conn: Any) -> dict[str, list[dict]]:
    db = MySQLRemoteCollector(cast(SSMExecutor, LocalSqlExecutor(conn)), "localhost", 3306, "", "")
    return {
        "procedures": db.collect_procedures(),
        "triggers": db.collect_triggers(),
        "events": db.collect_events(),
        "parameters": db.collect_routine_parameters(),
    }


def _catalog(conn: Any, schema: str) -> dict[str, list[dict]]:
    cursor = conn.cursor(dictionary=True)
    out: dict[str, list[dict]] = {}
    for table, column in _SCHEMA_COLUMN.items():
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"SELECT * FROM information_schema.{table} WHERE {column} = %s ORDER BY {_ORDER[table]}",  # nosec B608
            (schema,),
        )
        out[table] = [
            {k: v for k, v in r.items() if k not in _DROPPED[table]} for r in cursor.fetchall()
        ]
    cursor.close()
    return out


@pytest.fixture
def schemas() -> Iterator[tuple[str, str]]:
    admin = _connect("MYSQL_ROUTINE_TEST_DSN")
    sid = uuid4().hex[:10]
    src, dst = f"rt_src_{sid}", f"rt_dst_{sid}"
    cursor = admin.cursor()
    try:
        for name in (src, dst):
            cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"
            )
        yield src, dst
    finally:
        for name in (src, dst):
            cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
                f"DROP DATABASE IF EXISTS `{name}`"
            )
        admin.close()


def test_every_rebuilt_create_loads_to_the_same_catalog_rows(schemas):
    src, dst = schemas
    source = _connect("MYSQL_ROUTINE_TEST_DSN", src)
    for sql_mode, statement in CORPUS:
        _run(source, sql_mode, statement)
    rows = _collect(source)
    build = build_routine_sources("mysql", **rows)
    assert build.statuses == {}
    assert len(build.sources) == ROUTINES + TRIGGERS + EVENTS
    assert {s.session.character_set_client for s in build.sources} == {"utf8mb4"}

    # (3) information_schema's body is SHOW CREATE's body, byte for byte, but for the
    # character set introducers (_utf8mb4'...') ROUTINE_DEFINITION leaves out: the
    # rebuilt statement is replayed in character_set_client, so a literal reads the same
    # (steps 5 and 6 prove it on the catalog).
    cursor = source.cursor(dictionary=True)
    for p in rows["procedures"]:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"SHOW CREATE {p['routine_type']} {quote_ident(p['routine_name'])}"
        )
        shown = cursor.fetchone()[f"Create {p['routine_type'].title()}"]
        assert _INTRODUCER.sub("'", shown).endswith(p["definition"]), p["routine_name"]
    for t in rows["triggers"]:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"SHOW CREATE TRIGGER {quote_ident(t['trigger_name'])}"
        )
        assert cursor.fetchone()["SQL Original Statement"].endswith(t["definition"]), t[
            "trigger_name"
        ]
    for e in rows["events"]:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"SHOW CREATE EVENT {quote_ident(e['event_name'])}"
        )
        assert cursor.fetchone()["Create Event"].endswith(e["definition"]), e["event_name"]
    cursor.close()

    # (4) load the rebuilt statements in order: routines, triggers by action order
    # (FOLLOWS needs its predecessor), events.
    target = _connect("MYSQL_ROUTINE_TEST_DSN", dst)
    _run(target, "STRICT_TRANS_TABLES", CORPUS[0][1])
    rank = {"function": 0, "procedure": 0, "trigger": 1, "event": 2}
    order = {
        f"{t['table_name']}.{t['trigger_name']}": int(t["action_order"]) for t in rows["triggers"]
    }
    for s in sorted(
        build.sources, key=lambda s: (rank[s.kind], order.get(s.specific_name, 0), s.specific_name)
    ):
        _run(target, s.session.sql_mode or "", s.create_statement, s.session.time_zone or "+00:00")

    # (5) the same catalog rows; (6) the same text rebuilt from the copy.
    assert _catalog(target, dst) == _catalog(source, src)
    again = build_routine_sources("mysql", **_collect(target))
    assert sorted((s.kind, s.specific_name, s.create_statement) for s in again.sources) == sorted(
        (s.kind, s.specific_name, s.create_statement) for s in build.sources
    )


def _script_collection(
    variable: str, database: str, client: str = "mysql", settings: str = ""
) -> dict:
    host, port, user, password = _dsn(variable)
    where = ["--socket", host] if host.startswith("/") else ["-h", host, "-P", str(port)]
    out = subprocess.run(  # nosec B603
        [client, "-N", "--max-allowed-packet=1G", "--raw",
         f"--init-command=SET @rate_sample_seconds=0{settings}", "-u", user, *where,
         "-D", database],
        input=SCRIPT.read_text(), capture_output=True, text=True, check=True, timeout=300,
        env={**os.environ, "MYSQL_PWD": password},
    ).stdout  # fmt: skip
    collection: dict = json.loads(extract_json_object(out, source="mysql"))
    return collection


@pytest.mark.skipif(not shutil.which("mysql"), reason="needs the mysql client")
def test_the_script_reads_what_the_live_collector_reads(schemas):
    src, _ = schemas
    source = _connect("MYSQL_ROUTINE_TEST_DSN", src)
    for sql_mode, statement in CORPUS:
        _run(source, sql_mode, statement)
    script = routine_sources_from_collection(_script_collection("MYSQL_ROUTINE_TEST_DSN", src))
    live = build_routine_sources("mysql", **_collect(source))
    assert sorted((s.kind, s.specific_name, s.create_statement) for s in script.sources) == sorted(
        (s.kind, s.specific_name, s.create_statement) for s in live.sources
    )


@pytest.mark.skipif(
    not shutil.which(os.environ.get("MARIADB_ROUTINE_TEST_CLIENT", "mysql")),
    reason="needs the mysql or mariadb client",
)
def test_the_mysql_script_still_runs_on_mariadb():
    admin = _connect("MARIADB_ROUTINE_TEST_DSN")
    name = f"rt_maria_{uuid4().hex[:10]}"
    cursor = admin.cursor()
    try:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"CREATE DATABASE `{name}`"
        )
        conn = _connect("MARIADB_ROUTINE_TEST_DSN", name)
        _run(conn, "STRICT_TRANS_TABLES", CORPUS[0][1])
        _run(conn, "STRICT_TRANS_TABLES", "CREATE PROCEDURE dup() SELECT 1")
        # MARIADB_ROUTINE_TEST_CLIENT: the client to run it with (default mysql)
        client = os.environ.get("MARIADB_ROUTINE_TEST_CLIENT", "mysql")
        raw = _script_collection("MARIADB_ROUTINE_TEST_DSN", name, client)
        assert [p["body_status"] for p in raw["procedures"]] == ["collected"]
        assert raw["metadata"]["routine_capture"]["include_bodies"] is True
        # MariaDB moves to Aurora MySQL: its routines are carried, rebuilt as MySQL's
        (dup,) = routine_sources_from_collection(raw).sources
        assert (dup.kind, dup.specific_name, dup.exact) == ("procedure", "dup", True)
        off = _script_collection(
            "MARIADB_ROUTINE_TEST_DSN", name, client, ", @include_routine_bodies=0"
        )
        assert off["metadata"]["routine_capture"]["include_bodies"] is False
        assert [(p["body_status"], p["definition"]) for p in off["procedures"]] == [
            ("opted_out", None)
        ]
    finally:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"DROP DATABASE IF EXISTS `{name}`"
        )
        admin.close()


# The routines whose catalog body lost a character-set introducer (review of #2009):
# rebuilt, each returns something else; read with SHOW CREATE, each returns the same.
_INTRODUCERS = [
    "CREATE TABLE users (u VARCHAR(10) COLLATE utf8mb4_0900_ai_ci)",
    "INSERT INTO users VALUES ('admin')",
    "CREATE FUNCTION bin_eq() RETURNS INT DETERMINISTIC RETURN _binary'Admin' = 'admin'",
    "CREATE FUNCTION bin_where() RETURNS INT READS SQL DATA "
    "RETURN (SELECT COUNT(*) FROM users WHERE u = _binary'Admin')",
    "CREATE FUNCTION utf16_hex() RETURNS VARCHAR(20) DETERMINISTIC RETURN HEX(_utf16'a')",
]
_CALLS = ["bin_eq", "bin_where", "utf16_hex"]


def _results(conn: Any) -> dict[str, Any]:
    cursor = conn.cursor()
    out = {}
    for name in _CALLS:
        cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query, formatted-sql-query -- disposable test server
            f"SELECT `{name}`()"
        )
        out[name] = cursor.fetchone()[0]
    cursor.close()
    return out


def _load(conn: Any, sources: list) -> None:
    for statement in _INTRODUCERS[:2]:
        _run(conn, "STRICT_TRANS_TABLES", statement)
    for s in sources:
        _run(conn, s.session.sql_mode or "", s.create_statement)


def test_show_create_keeps_what_the_rebuild_loses(schemas):
    src, dst = schemas
    source = _connect("MYSQL_ROUTINE_TEST_DSN", src)
    for statement in _INTRODUCERS:
        _run(source, "STRICT_TRANS_TABLES", statement)
    expected = _results(source)
    db = MySQLRemoteCollector(
        cast(SSMExecutor, LocalSqlExecutor(source)), "localhost", 3306, "", ""
    )
    live = build_routine_sources("mysql", **mysql_collector._collect_routine_creates_raw(db, True))
    assert sorted(s.specific_name for s in live.sources) == sorted(_CALLS)
    assert {(s.origin, s.exact) for s in live.sources} == {("show_create", True)}
    target = _connect("MYSQL_ROUTINE_TEST_DSN", dst)
    _load(target, live.sources)
    assert _results(target) == expected


def test_a_rebuild_that_drops_an_introducer_is_marked_not_exact(schemas):
    src, dst = schemas
    source = _connect("MYSQL_ROUTINE_TEST_DSN", src)
    for statement in _INTRODUCERS:
        _run(source, "STRICT_TRANS_TABLES", statement)
    expected = _results(source)
    rebuilt = build_routine_sources("mysql", **_collect(source))
    assert {(s.origin, s.exact) for s in rebuilt.sources} == {("information_schema", False)}
    target = _connect("MYSQL_ROUTINE_TEST_DSN", dst)
    _load(target, rebuilt.sources)
    assert _results(target) != expected  # why the rebuild is never carried as exact
