# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collector path keeps every DML trigger with its timing and events (#1651).

The offline path is ``parse_offline_collection`` then the shared ``_build_triggers``
(``mysql_collector``, also MySQL's and MariaDB's live builder); SQL Server, Oracle and
PostgreSQL live collections have their own builders.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.agents.collector.mysql_collector import _build_triggers as shared
from src.agents.collector.oracle_collector import _build_triggers as oracle_live
from src.agents.collector.postgres_collector import _build_triggers as postgres_live
from src.agents.collector.sqlserver_collector import _build_triggers as sqlserver_live
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

_ROOT = Path(__file__).resolve().parents[3]
_BUILDERS = [shared, sqlserver_live, oracle_live, postgres_live]
_IDS = ["shared", "sqlserver", "oracle", "postgresql"]


def _row(**kw):
    return {"trigger_name": "tr", "table_name": "orders", "schema_name": "dbo", **kw}


def _offline(engine: str, triggers: list[dict]) -> list:
    parsed = parse_offline_collection(
        {
            "metadata": {"engine": engine, "database_name": "d"},
            "tables": [{"table_name": "orders", "row_count": 1}],
            "columns": [],
            "queries": [],
            "triggers": triggers,
        }
    )
    return shared(parsed["triggers"]) or []


@pytest.mark.parametrize("build", _BUILDERS, ids=_IDS)
@pytest.mark.parametrize("timing", ["INSTEAD_OF", "INSTEAD OF"])
def test_instead_of_in_either_spelling(build, timing):
    (t,) = build([_row(timing=timing, event_type="INSERT")])
    assert t.timing.value == "INSTEAD OF"


@pytest.mark.parametrize("build", _BUILDERS, ids=_IDS)
@pytest.mark.parametrize("events", ["INSERT OR UPDATE", "INSERT,UPDATE", "UPDATE, INSERT"])
def test_every_event_is_kept(build, events):
    (t,) = build([_row(timing="AFTER", event_type=events)])
    assert [e.value for e in t.events] == ["INSERT", "UPDATE"]
    assert t.event_type.value == "INSERT"


@pytest.mark.parametrize("build", _BUILDERS, ids=_IDS)
def test_an_events_list_beside_a_first_event_is_read(build):
    """The SQL Server and Oracle scripts write ``event_type`` (the first) and ``events``."""
    (t,) = build([_row(timing="BEFORE EACH ROW", event_type="UPDATE", events="UPDATE,DELETE")])
    assert [e.value for e in t.events] == ["UPDATE", "DELETE"]
    assert t.timing.value == "BEFORE"


@pytest.mark.parametrize("build", [sqlserver_live, oracle_live, postgres_live], ids=_IDS[1:])
@pytest.mark.parametrize("event", ["LOGON", "OTHER", "MULTIPLE", "TRUNCATE"])
def test_a_live_trigger_with_no_dml_event_is_skipped(build, event, caplog):
    rows = [
        _row(timing="AFTER", event_type=event),
        _row(timing="AFTER", event_type="DELETE", trigger_name="tr_delete"),
    ]
    with caplog.at_level(logging.WARNING):
        triggers = build(rows)
    assert [[e.value for e in t.events] for t in triggers] == [["DELETE"]]
    assert "Skipped 1 non-DML or DB-level trigger(s)" in caplog.text


@pytest.mark.parametrize("build", [sqlserver_live, oracle_live, postgres_live], ids=_IDS[1:])
def test_a_live_trigger_with_unknown_timing_or_no_table_is_skipped(build):
    rows = [_row(timing="ON COMMIT", event_type="INSERT"), _row(timing="AFTER", table_name="")]
    rows[1]["event_type"] = "INSERT"
    assert build(rows) is None


@pytest.mark.parametrize("build", _BUILDERS, ids=_IDS)
def test_the_skip_warning_names_each_skipped_trigger_and_the_reasons(build, caplog):
    rows = [
        _row(timing="AFTER", event_type="LOGON", trigger_name="tr_logon"),
        _row(timing="ON COMMIT", event_type="INSERT", trigger_name="tr_timing"),
        {"trigger_name": "tr_db", "timing": "AFTER", "event_type": "INSERT"},
    ]
    with caplog.at_level(logging.WARNING):
        assert build(rows) is None
    assert "Skipped 3 non-DML or DB-level trigger(s)" in caplog.text
    assert "unknown timing" in caplog.text
    assert "dbo.orders.tr_logon, dbo.orders.tr_timing, tr_db" in caplog.text


def test_the_skip_warning_names_twenty_and_counts_the_rest(caplog):
    rows = [_row(timing="AFTER", event_type="LOGON", trigger_name=f"t{i:02}") for i in range(23)]
    with caplog.at_level(logging.WARNING):
        shared(rows)
    assert "dbo.orders.t19 and 3 more" in caplog.text
    assert "t20" not in caplog.text


