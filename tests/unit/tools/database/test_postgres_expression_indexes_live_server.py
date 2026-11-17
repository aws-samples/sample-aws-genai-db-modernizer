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
import re
import signal
import subprocess
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
CREATE INDEX audit_log_coalesce ON audit_log (COALESCE(msg, 'none'), upper(msg));
-- what pg_get_expr(indexprs) does not carry: the review's four, then INCLUDE and WITH
CREATE INDEX audit_log_opclass ON audit_log (lower(msg) text_pattern_ops);
CREATE INDEX audit_log_desc ON audit_log (lower(msg) DESC NULLS LAST);
CREATE UNIQUE INDEX audit_log_collate ON audit_log (lower(msg) COLLATE "C");
CREATE UNIQUE INDEX audit_log_nnd ON audit_log (lower(msg)) NULLS NOT DISTINCT;
CREATE INDEX audit_log_include ON audit_log (upper(msg)) INCLUDE (msg);
CREATE INDEX audit_log_with ON audit_log (md5(msg)) WITH (fillfactor = 50);
"""
_RECREATED = {"audit_log_lower_msg", "audit_log_left_msg", "audit_log_coalesce"}
_SAYS_MORE = {"audit_log_opclass", "audit_log_desc", "audit_log_collate", "audit_log_nnd",
              "audit_log_include", "audit_log_with"}  # fmt: skip
_LISTED = {"audit_log_json", "audit_log_mixed"} | _SAYS_MORE
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


def _indexdefs(server, schema: str) -> dict[str, str]:
    """Each index of ``<schema>.audit_log`` as ``pg_get_indexdef`` writes it, the schema
    taken out, so a source and a recreated index compare whole."""
    rows = _run(server, "SELECT i.relname AS name, pg_get_indexdef(x.indexrelid) AS d "
                        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                        "JOIN pg_class t ON t.oid = x.indrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        f"WHERE n.nspname = '{schema}' AND t.relname = 'audit_log'", None)  # fmt: skip
    return {r["name"]: r["d"].replace(f" ON {schema}.", " ON ") for r in rows}


def _psql_file(server, path) -> subprocess.CompletedProcess:
    """``psql -f`` on ``path``, stopping at the first error, as step-1.ddl.sql is run."""
    psql = pgserver._commands.POSTGRES_BIN_PATH / "psql"
    return subprocess.run(  # nosec B603 -- the bundled psql, a temp file, no shell
        [str(psql), server.get_uri(), "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _audit_log_statements(ddl: str) -> list[str]:
    """The DDL's statements on ``audit_log``, each without the comment lines before it."""
    chunks = [
        "\n".join(ln for ln in s.splitlines() if not ln.startswith("--")) for s in ddl.split(";\n")
    ]
    return [
        s.strip()
        for s in chunks
        if s.lstrip().startswith(('CREATE TABLE "audit_log"', "CREATE INDEX", "CREATE UNIQUE"))
        and '"audit_log"' in s
    ]


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

    script = tmp_path / "step1.sql"
    script.write_text("CREATE SCHEMA step1;\nSET search_path TO step1;\n"
                      + ";\n".join(_audit_log_statements(inputs.ddl)) + ";\n")  # fmt: skip
    ran = _psql_file(server, script)
    assert ran.returncode == 0, ran.stderr

    source, target = _expressions(server, "public"), _expressions(server, "step1")
    assert set(target) == _RECREATED | {"audit_log_plain"}
    for name in _RECREATED:
        assert target[name] == source[name], name
    assert {i.name for i in inputs.left_out_objects} == _LISTED


