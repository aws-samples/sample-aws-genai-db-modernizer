# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each collected PostgreSQL CREATE loads back to the same routine (#1652).

The live collector's routine reads, and the offline script's sections 8 and 9, run
against a PostgreSQL started by ``pgserver`` on a Unix socket in a temp folder (no TCP
listener). Every function, procedure and trigger of a small corpus is collected, its
routine source built, the objects dropped and the sources loaded back; collecting again
must give the same routines. A role with SELECT only must see every one of them, and a
routine an extension owns is not listed. The tests skip when ``pgserver`` is not
installed or cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import (
    build_routine_sources,
    routine_sources_from_collection,
)
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_CORPUS = """
CREATE TABLE public.rt_orders (id int PRIMARY KEY, status text, total numeric(10,2));
CREATE FUNCTION public.rt_set_status(p_id integer) RETURNS void LANGUAGE plpgsql
  SECURITY DEFINER SET search_path = public AS $$
  BEGIN UPDATE rt_orders SET status = 'shipped; it''s done' WHERE id = p_id; END $$;
CREATE FUNCTION public.rt_set_status(p_id text) RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION public.rt_total(p_id integer) RETURNS numeric LANGUAGE sql STABLE
  BEGIN ATOMIC SELECT total FROM public.rt_orders WHERE id = p_id; END;
CREATE PROCEDURE public.rt_purge() LANGUAGE plpgsql AS $$
  BEGIN DELETE FROM rt_orders WHERE status = 'void'; END $$;
CREATE FUNCTION public."RtLogOrder"() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN NEW.status := coalesce(NEW.status, 'new'); RETURN NEW; END $$;
CREATE TRIGGER rt_tg BEFORE INSERT OR UPDATE OF status ON public.rt_orders
  FOR EACH ROW WHEN (NEW.id > 0) EXECUTE FUNCTION public."RtLogOrder"();
CREATE AGGREGATE public.rt_agg(integer) (SFUNC = int4pl, STYPE = integer);
CREATE ROLE rt_ro;
GRANT USAGE ON SCHEMA public TO rt_ro;
GRANT SELECT ON public.rt_orders TO rt_ro;
REVOKE EXECUTE ON PROCEDURE public.rt_purge() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.rt_total(integer) FROM PUBLIC;
"""
_NAMES = ["RtLogOrder", "rt_purge", "rt_set_status", "rt_set_status", "rt_total"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _CORPUS)  # a bad corpus fails, never skips
    yield srv
    srv.cleanup()


def _run(server, sql: str, role: str | None = None) -> list[dict]:
    set_role = f"SET ROLE {role};\n" if role else ""
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{set_role}{wrapped}\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def _live(server, role: str | None = None) -> PostgreSQLRemoteCollector:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"), role)

    return Live.__new__(Live)


def _ours(rows: list[dict], key: str) -> list[dict]:
    return sorted(
        (r for r in rows if str(r.get(key) or "").lower().startswith("rt")),
        key=lambda r: (str(r[key]), str(r.get("identity_arguments") or "")),
    )


def _shape(p: dict) -> tuple:
    return (
        p["routine_name"], p["identity_arguments"], p["definition"], p["security_definer"],
        json.dumps(p["depends_on"], sort_keys=True), p["owner"], p["routine_type"],
    )  # fmt: skip


def _script_rows(server, name: str, include: str = "1") -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
    cte = cte.replace(":'routine_bodies_on'", "'t'" if include == "1" else "'f'")
    sql = f"WITH {name} AS ({cte}\n) SELECT json_array_elements(data) AS r FROM {name}"
    return [r["r"] for r in _run(server, sql)]


