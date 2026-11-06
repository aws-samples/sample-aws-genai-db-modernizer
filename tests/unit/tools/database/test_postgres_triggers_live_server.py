# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL triggers, read from a real server by a read-only user (#1651).

Both trigger queries (``scripts/collect-postgresql.sql`` and the live collector's
``collect_triggers``) run against a PostgreSQL started by ``pgserver`` on a Unix socket in
a temp folder (no TCP listener), as the owner and as a role with SELECT only.
``information_schema.triggers`` lists only the triggers on tables the user owns or holds
more than SELECT on, so a read-only collector saw none; both queries read ``pg_trigger``
and must give every DML trigger once, with its timing and every event, whoever runs them.
The tests skip when ``pgserver`` is not installed or cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.collector.mysql_collector import _build_triggers as offline_build
from src.agents.collector.postgres_collector import _build_triggers as live_build
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_SCHEMA = """
CREATE SCHEMA other;
CREATE FUNCTION trg_fn() RETURNS trigger LANGUAGE plpgsql AS
  $$BEGIN RETURN coalesce(NEW, OLD); END$$;

CREATE TABLE orders (id int PRIMARY KEY, a int);
CREATE TABLE items (id int, order_id int REFERENCES orders (id), b int);
CREATE TABLE other.orders (id int);
CREATE VIEW v_orders AS SELECT id, a FROM orders;

-- two events; the same name on another table and on the same table name elsewhere
CREATE TRIGGER tg_multi BEFORE INSERT OR UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION trg_fn();
CREATE TRIGGER tg_multi AFTER DELETE ON items
  FOR EACH ROW EXECUTE FUNCTION trg_fn();
CREATE TRIGGER tg_multi AFTER UPDATE OR DELETE ON other.orders
  FOR EACH ROW EXECUTE FUNCTION trg_fn();
-- all three events, per statement, and a column list with a WHEN clause
CREATE TRIGGER tg_stmt AFTER DELETE OR INSERT OR UPDATE ON orders
  FOR EACH STATEMENT EXECUTE FUNCTION trg_fn();
CREATE TRIGGER tg_upd_of BEFORE UPDATE OF b ON items
  FOR EACH ROW WHEN (NEW.b IS DISTINCT FROM OLD.b) EXECUTE FUNCTION trg_fn();
-- INSTEAD OF on a view
CREATE TRIGGER tg_view INSTEAD OF INSERT OR DELETE ON v_orders
  FOR EACH ROW EXECUTE FUNCTION trg_fn();
-- TRUNCATE only: no DML event, so the contract cannot hold it
CREATE TRIGGER tg_trunc AFTER TRUNCATE ON orders
  FOR EACH STATEMENT EXECUTE FUNCTION trg_fn();
-- the foreign key above also gives internal triggers, which never count
-- a partitioned table's trigger is cloned onto each partition: one trigger, the parent's
CREATE TABLE events (tenant int, v int) PARTITION BY LIST (tenant);
CREATE TABLE events_1 PARTITION OF events FOR VALUES IN (1);
CREATE TABLE events_2 PARTITION OF events FOR VALUES IN (2);
CREATE TRIGGER tg_part AFTER INSERT OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION trg_fn();

-- the role the script header asks for: SELECT only, owns nothing
CREATE ROLE ro_collector;
GRANT USAGE ON SCHEMA public, other TO ro_collector;
GRANT SELECT ON ALL TABLES IN SCHEMA public, other TO ro_collector;
"""

# (schema, table, trigger) -> (timing, events)
_EXPECTED = {
    ("public", "orders", "tg_multi"): ("BEFORE", ["INSERT", "UPDATE"]),
    ("public", "items", "tg_multi"): ("AFTER", ["DELETE"]),
    ("other", "orders", "tg_multi"): ("AFTER", ["UPDATE", "DELETE"]),
    ("public", "orders", "tg_stmt"): ("AFTER", ["INSERT", "UPDATE", "DELETE"]),
    ("public", "items", "tg_upd_of"): ("BEFORE", ["UPDATE"]),
    ("public", "v_orders", "tg_view"): ("INSTEAD OF", ["INSERT", "DELETE"]),
    ("public", "events", "tg_part"): ("AFTER", ["INSERT", "DELETE"]),
}
_PARTITIONS = {"events_1", "events_2"}
_ROLES = pytest.mark.parametrize("role", [None, "ro_collector"], ids=["owner", "select-only"])


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SCHEMA)  # a bad schema fails, never skips
    yield srv
    srv.cleanup()


