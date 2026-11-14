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

import pytest

from src.agents.collector.routine_sources import build_routine_sources
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

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
