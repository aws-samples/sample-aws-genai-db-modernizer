# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A TRUNCATE-only trigger and event triggers are collected, kept and load back (#2023).

The live collector's reads and the offline script's sections run against a PostgreSQL
started by ``pgserver`` on a Unix socket in a temp folder (no TCP listener), as in
``test_postgres_routine_round_trip.py``. Each trigger is collected, its routine source
built, the triggers dropped and the sources loaded back; collecting again must give the
same rows. A role with no privilege beyond the catalog must see them all. The tests skip
when ``pgserver`` is not installed or cannot start.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.routine_sources import build_routine_sources
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]
_SEED = "jane@example.com"

_CORPUS = f"""
CREATE TABLE public.nd_orders (id int PRIMARY KEY);
CREATE FUNCTION public.nd_audit() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN RETURN NULL; END $$;
CREATE TRIGGER nd_truncate BEFORE TRUNCATE ON public.nd_orders
  FOR EACH STATEMENT EXECUTE FUNCTION public.nd_audit('it''s {_SEED}');
CREATE FUNCTION public."NdGuard"() RETURNS event_trigger LANGUAGE plpgsql AS $$
  BEGIN RAISE NOTICE 'ddl %', tg_tag; END $$;
CREATE EVENT TRIGGER "Nd Guard" ON ddl_command_start WHEN TAG IN ('DROP TABLE', 'ALTER TABLE')
  EXECUTE FUNCTION public."NdGuard"();
CREATE EVENT TRIGGER nd_end ON ddl_command_end EXECUTE FUNCTION public."NdGuard"();
ALTER EVENT TRIGGER nd_end DISABLE;
CREATE ROLE nd_ro;
"""


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


def _ours(rows: list[dict]) -> list[dict]:
    return sorted(
        (r for r in rows if str(r.get("trigger_name") or "").lower().startswith("nd")),
        key=lambda r: str(r["trigger_name"]),
    )


def _script_rows(server, name: str, include: bool = True) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
    cte = cte.replace(":'routine_bodies_on'", "'t'" if include else "'f'")
    sql = f"WITH {name} AS ({cte}\n) SELECT json_array_elements(data) AS r FROM {name}"
    return [r["r"] for r in _run(server, sql)]


def test_each_create_loads_back_to_the_same_trigger(server):
    db = _live(server)
    triggers = _ours(db.collect_triggers())
    events = _ours(db.collect_event_triggers())
    assert [t["trigger_name"] for t in triggers] == ["nd_truncate"]
    assert [(e["trigger_name"], e["event"], e["tags"], e["is_enabled"]) for e in events] == [
        ("Nd Guard", "ddl_command_start", ["DROP TABLE", "ALTER TABLE"], "O"),
        ("nd_end", "ddl_command_end", [], "D"),
    ]
    build = build_routine_sources(
        "postgresql", procedures=[], triggers=triggers, event_triggers=events
    )
    assert (sorted((s.kind, s.specific_name) for s in build.sources), build.statuses) == (
        [("event_trigger", "Nd Guard"), ("event_trigger", "nd_end"),
         ("trigger", "nd_orders.nd_truncate")],
        {},
    )  # fmt: skip
    assert any(_SEED in s.create_statement for s in build.sources)

    drops = [
        "DROP TRIGGER nd_truncate ON public.nd_orders;",
        'DROP EVENT TRIGGER "Nd Guard";',
        "DROP EVENT TRIGGER nd_end;",
    ]
    loads = [s.create_statement.rstrip().rstrip(";") + ";" for s in build.sources]
    # The state is kept apart from the CREATE, as a trigger's is (is_enabled).
    server.psql("\\set ON_ERROR_STOP on\n" + "\n".join(
        drops + loads + ["ALTER EVENT TRIGGER nd_end DISABLE;"]))  # fmt: skip

    assert _ours(db.collect_triggers()) == triggers
    assert _ours(db.collect_event_triggers()) == events


def test_the_script_reads_what_the_live_collector_reads(server):
    db = _live(server)
    assert _ours(_script_rows(server, "event_triggers_data")) == _ours(db.collect_event_triggers())
    assert [t["definition"] for t in _ours(_script_rows(server, "triggers_data"))] == [
        t["definition"] for t in _ours(db.collect_triggers())
    ]


def test_a_role_with_no_grant_sees_them_all(server):
    db = _live(server, "nd_ro")
    assert [t["trigger_name"] for t in _ours(db.collect_triggers())] == ["nd_truncate"]
    assert [e["trigger_name"] for e in _ours(db.collect_event_triggers())] == ["Nd Guard", "nd_end"]


def test_a_script_collection_keeps_them_in_the_contract(server):
    raw = {
        "metadata": {"database_name": "shop", "version": "PostgreSQL 16",
                     "routine_capture": {"include_bodies": True, "privileges_not_seen": []}},
        "tables": [{"table_name": "nd_orders", "schema_name": "public", "row_count": 0}],
        "columns": [{"table_name": "nd_orders", "column_name": "id", "data_type": "int4",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [],
        "triggers": _ours(_script_rows(server, "triggers_data")),
        "event_triggers": _ours(_script_rows(server, "event_triggers_data")),
    }  # fmt: skip
    output = build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j", database_name="shop", s3_key="k"
    )
    kept = {t["trigger_name"]: t for t in output["database_schema"]["non_dml_triggers"]}
    assert set(kept) == {"nd_truncate", "Nd Guard", "nd_end"}
    assert {t["body_status"] for t in kept.values()} == {"collected"}
    assert output["database_schema"].get("triggers") is None
    assert _SEED not in json.dumps(output)


def test_opting_out_reads_names_and_sizes_only(server):
    rows = _ours(_script_rows(server, "event_triggers_data", include=False))
    assert [r["body_status"] for r in rows] == ["opted_out", "opted_out"]
    assert (
        build_routine_sources("postgresql", procedures=[], triggers=[], event_triggers=rows).sources
        == []
    )