def test_every_create_loads_back_to_the_same_routine(server):
    db = _live(server)
    procedures = _ours(db.collect_procedures(), "routine_name")
    triggers = _ours(db.collect_triggers(), "trigger_name")
    assert [p["routine_name"] for p in procedures] == sorted(_NAMES)  # no aggregate
    total = next(p for p in procedures if p["routine_name"] == "rt_total")
    assert {"kind": "table", "name": "rt_orders"} in total["depends_on"]
    assert next(p for p in procedures if p["identity_arguments"] == "p_id integer"
                and p["routine_name"] == "rt_set_status")["security_definer"] is True  # fmt: skip
    (trigger,) = triggers
    assert (trigger["is_enabled"], trigger["for_each"]) == ("O", "ROW")
    build = build_routine_sources("postgresql", procedures=procedures, triggers=triggers)
    assert (len(build.sources), build.statuses) == (6, {})
    assert any("shipped; it''s done" in s.create_statement for s in build.sources)

    drops = ["DROP TRIGGER rt_tg ON public.rt_orders;"] + [
        f'DROP {p["routine_type"]} public."{p["routine_name"]}"({p["identity_arguments"]});'
        for p in procedures
    ]
    loads = [s.create_statement.rstrip().rstrip(";") + ";" for s in sorted(
        build.sources, key=lambda s: s.kind == "trigger")]  # fmt: skip
    server.psql("\\set ON_ERROR_STOP on\n" + "\n".join(drops + loads))

    again = _ours(db.collect_procedures(), "routine_name")
    assert sorted(map(_shape, again)) == sorted(map(_shape, procedures))
    assert [t["definition"] for t in _ours(db.collect_triggers(), "trigger_name")] == [
        t["definition"] for t in triggers
    ]


def test_a_select_only_role_sees_every_routine_and_trigger(server):
    # Again here: a routine loaded back (above) has the default EXECUTE grant.
    server.psql(
        "\\set ON_ERROR_STOP on\n"
        "REVOKE EXECUTE ON PROCEDURE public.rt_purge() FROM PUBLIC;\n"
        "REVOKE EXECUTE ON FUNCTION public.rt_total(integer) FROM PUBLIC;\n"
    )
    db = _live(server, "rt_ro")
    procedures = _ours(db.collect_procedures(), "routine_name")
    assert [p["routine_name"] for p in procedures] == sorted(_NAMES)
    assert all(p["definition"] and p["body_status"] == "collected" for p in procedures)
    assert [t["trigger_name"] for t in _ours(db.collect_triggers(), "trigger_name")] == ["rt_tg"]
    # information_schema.routines hides the ones whose EXECUTE was revoked
    hidden = _run(server, "SELECT routine_name FROM information_schema.routines "
                          "WHERE routine_schema = 'public'", "rt_ro")  # fmt: skip
    assert {"rt_purge", "rt_total"}.isdisjoint({r["routine_name"] for r in hidden})


def test_the_script_reads_what_the_live_collector_reads(server):
    live = {(p["routine_name"], p["identity_arguments"]): p["definition"]
            for p in _ours(_live(server).collect_procedures(), "routine_name")}  # fmt: skip
    script = {(p["routine_name"], p["identity_arguments"]): p["definition"]
              for p in _ours(_script_rows(server, "procedures_data"), "routine_name")}  # fmt: skip
    assert script == live
    assert [
        t["definition"] for t in _ours(_script_rows(server, "triggers_data"), "trigger_name")
    ] == [t["definition"] for t in _ours(_live(server).collect_triggers(), "trigger_name")]


def test_opting_out_reads_names_and_sizes_only(server):
    rows = _ours(_script_rows(server, "procedures_data", include="0"), "routine_name")
    assert [r["routine_name"] for r in rows] == sorted(_NAMES)
    assert all(r["definition"] is None and r["body_status"] == "opted_out" for r in rows)
    assert all(r["body_chars"] > 0 for r in rows)
    raw = {"metadata": {"engine": "postgresql", "version": "PostgreSQL 16"}, "procedures": rows,
           "triggers": _ours(_script_rows(server, "triggers_data", include="0"), "trigger_name")}  # fmt: skip
    assert routine_sources_from_collection(raw).sources == []


def test_function_counts_say_tracking_is_off_without_changing_it(server):
    (status,) = _run(server, "SELECT * FROM (" + _script_body("routine_stats_status_data") + ") s")
    assert status["data"] == {"status": "not_tracked", "reason": "track_functions is none"}
    stats, status = _live(server).collect_routine_stats()
    assert (stats, status["status"]) == ([], "not_tracked")
    (setting,) = _run(server, "SELECT current_setting('track_functions') AS v")
    assert setting["v"] == "none"


def _script_body(name: str) -> str:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    return script.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
