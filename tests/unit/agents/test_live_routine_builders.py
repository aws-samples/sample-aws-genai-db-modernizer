# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live collectors keep routine bodies, every trigger's orientation and state, the
execution counts the engine keeps and how they were read (#1652)."""

from __future__ import annotations

import pytest

from src.agents.collector import (
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)


class _Db:
    """The schema reads a live collector makes, answered from canned rows."""

    def __init__(self, **rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        if not name.startswith("collect_"):
            raise AttributeError(name)

        def read(*args, **kwargs):
            self.calls.append((name, kwargs))
            value = self.rows.get(name, [])
            if isinstance(value, Exception):
                raise value
            return value

        return read


# -- SQL Server --------------------------------------------------------------------


def test_sqlserver_procedures_keep_body_schema_and_counts():
    (p,) = sqlserver_collector._build_procedures(
        [{"routine_name": "usp_place", "schema_name": "sales", "routine_type": "PROCEDURE",
          "definition": "CREATE PROCEDURE x AS SELECT 1", "execution_count": 4,
          "total_time_ms": 2}]
    )  # fmt: skip
    assert (p.procedure_id, p.schema_name) == ("sales.usp_place", "sales")
    assert p.definition == "CREATE PROCEDURE x AS SELECT 1"
    assert p.execution_stats is not None and p.execution_stats.total_executions == 4


@pytest.mark.passes_on_main
def test_sqlserver_encrypted_module_is_listed_without_a_body():
    (p,) = sqlserver_collector._build_procedures(
        [{"routine_name": "usp_secret", "schema_name": "dbo", "routine_type": "PROCEDURE",
          "definition": None}]
    )  # fmt: skip
    assert (p.procedure_id, p.definition) == ("dbo.usp_secret", None)


def test_sqlserver_trigger_keeps_every_event_state_and_body():
    (t,) = sqlserver_collector._build_triggers(
        [{"trigger_name": "tr", "table_name": "orders", "schema_name": "dbo",
          "timing": "INSTEAD OF", "event_type": "INSERT", "events": "INSERT,UPDATE",
          "for_each": "STATEMENT", "is_enabled": 0, "execution_count": 2,
          "definition": "CREATE TRIGGER tr ON dbo.orders INSTEAD OF INSERT AS SELECT 1"}]
    )  # fmt: skip
    assert t.timing.value == "INSTEAD OF"
    assert [e.value for e in t.events] == ["INSERT", "UPDATE"]
    assert (t.for_each.value, t.is_enabled) == ("STATEMENT", False)
    assert t.definition is not None and t.execution_stats.total_executions == 2


def test_sqlserver_schema_stage_attaches_counts_and_their_status():
    db = _Db(
        collect_tables=[],
        collect_procedures=[{"routine_name": "usp", "schema_name": "dbo", "routine_type": "PROCEDURE"}],
        collect_triggers=[],
        collect_routine_stats=([{"routine_name": "usp", "schema_name": "dbo",
                                 "object_type": "PROCEDURE", "execution_count": 9}],
                               {"status": "tracked"}),
    )  # fmt: skip
    raw = sqlserver_collector._collect_schema_raw(db)
    assert raw["procedures"][0]["execution_count"] == 9
    assert raw["routine_stats_status"] == {"status": "tracked"}


def test_a_failed_count_read_never_fails_the_schema_stage():
    db = _Db(collect_tables=[], collect_routine_stats=RuntimeError("no VIEW SERVER STATE"))
    raw = sqlserver_collector._collect_schema_raw(db)
    assert raw["routine_stats_status"] is None


# -- PostgreSQL --------------------------------------------------------------------


def test_postgresql_procedure_keeps_body_counts_and_migration_fields():
    (p,) = postgres_collector._build_procedures(
        [{"routine_name": "place_order", "schema_name": "public", "routine_type": "FUNCTION",
          "language": "PLPGSQL", "definition": "CREATE FUNCTION place_order() ...",
          "specific_name": "place_order_16390", "owner": "app", "security_definer": "t",
          "depends_on": '[{"kind": "table", "name": "orders"}]', "execution_count": 9,
          "body_status": "collected", "body_chars": 30}]
    )  # fmt: skip
    assert p.definition is not None and p.execution_stats.total_executions == 9
    assert (p.specific_name, p.owner, p.security_definer) == ("place_order_16390", "app", True)
    assert [d.name for d in p.depends_on] == ["orders"]
    assert (p.schema_name, p.body_status.value, p.body_chars) == ("public", "collected", 30)


def test_postgresql_trigger_names_its_function_orientation_and_state():
    (t,) = postgres_collector._build_triggers(
        [{"trigger_name": "tg", "table_name": "orders", "schema_name": "public",
          "timing": "AFTER", "event_type": "INSERT OR UPDATE", "for_each": "ROW",
          "is_enabled": "D", "function_signature": "log_order()",
          "definition": "CREATE TRIGGER tg AFTER INSERT OR UPDATE ON public.orders FOR EACH ROW "
                        "EXECUTE FUNCTION log_order()"}]
    )  # fmt: skip
    assert [e.value for e in t.events] == ["INSERT", "UPDATE"]
    assert (t.function_id, t.for_each.value, t.is_enabled) == ("log_order", "ROW", False)
    assert [d.name for d in t.depends_on] == ["log_order()"]


def test_postgresql_nested_reaches_the_pattern():
    queries = postgres_collector._build_queries(
        [{"query_id": "1", "query_text": "UPDATE orders SET a = 1", "frequency_per_hour": 1,
          "tables_accessed": ["orders"], "nested": True}]
    )  # fmt: skip
    assert queries.query_patterns[0].nested is True


def test_postgresql_schema_stage_reads_counts_and_records_the_capture():
    db = _Db(
        collect_tables=[], collect_procedures=[{"routine_name": "f"}],
        collect_routine_stats=([{"routine_name": "f", "execution_count": 2}],
                               {"status": "tracked"}),
    )  # fmt: skip
    raw = postgres_collector._collect_schema_raw(db, include_bodies=False)
    assert raw["procedures"][0]["execution_count"] == 2
    assert raw["routine_stats_status"] == {"status": "tracked"}
    assert raw["routine_capture"] == {"include_bodies": False, "privileges_not_seen": []}
    assert ("collect_procedures", {"include_bodies": False}) in db.calls


# -- Oracle ------------------------------------------------------------------------


def test_oracle_compound_event_keeps_every_event_orientation_and_state():
    (t,) = oracle_collector._build_triggers(
        [{"trigger_name": "tg", "table_name": "orders", "schema_name": "app",
          "timing": "BEFORE", "event_type": "INSERT OR UPDATE",
          "for_each": "BEFORE EACH ROW", "is_enabled": "DISABLED", "definition": "TRIGGER tg"}]
    )  # fmt: skip
    assert [e.value for e in t.events] == ["INSERT", "UPDATE"]
    assert (t.timing.value, t.for_each.value, t.is_enabled) == ("BEFORE", "ROW", False)
    assert t.definition == "TRIGGER tg" and t.function_id is None


def test_oracle_schema_stage_joins_bodies_and_says_no_counts_are_kept():
    db = _Db(
        collect_tables=[],
        collect_procedures=[{"routine_name": "p", "schema_name": "app", "routine_type": "PROCEDURE"}],
        collect_routine_sources=[{"owner": "app", "name": "p", "type": "PROCEDURE", "line": 1,
                                  "text": "PROCEDURE p IS BEGIN NULL; END;"}],
    )  # fmt: skip
    raw = oracle_collector._collect_schema_raw(db)
    assert raw["procedures"][0]["definition"] == "PROCEDURE p IS BEGIN NULL; END;"
    assert raw["routine_stats_status"]["status"] == "not_tracked"
    assert "routine_sources" not in raw  # only definitions, which a checkpoint redacts
    (p,) = oracle_collector._build_procedures(raw["procedures"])
    assert (p.procedure_id, p.definition) == ("app.p", "PROCEDURE p IS BEGIN NULL; END;")


# -- MySQL -------------------------------------------------------------------------


def test_mysql_schema_stage_reads_events_counts_and_the_capture():
    db = _Db(
        collect_tables=[],
        collect_procedures=[{"routine_name": "p", "schema_name": "shop", "routine_type": "PROCEDURE"}],
        collect_events=[{"event_name": "purge"}],
        collect_routine_stats=([{"routine_name": "p", "schema_name": "shop",
                                 "object_type": "PROCEDURE", "execution_count": 12}],
                               {"status": "tracked"}),
        collect_routine_capture={"routines_counted": 2, "privileges_not_seen": ["EVENT"]},
    )  # fmt: skip
    raw = mysql_collector._collect_schema_raw(db, include_bodies=False)
    assert raw["procedures"][0]["execution_count"] == 12
    assert raw["events"] == [{"event_name": "purge"}]
    assert raw["routine_capture"] == {
        "include_bodies": False, "routines_counted": 2, "privileges_not_seen": ["EVENT"],
    }  # fmt: skip
    fields = mysql_collector.live_routine_fields(raw)
    assert fields["routine_stats_source"] == {"status": "tracked"}
    assert fields["routine_capture"]["routines_listed"] == 2
    assert [e.event_id for e in fields["events"]] == ["purge"]


def test_a_read_a_server_lacks_costs_only_that_read():
    db = _Db(
        collect_tables=[],
        collect_procedures=[{"routine_name": "p", "routine_type": "PROCEDURE"}],
        collect_events=RuntimeError("no information_schema.events"),
        collect_routine_stats=RuntimeError("no performance_schema"),
        collect_routine_capture=RuntimeError("no user_privileges"),
    )  # fmt: skip
    raw = mysql_collector._collect_schema_raw(db)
    assert raw["procedures"] == [{"routine_name": "p", "routine_type": "PROCEDURE"}]
    assert (raw["events"], raw["routine_stats_status"]) == ([], None)
    assert raw["routine_capture"] == {"include_bodies": True}


def test_an_older_checkpoint_has_no_routine_fields():
    assert mysql_collector.live_routine_fields({"tables": []}) == {
        "events": None, "routine_stats_source": None, "routine_capture": None,
    }  # fmt: skip
