# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PostgreSQL trigger keeps its exact enable mode (#2071).

``pg_trigger.tgenabled`` and ``pg_event_trigger.evtenabled`` have four letters: ``O``
(origin and local sessions), ``D`` (disabled), ``R`` (ENABLE REPLICA: only sessions with
``session_replication_role = replica``) and ``A`` (ENABLE ALWAYS). ``is_enabled`` folds
them into a boolean; ``enabled_mode`` keeps the letter, so a step that recreates a trigger
on the target can set the same mode. A DML trigger, a TRUNCATE-only trigger and an event
trigger in each mode run against a PostgreSQL started by ``pgserver`` on a Unix socket in a
temp folder (no TCP listener), through the live collector and the offline script, read
only. The tests skip when ``pgserver`` is not installed or cannot start.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.postgres_collector import _build_triggers as live_build
from src.agents.collector.routine_sources import non_dml_triggers
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

# Letter -> the ALTER clause that sets it, and what is_enabled has always said.
_MODES = {
    "O": ("ENABLE", True),
    "D": ("DISABLE", False),
    "R": ("ENABLE REPLICA", False),
    "A": ("ENABLE ALWAYS", True),
}


def _corpus() -> str:
    lines = [
        "CREATE TABLE public.em_orders (id int PRIMARY KEY);",
        "CREATE FUNCTION public.em_fn() RETURNS trigger LANGUAGE plpgsql AS"
        " $$BEGIN RETURN NULL; END$$;",
        "CREATE FUNCTION public.em_evt() RETURNS event_trigger LANGUAGE plpgsql AS"
        " $$BEGIN NULL; END$$;",
    ]
    for letter, (clause, _) in _MODES.items():
        name = letter.lower()
        lines += [
            f"CREATE TRIGGER em_dml_{name} AFTER INSERT ON public.em_orders"
            " FOR EACH ROW EXECUTE FUNCTION public.em_fn();",
            f"ALTER TABLE public.em_orders {clause} TRIGGER em_dml_{name};",
            f"CREATE TRIGGER em_trunc_{name} AFTER TRUNCATE ON public.em_orders"
            " FOR EACH STATEMENT EXECUTE FUNCTION public.em_fn();",
            f"ALTER TABLE public.em_orders {clause} TRIGGER em_trunc_{name};",
            f"CREATE EVENT TRIGGER em_evt_{name} ON ddl_command_end"
            " EXECUTE FUNCTION public.em_evt();",
            f"ALTER EVENT TRIGGER em_evt_{name} {clause};",
        ]
    return "\n".join(lines) + "\n"


def _expected(kind: str) -> dict[str, tuple[str, bool]]:
    return {f"em_{kind}_{k.lower()}": (k, enabled) for k, (_, enabled) in _MODES.items()}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _corpus())  # a bad corpus fails, never skips
    yield srv
    srv.cleanup()


def _run(server, sql: str) -> list[dict]:
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]')::text FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{wrapped}\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def _live(server) -> PostgreSQLRemoteCollector:
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"))

    return Live.__new__(Live)


def _script_rows(server, name: str) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
    cte = cte.replace(":'routine_bodies_on'", "'t'")
    sql = f"WITH {name} AS ({cte}\n) SELECT json_array_elements(data) AS r FROM {name}"
    return [r["r"] for r in _run(server, sql)]


def _ours(rows: list[dict]) -> list[dict]:
    return [r for r in rows if str(r.get("trigger_name") or "").startswith("em_")]


def _modes(items) -> dict[str, tuple[str | None, bool | None]]:
    def get(item, key):
        return item.get(key) if isinstance(item, dict) else getattr(item, key)

    return {get(t, "trigger_name"): (get(t, "enabled_mode"), get(t, "is_enabled")) for t in items}


def test_the_live_collector_keeps_each_trigger_s_exact_mode(server):
    db = _live(server)
    triggers, events = _ours(db.collect_triggers()), _ours(db.collect_event_triggers())

    assert _modes(live_build(triggers) or []) == _expected("dml")
    other = non_dml_triggers(triggers, events) or []
    assert _modes(t for t in other if t.scope == "table") == _expected("trunc")
    assert _modes(t for t in other if t.scope == "database") == _expected("evt")


def test_a_script_collection_keeps_each_trigger_s_exact_mode(server):
    raw = {
        "metadata": {"database_name": "shop", "version": "PostgreSQL 16",
                     "routine_capture": {"include_bodies": True, "privileges_not_seen": []}},
        "tables": [{"table_name": "em_orders", "schema_name": "public", "row_count": 0}],
        "columns": [{"table_name": "em_orders", "column_name": "id", "data_type": "int4",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [],
        "triggers": _ours(_script_rows(server, "triggers_data")),
        "event_triggers": _ours(_script_rows(server, "event_triggers_data")),
    }  # fmt: skip
    schema = build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j", database_name="shop", s3_key="k"
    )["database_schema"]

    assert _modes(schema["triggers"]) == _expected("dml")
    other = schema["non_dml_triggers"]
    assert _modes(t for t in other if t["scope"] == "table") == _expected("trunc")
    assert _modes(t for t in other if t["scope"] == "database") == _expected("evt")
