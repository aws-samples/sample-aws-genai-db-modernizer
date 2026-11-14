# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A MySQL event's test run on a real server (#1971).

Needs ``mysqld`` on PATH: the disposable engine starts a throwaway server (no binary log,
``--event-scheduler=OFF``), loads the generated setup and the events' ``CREATE EVENT``
text, and runs the generated call once. Run it with ``uv run pytest -m integration
tests/integration/test_routine_events_mysql.py``.
"""

from __future__ import annotations

import os
import time

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.proof_inputs import (
    EventSpec,
    RoutineProofInput,
    build_proof_inputs,
    load_setup,
    run_call,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary"),
    pytest.mark.skipif(os.geteuid() == 0, reason="the disposable engine refuses to run as root"),
]


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


TABLES = [
    {"table_id": "src.orders", "table_name": "orders", "row_count": 4, "primary_key": ["id"],
     "columns": [_col("id", "int")]},
    {"table_id": "src.order_log", "table_name": "order_log", "row_count": 0,
     "primary_key": ["id"], "columns": [_col("id", "int"), _col("note", "varchar(20)", True)]},
]  # fmt: skip
SOURCES = [
    de.SourceRoutine("log_orders", "event",
        "CREATE EVENT log_orders ON SCHEDULE EVERY 1 HOUR DO "
        "INSERT INTO order_log (id, note) SELECT COALESCE(MAX(id), 0) + 1, 'ran' FROM order_log"),
    de.SourceRoutine("compound", "event",
        "CREATE EVENT compound ON SCHEDULE EVERY 1 DAY DISABLE DO BEGIN "
        "DECLARE n INT; SELECT COUNT(*) INTO n FROM orders; "
        "INSERT INTO order_log (id, note) VALUES (100 + n, 'compound'); END"),
    de.SourceRoutine("soon", "event",
        "CREATE EVENT soon ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 SECOND "
        "ON COMPLETION PRESERVE DO INSERT INTO order_log (id, note) VALUES (500, 'fired')"),
    de.SourceRoutine("broken", "event",
        "CREATE EVENT broken ON SCHEDULE EVERY 1 HOUR DO "
        "INSERT INTO order_log (id, note) VALUES (1, REPEAT('x', 50))"),
]  # fmt: skip
EVENTS = [
    RoutineProofInput(routine_id="src.log_orders", kind="event", name="log_orders",
                      event=EventSpec(schedule="RECURRING", interval_value=1,
                                      interval_field="HOUR"), tables=("order_log",)),
    RoutineProofInput(routine_id="src.compound", kind="event", name="compound",
                      event=EventSpec(schedule="RECURRING", interval_value=1,
                                      interval_field="DAY", status="DISABLED"),
                      tables=("orders", "order_log")),
    RoutineProofInput(routine_id="src.soon", kind="event", name="soon",
                      event=EventSpec(schedule="ONE TIME"), tables=("order_log",)),
    RoutineProofInput(routine_id="src.broken", kind="event", name="broken",
                      event=EventSpec(schedule="RECURRING", interval_value=1,
                                      interval_field="HOUR"), tables=("order_log",)),
]  # fmt: skip


def _log(h):
    return {r["note"] for r in de.snapshot(h, ["order_log"])["order_log"]}


def test_an_event_runs_once_on_demand_while_the_scheduler_stays_off():
    inputs = build_proof_inputs(EVENTS, TABLES, dialect="mysql", seed=2)
    assert inputs.skipped == ()
    calls = {c.routine_id: c for c in inputs.calls}
    assert {c.kind for c in calls.values()} == {"event"}
    with de.running("mysql", de.available_majors("mysql")[0]) as h:
        load_setup(h, inputs, SOURCES)
        assert de.execute(h, "SELECT @@event_scheduler").rows == [["OFF"]]
        seeded = _log(h)
        time.sleep(2.5)  # the one-time event's moment has passed; nothing fired it
        assert _log(h) == seeded
        ev = de.execute(h, "SELECT EVENT_NAME, STATUS FROM information_schema.EVENTS "
                        "ORDER BY EVENT_NAME").rows  # fmt: skip
        assert ev == [["broken", "ENABLED"], ["compound", "DISABLED"],
                      ["log_orders", "ENABLED"], ["soon", "ENABLED"]]  # fmt: skip

        first = run_call(h, calls["src.log_orders"])
        assert first.error is None
        assert _log(h) - seeded == {"ran"}  # its table change is data the comparison reads
        rows_once = de.snapshot(h, ["order_log"])["order_log"]
        run_call(h, calls["src.log_orders"])  # on demand: each run is one more run
        assert len(de.snapshot(h, ["order_log"])["order_log"]) == len(rows_once) + 1

        # a disabled event with a compound body runs too
        assert run_call(h, calls["src.compound"]).error is None
        assert "compound" in _log(h)
        assert run_call(h, calls["src.soon"]).error is None
        assert "fired" in _log(h)

        # an error in the body is the run's outcome, not an exception
        bad = run_call(h, calls["src.broken"])
        assert bad.error is not None and bad.errno is not None
        assert de.execute(h, "SELECT @@event_scheduler").rows == [["OFF"]]
        names = de.execute(h, "SELECT COUNT(*) FROM information_schema.EVENTS").rows
        assert names == [[4]]  # the runs left the events as they were
