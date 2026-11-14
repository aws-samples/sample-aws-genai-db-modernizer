# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live PostgreSQL collector reads routines from every user schema (#2268).

Run against a PostgreSQL started by ``pgserver`` on a Unix socket in a temp folder (no
TCP listener): functions, procedures and triggers in two non-public schemas and in a
schema with a quoted odd name are collected with their schema, the system schemas and an
extension's routines are left out, and opting out still reads no body. The tests skip
when ``pgserver`` is not installed or cannot start.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import build_routine_sources
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_ODD = 'Odd "Schema"; x'
_CORPUS = f"""
CREATE SCHEMA app_one;
CREATE SCHEMA app_two;
CREATE SCHEMA "{_ODD.replace('"', '""')}";
CREATE SCHEMA ext_home;
CREATE TABLE public.sc_pub (id int);
CREATE TABLE app_one.sc_orders (id int, status text);
CREATE TABLE "{_ODD.replace('"', '""')}".sc_odd (id int);
CREATE FUNCTION public.sc_pub_fn() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;
-- user functions whose names start with pg (the underscore is no wildcard for a name)
CREATE FUNCTION public.pg_audit_note() RETURNS int LANGUAGE sql AS $$ SELECT 4 $$;
CREATE FUNCTION public.pgsync_rows() RETURNS int LANGUAGE sql AS $$ SELECT 5 $$;
CREATE FUNCTION app_one.pgq_insert() RETURNS int LANGUAGE sql AS $$ SELECT 6 $$;
-- two schemas whose names differ only by case, each with a plpgsql function f
CREATE SCHEMA "Reporting";
CREATE SCHEMA reporting;
CREATE FUNCTION "Reporting".f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 1; END $$;
CREATE FUNCTION reporting.f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 2; END $$;
-- an aggregate and a window function: not collected on either path
CREATE FUNCTION app_one.sc_sfunc(a int, b int) RETURNS int LANGUAGE sql AS $$ SELECT a + b $$;
CREATE AGGREGATE app_one.sc_sum(int) (SFUNC = app_one.sc_sfunc, STYPE = int);
CREATE FUNCTION app_one.sc_one_fn(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT p $$;
CREATE PROCEDURE app_two.sc_two_proc() LANGUAGE plpgsql AS $$ BEGIN NULL; END $$;
CREATE FUNCTION "{_ODD.replace('"', '""')}".sc_odd_fn() RETURNS int LANGUAGE sql AS $$ SELECT 3 $$;
CREATE FUNCTION app_one.sc_trg() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN RETURN NEW; END $$;
CREATE TRIGGER sc_tg_one BEFORE INSERT ON app_one.sc_orders
  FOR EACH ROW EXECUTE FUNCTION app_one.sc_trg();
CREATE FUNCTION "{_ODD.replace('"', '""')}".sc_trg2() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN RETURN NEW; END $$;
CREATE TRIGGER sc_tg_odd BEFORE INSERT ON "{_ODD.replace('"', '""')}".sc_odd
  FOR EACH ROW EXECUTE FUNCTION "{_ODD.replace('"', '""')}".sc_trg2();
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _CORPUS)
    try:
        srv.psql("\\set ON_ERROR_STOP on\nCREATE EXTENSION vector SCHEMA ext_home;")
    except Exception:
        pytest.skip("the bundled PostgreSQL has no vector extension")
    # the pg_cron shape: a table, its trigger and the trigger's function all belong to an
    # extension (here vector's), in a schema of the extension's own
    srv.psql(
        "\\set ON_ERROR_STOP on\n"
        "CREATE TABLE ext_home.ext_jobs (id int);\n"
        "CREATE FUNCTION ext_home.ext_cache_invalidate() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RETURN NEW; END $$;\n"
        "CREATE TRIGGER ext_cache_invalidate BEFORE INSERT ON ext_home.ext_jobs "
        "FOR EACH ROW EXECUTE FUNCTION ext_home.ext_cache_invalidate();\n"
        "ALTER EXTENSION vector ADD TABLE ext_home.ext_jobs;\n"
        "ALTER EXTENSION vector ADD FUNCTION ext_home.ext_cache_invalidate();\n"
    )
    yield srv
    srv.cleanup()


def _live(server) -> PostgreSQLRemoteCollector:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql.strip().rstrip(';')}) t;"
            out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{wrapped}\n")
            rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
            return rows

    return Live.__new__(Live)


def test_routines_come_from_every_user_schema_and_never_a_system_one(server):
    rows = _live(server).collect_procedures()
    pairs = {(r["schema_name"], r["routine_name"]) for r in rows}
    assert {
        ("public", "sc_pub_fn"),
        ("app_one", "sc_one_fn"),
        ("app_two", "sc_two_proc"),
        (_ODD, "sc_odd_fn"),
    } <= pairs
    schemas = {r["schema_name"] for r in rows}
    assert schemas.isdisjoint({"pg_catalog", "information_schema"})
    assert not any(s.startswith(("pg_toast", "pg_temp")) for s in schemas)
    # an extension's own functions (vector, installed in ext_home) are not listed
    assert "ext_home" not in schemas


def test_triggers_come_from_every_user_schema(server):
    rows = _live(server).collect_triggers()
    pairs = {(r["schema_name"], r["trigger_name"]) for r in rows}
    assert {("app_one", "sc_tg_one"), (_ODD, "sc_tg_odd")} <= pairs


def test_each_routine_source_keeps_its_schema_and_the_quoted_name(server):
    db = _live(server)
    build = build_routine_sources(
        "postgresql", procedures=db.collect_procedures(), triggers=db.collect_triggers()
    )
    schemas = {s.schema_name for s in build.sources}
    assert {"app_one", "app_two", _ODD} <= schemas
    (proc,) = [s for s in build.sources if s.schema_name == "app_two"]
    assert "app_two.sc_two_proc" in proc.create_statement
    (odd_fn,) = [s for s in build.sources if s.specific_name.startswith("sc_odd_fn")]
    assert '"Odd ""Schema""; x".sc_odd_fn' in odd_fn.create_statement


def test_opting_out_reads_no_body_in_any_schema(server):
    db = _live(server)
    rows = [r for r in db.collect_procedures(include_bodies=False) if r["schema_name"] != "public"]
    assert {r["schema_name"] for r in rows} >= {"app_one", "app_two", _ODD}
    assert all(r["definition"] is None and r["body_status"] == "opted_out" for r in rows)
    trig = db.collect_triggers(include_bodies=False)
    assert {r["schema_name"] for r in trig} >= {"app_one", _ODD}
    assert all(r["definition"] is None for r in trig)


def test_function_counts_are_read_for_every_user_schema(server):
    server.psql("ALTER DATABASE postgres SET track_functions = 'all';")
    server.psql("CALL app_two.sc_two_proc(); CALL app_two.sc_two_proc();")
    stats, status = _live(server).collect_routine_stats()
    assert status["status"] == "tracked"
    assert ("app_two", "sc_two_proc") in {(r["schema_name"], r["routine_name"]) for r in stats}


def _script_rows(server, name: str) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
    cte = cte.replace(":'routine_bodies_on'", "'t'")
    sql = f"WITH {name} AS ({cte}\n) SELECT json_array_elements(data) AS r FROM {name}"
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{wrapped}\n")
    return [r["r"] for r in json.loads([ln for ln in out.splitlines() if ln.strip()][-1])]


def test_a_user_function_named_pg_something_is_kept(server):
    names = {(r["schema_name"], r["routine_name"]) for r in _live(server).collect_procedures()}
    assert {
        ("public", "pg_audit_note"),
        ("public", "pgsync_rows"),
        ("app_one", "pgq_insert"),
    } <= names
    script = {
        (r["schema_name"], r["routine_name"]) for r in _script_rows(server, "procedures_data")
    }
    assert {("public", "pg_audit_note"), ("public", "pgsync_rows")} <= script


@pytest.mark.passes_on_main
def test_aggregates_are_not_collected(server):
    names = {r["routine_name"] for r in _live(server).collect_procedures()}
    assert "sc_sum" not in names and "sc_sfunc" in names


def test_a_trigger_on_an_extension_table_is_left_out_like_its_function(server):
    live = _live(server)
    assert "ext_cache_invalidate" not in {r["routine_name"] for r in live.collect_procedures()}
    assert "ext_cache_invalidate" not in {r["trigger_name"] for r in live.collect_triggers()}
    script = {r["trigger_name"] for r in _script_rows(server, "triggers_data")}
    assert "ext_cache_invalidate" not in script and "sc_tg_one" in script


def test_another_sessions_temporary_routines_and_a_toast_schema_are_left_out(server):
    # a temporary function and a trigger on a temporary table live in pg_temp_N while
    # their session is open; a function in a pg_toast schema needs allow_system_table_mods
    server.psql(
        "\\set ON_ERROR_STOP on\nSET allow_system_table_mods = on;\n"
        "CREATE FUNCTION pg_toast.sc_toast_fn() RETURNS int LANGUAGE sql AS $$ SELECT 9 $$;\n"
    )
    ready = threading.Event()

    def hold():
        ready.set()
        server.psql(
            "CREATE FUNCTION pg_temp.sc_tmp_fn() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RETURN NEW; END $$;\n"
            "CREATE TEMP TABLE sc_tmp_t (id int);\n"
            "CREATE TRIGGER sc_tmp_tg BEFORE INSERT ON sc_tmp_t FOR EACH ROW "
            "EXECUTE FUNCTION pg_temp.sc_tmp_fn();\nSELECT pg_sleep(6);\n"
        )

    thread = threading.Thread(target=hold)
    thread.start()
    ready.wait()
    time.sleep(2)
    try:
        db = _live(server)
        seen = _run_all(server, "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pg_temp%'")
        assert seen, "the other session's temporary schema must exist for this test"
        for rows in (
            db.collect_procedures(),
            db.collect_triggers(),
            _script_rows(server, "procedures_data"),
            _script_rows(server, "triggers_data"),
        ):
            schemas = {r["schema_name"] for r in rows}
            assert not any(x.startswith(("pg_temp", "pg_toast")) for x in schemas), schemas
    finally:
        thread.join()


def _run_all(server, sql: str) -> list[dict]:
    return _live(server)._query(sql)


def test_function_counts_leave_out_extension_and_system_functions(server):
    server.psql("ALTER DATABASE postgres SET track_functions = 'all';")
    server.psql("SELECT ext_home.vector_dims('[1,2]'::ext_home.vector); SELECT 1;")
    server.psql("SELECT ext_home.vector_dims('[1,2]'::ext_home.vector);")
    stats, _ = _live(server).collect_routine_stats()
    schemas = {r["schema_name"] for r in stats}
    assert "ext_home" not in schemas and not schemas & {"pg_catalog", "information_schema"}


def test_function_counts_keep_schemas_that_differ_only_by_case_apart(server):
    server.psql("ALTER DATABASE postgres SET track_functions = 'all';")
    server.psql('SELECT "Reporting".f(); SELECT reporting.f(); SELECT reporting.f();')
    db = _live(server)
    stats, _ = db.collect_routine_stats()
    procs = [r for r in db.collect_procedures() if r["routine_name"] == "f"]
    from src.agents.collector.routine_rows import attach_routine_stats

    attach_routine_stats(procs, [], stats)
    counts = {p["schema_name"]: p.get("execution_count") for p in procs}
    assert counts["Reporting"] != counts["reporting"] and counts["reporting"] >= 2


def test_the_scripts_function_counts_leave_out_extension_functions(server):
    server.psql("ALTER DATABASE postgres SET track_functions = 'all';")
    server.psql("SELECT ext_home.vector_dims('[1,2]'::ext_home.vector);")
    server.psql("CALL app_two.sc_two_proc();")
    rows = _script_rows(server, "routine_stats_data")
    schemas = {r["schema_name"] for r in rows}
    assert "app_two" in schemas
    assert "ext_home" not in schemas and not schemas & {"pg_catalog", "information_schema"}