@pytest.mark.passes_on_main
def test_non_dml_trigger_is_still_skipped_offline():
    assert shared([_row(timing="AFTER", event_type="LOGON")]) is None


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("build", "trigger_id", "table_id"),
    [
        (shared, "tr", "orders"),
        (sqlserver_live, "dbo.tr", "dbo.orders"),
        (oracle_live, "dbo.tr", "dbo.orders"),
        (postgres_live, "tr", "orders"),
    ],
    ids=_IDS,
)
def test_ids_keep_their_format(build, trigger_id, table_id):
    (t,) = build([_row(timing="AFTER", event_type="INSERT")])
    assert (t.trigger_id, t.table_id) == (trigger_id, table_id)


def test_postgresql_offline_rows_per_event_become_one_trigger():
    rows = [_row(timing="BEFORE", event_type=e) for e in ("UPDATE", "INSERT")]
    rows.append({**_row(timing="AFTER", event_type="DELETE"), "table_name": "items"})
    rows.append({**_row(timing="AFTER", event_type="DELETE"), "schema_name": "other"})
    triggers = _offline("postgresql", rows)
    assert [(t.table_id, t.timing.value, [e.value for e in t.events]) for t in triggers] == [
        ("orders", "BEFORE", ["INSERT", "UPDATE"]),
        ("items", "AFTER", ["DELETE"]),
        ("orders", "AFTER", ["DELETE"]),
    ]


def test_postgresql_live_rows_per_event_become_one_trigger():
    rows = [_row(timing="BEFORE", event_type=e) for e in ("INSERT", "UPDATE")]
    rows.append({**_row(timing="AFTER", event_type="DELETE"), "table_name": "items"})
    triggers = postgres_live(rows)
    assert [(t.table_id, [e.value for e in t.events]) for t in triggers] == [
        ("orders", ["INSERT", "UPDATE"]),
        ("items", ["DELETE"]),
    ]


def test_sqlserver_offline_instead_of_trigger_is_kept_with_its_events():
    row = _row(timing="INSTEAD_OF", event_type="INSERT", events="INSERT,DELETE")
    (t,) = _offline("sqlserver", [row])
    assert (t.timing.value, [e.value for e in t.events]) == ("INSTEAD OF", ["INSERT", "DELETE"])


@pytest.mark.passes_on_main
def test_the_offline_builder_keeps_definition_and_sql_mode():
    row = _row(timing="AFTER", event_type="UPDATE", definition="BEGIN END", sql_mode="ANSI")
    (t,) = shared([row])
    assert (t.definition, t.sql_mode) == ("BEGIN END", "ANSI")


# --- the live tools and the scripts write the contract spelling and every event --------


def test_the_sqlserver_live_tool_writes_instead_of_and_every_event():
    sent: list[str] = []
    tool = SQLServerRemoteCollector.__new__(SQLServerRemoteCollector)
    tool._query = lambda sql: sent.append(sql) or []  # type: ignore[method-assign]
    tool.collect_triggers()
    (sql,) = sent
    assert "'INSTEAD OF'" in sql and "INSTEAD_OF" not in sql
    assert "sys.trigger_events" in sql and "AS events" in sql


def test_the_sqlserver_script_writes_instead_of_and_every_event():
    script = (_ROOT / "scripts/collect-sqlserver.sql").read_text()
    section = script.split("-- ===================== 10. Triggers", 1)[1].split("-- =====", 1)[0]
    assert "'INSTEAD OF'" in section and "INSTEAD_OF" not in section
    assert "sys.trigger_events" in section and "AS events" in section


def _sqlserver_trigger_sql() -> dict[str, str]:
    sent: list[str] = []

    def query(sql: str) -> list[dict]:
        sent.append(sql)
        return []

    tool = SQLServerRemoteCollector.__new__(SQLServerRemoteCollector)
    tool._query = query  # type: ignore[method-assign]
    tool.collect_triggers()
    script = (_ROOT / "scripts/collect-sqlserver.sql").read_text()
    section = script.split("-- ===================== 10. Triggers", 1)[1].split("-- =====", 1)[0]
    return {"live": sent[0], "script": section}


@pytest.mark.parametrize("where", ["live", "script"])
def test_sqlserver_reads_triggers_on_views_too(where):
    """An INSTEAD OF trigger is usually on a view: the parent join takes tables and views."""
    sql = " ".join(_sqlserver_trigger_sql()[where].split())
    assert "INNER JOIN sys.objects t ON tr.parent_id = t.object_id AND t.type IN ('U', 'V')" in sql
    assert "JOIN sys.tables" not in sql


def test_the_oracle_live_tool_writes_instead_of():
    tool = OracleRemoteCollector.__new__(OracleRemoteCollector)
    tool._query = lambda sql: [  # type: ignore[method-assign]
        {"trigger_name": "T", "table_name": "V", "schema_name": "APP",
         "timing": "INSTEAD OF", "event_type": "INSERT OR UPDATE"},  # fmt: skip
    ]
    (row,) = tool.collect_triggers(owner="APP")
    assert row["timing"] == "INSTEAD OF"
    (t,) = oracle_live([row])
    assert [e.value for e in t.events] == ["INSERT", "UPDATE"]


def test_the_oracle_script_writes_every_event():
    script = (_ROOT / "scripts/collect-oracle.sql").read_text()
    section = script.split("-- 10. Triggers", 1)[1].split("-- 11.", 1)[0]
    assert "'events' VALUE TRIGGERING_EVENT" in section