def test_every_recreated_index_has_the_sources_whole_definition(server, tmp_path):
    """``pg_get_indexdef`` of each recreated index equals the source's, schema aside: a
    property the recreated statement dropped (an operator class, a sort order, a
    collation, NULLS NOT DISTINCT, INCLUDE, WITH) fails it."""
    collected = [
        {k: v for k, v in i.items() if k != "is_primary"} for i in _live_indexes(server).values()
    ]
    inputs = load_step1(write_pg_job(tmp_path, indexes={"audit_log": collected}), "shop", "job1")
    script = tmp_path / "step1.sql"
    script.write_text("CREATE SCHEMA step1_whole;\nSET search_path TO step1_whole;\n"
                      + ";\n".join(_audit_log_statements(inputs.ddl)) + ";\n")  # fmt: skip
    ran = _psql_file(server, script)
    assert ran.returncode == 0, ran.stderr

    source, target = _indexdefs(server, "public"), _indexdefs(server, "step1_whole")
    recreated = set(target) - {"audit_log_plain"}
    assert recreated == _RECREATED
    for name in recreated:
        assert target[name] == source[name], name
    assert not recreated & _SAYS_MORE


def test_psql_runs_no_collected_literal_as_sql_after_a_nul(server, tmp_path):
    """The review's repro: a NUL in one index's literal cut the ``psql -f`` line short and
    left the literal open, so the next index's literal (collector text) ran as SQL."""
    hostile = [
        {"index_name": "nul_ix", "columns": [], "is_unique": False, "index_type": "btree",
         "has_expressions": True, "index_expressions": "COALESCE(msg, 'x\x00')",
         "index_def": "CREATE INDEX nul_ix ON public.audit_log USING btree "
                      "(COALESCE(msg, 'x\x00'))"},
        {"index_name": "next_ix", "columns": [], "is_unique": False, "index_type": "btree",
         "has_expressions": True, "index_expressions": "COALESCE(msg, 'y')",
         "index_def": "CREATE INDEX next_ix ON public.audit_log USING btree "
                      "(COALESCE(msg, 'y'))"},
    ]  # fmt: skip
    inputs = load_step1(write_pg_job(tmp_path, indexes={"audit_log": hostile}), "shop", "job1")
    script = tmp_path / "step1.sql"
    script.write_bytes(("CREATE SCHEMA step1_nul;\nSET search_path TO step1_nul;\n"
                        + ";\n".join(_audit_log_statements(inputs.ddl)) + ";\n").encode())  # fmt: skip

    ran = _psql_file(server, script)
    assert ran.returncode == 0, ran.stderr
    assert not re.search(r"syntax error", ran.stderr)
    assert set(_expressions(server, "step1_nul")) == {"next_ix"}
    assert [i.name for i in inputs.left_out_objects] == ["nul_ix"]
    assert b"\x00" not in script.read_bytes()


def test_psql_runs_no_collected_name_as_sql_after_a_nul(server, tmp_path):
    """Review 2's repro: an index named ``a<NUL> tail`` then one named ``SELECT 1``. A NUL
    in a quoted name cut the ``psql -f`` line short and left the identifier open, so the
    next index's name ran as SQL (``syntax error at or near "SELECT"``)."""
    plain = [
        {"index_name": "a\x00 tail", "columns": ["msg"], "is_unique": False},
        {"index_name": "SELECT 1", "columns": ["msg"], "is_unique": False},
    ]
    inputs = load_step1(write_pg_job(tmp_path, indexes={"audit_log": plain}), "shop", "job1")
    script = tmp_path / "step1.sql"
    script.write_bytes(
        (
            "CREATE SCHEMA step1_name;\nSET search_path TO step1_name;\n"
            + ";\n".join(_audit_log_statements(inputs.ddl))
            + ";\n"
        ).encode()
    )

    ran = _psql_file(server, script)
    assert ran.returncode == 0, ran.stderr
    assert not re.search(r"syntax error", ran.stderr)
    assert set(_indexdefs(server, "step1_name")) == {"SELECT 1"}
    assert [(o.kind, o.name) for o in inputs.left_out_objects] == [("index", "a\x00 tail")]
    assert b"\x00" not in script.read_bytes()