def _run(server, sql: str, role: str | None) -> list[dict]:
    set_role = f"SET ROLE {role};\n" if role else ""
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]') FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{set_role}{wrapped}\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def _script_rows(server, role, pg_trigger: str = "pg_trigger") -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("triggers_data AS (", 1)[1].split("\n),\n", 1)[0]
    cte = cte.replace("FROM pg_trigger t", f"FROM {pg_trigger} t")
    # #1652: the psql variable the script sets (include_routine_bodies, default 1).
    cte = cte.replace(":'routine_bodies_on'", "'t'")
    sql = (
        f"WITH triggers_data AS ({cte}\n) "
        "SELECT json_array_elements(data) AS tr FROM triggers_data"
    )
    return [r["tr"] for r in _run(server, sql, role)]


def _information_schema_rows(server, role) -> list[dict]:
    return _run(
        server,
        "SELECT trigger_name, trigger_schema AS schema_name, event_manipulation AS event_type,"
        " event_object_table AS table_name, action_timing AS timing,"
        " action_statement AS definition FROM information_schema.triggers"
        " WHERE trigger_schema NOT IN ('pg_catalog', 'information_schema')",
        role,
    )


@pytest.mark.passes_on_main
def test_information_schema_hides_the_triggers_from_a_select_only_role(server):
    """Why the queries read pg_trigger: the role really is SELECT only."""
    assert _information_schema_rows(server, None)
    assert _information_schema_rows(server, "ro_collector") == []


@_ROLES
def test_the_offline_script_gives_every_trigger_once_to_any_role(server, role):
    rows = _script_rows(server, role)
    got = {(r["schema_name"], r["table_name"], r["trigger_name"]) for r in rows}
    assert len(rows) == len(got)  # one row per trigger
    parsed = parse_offline_collection(
        {
            "metadata": {"database_name": "postgres", "engine": "postgresql"},
            "tables": [{"table_name": "orders", "row_count": 1}],
            "columns": [],
            "queries": [],
            "triggers": rows[::-1],
        }
    )
    triggers = offline_build(parsed["triggers"]) or []

    assert sorted((t.table_id, t.trigger_name, t.timing.value, [e.value for e in t.events])
                  for t in triggers) == sorted(
        (table, name, timing, events) for (_, table, name), (timing, events) in _EXPECTED.items()
    )  # fmt: skip
    # #1652: the definition is the whole CREATE TRIGGER (pg_get_triggerdef).
    assert all(t.definition and t.definition.startswith("CREATE TRIGGER") for t in triggers)
    assert all(t.function_id == "trg_fn" for t in triggers)


def test_the_offline_script_matches_information_schema_for_the_owner(server):
    """Timing, events and definition as information_schema.triggers gives them (which also
    lists each partition's clone, left out here). Since #1652 the script's definition is
    the whole CREATE TRIGGER, which ends with information_schema's action_statement."""

    def by_trigger(rows):
        out: dict[tuple, tuple] = {}
        for r in rows:
            key = (r["schema_name"], r["table_name"], r["trigger_name"])
            events = set(str(r["event_type"]).split(" OR ")) - {"TRUNCATE"}
            if not events or r["table_name"] in _PARTITIONS:
                continue
            _, have, _ = out.get(key, (None, set(), None))
            out[key] = (r["timing"], have | events, r["definition"])
        return out

    script = by_trigger(_script_rows(server, None))
    information_schema = by_trigger(_information_schema_rows(server, None))
    assert {k: v[:2] for k, v in script.items()} == {
        k: v[:2] for k, v in information_schema.items()
    }
    assert script.keys() == information_schema.keys()
    for key, (_, _, definition) in script.items():
        assert definition.startswith("CREATE TRIGGER")
        assert definition.endswith(information_schema[key][2]), key


@_ROLES
def test_the_live_collector_gives_every_public_trigger_to_any_role(server, role):
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"), role)

    rows = Live.__new__(Live).collect_triggers()
    triggers = live_build(rows) or []

    assert sorted((t.table_id, t.trigger_name, t.timing.value, [e.value for e in t.events])
                  for t in triggers) == sorted(
        (table, name, timing, events)
        for (schema, table, name), (timing, events) in _EXPECTED.items()
        if schema == "public"
    )  # fmt: skip


@pytest.mark.passes_on_main
def test_the_partition_guard_runs_where_pg_trigger_has_no_tgparentid(server):
    """PostgreSQL 12 has no tgparentid (its partition clones are internal): the script's
    guard must still run there. Here pg_trigger is cut to 12's columns, so on this server,
    whose clones are not internal, each partition's clone is listed."""
    old = "(SELECT oid, tgrelid, tgname, tgtype, tgisinternal, tgenabled, tgfoid FROM pg_trigger)"
    rows = [r for r in _script_rows(server, None, old) if r["trigger_name"] == "tg_part"]
    assert sorted((r["table_name"], r["event_type"]) for r in rows) == [
        (table, "INSERT OR DELETE") for table in sorted({"events", *_PARTITIONS})
    ]
