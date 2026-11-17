# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL expression indexes, read from a real server and created again (#2381).

Both index queries (``scripts/collect-postgresql.sql``'s ``indexes_data`` and the live
collector's ``collect_indexes``) run against a PostgreSQL started by ``pgserver`` on a
Unix socket in a temp folder (no TCP listener), and must read each expression index the
same way: its expression text, its plain columns and its predicate. Step 1's DDL, built
from what the live collector read, then runs on the same server in another schema: each
index it recreates has the source's own expression and predicate, as PostgreSQL reports
them. The server is stopped by its postmaster PID at the end. The tests skip when
``pgserver`` cannot start.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from src.tools.data_movement.dms_step import load_step1
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from tests.unit.tools.data_movement.dms_step_fixtures import write_pg_job
from tests.unit.tools.database.test_postgres_foreign_keys_live_server import _ROOT, _run

pgserver = pytest.importorskip("pgserver")

# ``audit_log (msg varchar(200))`` is the step 1 fixture's own table (``write_pg_job``).
_SCHEMA = """
CREATE TABLE audit_log (msg varchar(200));
CREATE INDEX audit_log_lower_msg ON audit_log (lower(msg));
CREATE UNIQUE INDEX audit_log_left_msg ON audit_log (left(msg, 20), md5(msg))
  WHERE msg IS NOT NULL;
CREATE INDEX audit_log_json ON audit_log ((msg::jsonb ->> 'a'));
CREATE INDEX audit_log_mixed ON audit_log (msg, upper(msg));
CREATE INDEX audit_log_plain ON audit_log (msg);
"""
_RECREATED = {"audit_log_lower_msg", "audit_log_left_msg"}
_LISTED = {"audit_log_json", "audit_log_mixed"}
_FIELDS = ("columns", "is_unique", "predicate", "has_expressions", "index_expressions")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    pid = srv.get_pid()
    try:
        srv.psql("\\set ON_ERROR_STOP on\n" + _SCHEMA)  # a bad schema fails, never skips
        yield srv
    finally:
        srv.cleanup()
        if pid is not None and _alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _alive(pid):
                    break
                time.sleep(0.1)


def _live_indexes(server) -> dict[str, dict]:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"), None)

    return {i["index_name"]: i for i in Live.__new__(Live).collect_indexes("audit_log")}


def _script_indexes(server) -> dict[str, dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("indexes_data AS (", 1)[1].split("\n),\n", 1)[0]
    sql = (f"WITH indexes_data AS ({cte}\n) "
           "SELECT jsonb_array_elements(data::jsonb) AS i FROM indexes_data")  # fmt: skip
    rows = [r["i"] for r in _run(server, sql, None)]
    data = {
        "metadata": {"database_name": "db", "engine": "postgresql"},
        "tables": [{"table_name": "audit_log", "row_count": 1}],
        "columns": [],
        "indexes": rows,
        "foreign_keys": [],
        "primary_keys": [],
    }
    [table] = parse_offline_collection(data)["tables"]
    return {i["index_name"]: i for i in table["indexes"]}


def _expressions(server, schema: str) -> dict[str, tuple]:
    rows = _run(server, "SELECT i.relname AS name, pg_get_expr(x.indexprs, x.indrelid) AS e, "
                        "pg_get_expr(x.indpred, x.indrelid) AS p, x.indisunique AS u "
                        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                        "JOIN pg_class t ON t.oid = x.indrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        f"WHERE n.nspname = '{schema}' AND t.relname = 'audit_log'", None)  # fmt: skip
    return {r["name"]: (r["e"], r["p"], r["u"]) for r in rows}


@pytest.mark.passes_on_main  # guards the harness: the server takes no TCP connection
def test_the_server_listens_on_no_tcp_address(server):
    assert _run(server, "SELECT current_setting('listen_addresses') AS a", None) == [{"a": ""}]


@pytest.mark.passes_on_main  # both paths read expressions since #295; this pins their parity
def test_the_script_and_the_live_collector_read_every_index_the_same_way(server):
    script, live = _script_indexes(server), _live_indexes(server)

    assert set(script) == set(live) == _RECREATED | _LISTED | {"audit_log_plain"}
    for name in script:
        assert {f: script[name].get(f) for f in _FIELDS} == {
            f: live[name].get(f) for f in _FIELDS
        }, name
    assert live["audit_log_lower_msg"]["index_expressions"] == "lower((msg)::text)"
    assert live["audit_log_lower_msg"]["columns"] == []
    assert live["audit_log_mixed"]["columns"] == ["msg"]


def test_step_1_creates_the_checked_indexes_again_with_the_sources_expressions(server, tmp_path):
    collected = [
        {k: v for k, v in i.items() if k != "is_primary"} for i in _live_indexes(server).values()
    ]
    inputs = load_step1(write_pg_job(tmp_path, indexes={"audit_log": collected}), "shop", "job1")

    statements = [
        s.strip()
        for s in inputs.ddl.split(";\n")
        if s.lstrip().startswith(('CREATE TABLE "audit_log"', "CREATE INDEX", "CREATE UNIQUE"))
        and '"audit_log"' in s
    ]
    server.psql("\\set ON_ERROR_STOP on\nCREATE SCHEMA step1;\nSET search_path TO step1;\n"
                + ";\n".join(statements) + ";\n")  # fmt: skip

    source, target = _expressions(server, "public"), _expressions(server, "step1")
    assert set(target) == _RECREATED | {"audit_log_plain"}
    for name in _RECREATED:
        assert target[name] == source[name], name
    assert {i.index_name for i in inputs.left_out_indexes} == _LISTED
