# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MySQL events in the routine proof (#1971): the typed input, the one generated run of
an event's body, and how that run is recorded (a call, so the later comparison reads its
table changes as data). The scheduler stays off: nothing here waits for an event to fire."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.tools.database import disposable_engine as de
from src.tools.routines import proof_inputs as pi
from src.tools.routines.proof_inputs import EventSpec, RoutineProofInput, build_proof_inputs

TABLES = [
    {"table_id": "src.orders", "table_name": "orders", "row_count": 5, "primary_key": ["id"],
     "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]},
    {"table_id": "src.archive", "table_name": "archive", "row_count": 5, "primary_key": ["id"],
     "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]},
]  # fmt: skip


def _event(name="purge", status="ENABLED", tables=("orders", "archive"), **spec):
    spec = {"schedule": "RECURRING", "interval_value": 1, "interval_field": "HOUR", **spec}
    return RoutineProofInput(routine_id=f"src.{name}", kind="event", name=name,
                             event=EventSpec(status=status, **spec), tables=tables)  # fmt: skip


def test_an_event_spec_is_normalised_and_read_as_enabled_or_not():
    spec = EventSpec(schedule="recurring", interval_value=2, interval_field="day")
    assert (spec.schedule, spec.interval_field, spec.status, spec.enabled) == (
        "RECURRING", "DAY", "ENABLED", True)  # fmt: skip
    assert EventSpec(schedule="one time", status="slaveside_disabled").enabled is False
    assert EventSpec(schedule="ONE TIME", status="DISABLED").enabled is False


@pytest.mark.parametrize(
    "bad",
    [
        {"schedule": "RECURRING"},  # a recurring event has an interval
        {"schedule": "RECURRING", "interval_value": 0, "interval_field": "HOUR"},
        {"schedule": "RECURRING", "interval_value": 1, "interval_field": "FORTNIGHT"},
        {"schedule": "ONE TIME", "interval_value": 1, "interval_field": "HOUR"},
        {"schedule": "SOMETIMES"},
        {"schedule": "ONE TIME", "status": "PAUSED"},
        {"schedule": "ONE TIME", "extra": 1},
    ],
)
def test_an_event_spec_refuses_what_no_schedule_can_be(bad):
    with pytest.raises(ValidationError):
        EventSpec(**bad)


def test_an_event_has_its_spec_and_only_an_event_does():
    with pytest.raises(ValidationError, match="event"):
        RoutineProofInput(routine_id="a", kind="event", name="a")
    with pytest.raises(ValidationError, match="event"):
        RoutineProofInput(routine_id="a", kind="procedure", name="a",
                          event=EventSpec(schedule="ONE TIME"))  # fmt: skip
    with pytest.raises(ValidationError):
        RoutineProofInput(routine_id="a", kind="event", name="a",
                          event=EventSpec(schedule="ONE TIME"),
                          trigger=pi.TriggerSpec(table="t", timing="AFTER", events=("INSERT",)))  # fmt: skip


def test_an_event_gets_one_run_of_its_body_recorded_as_a_call():
    inputs = build_proof_inputs([_event()], TABLES, dialect="mysql", seed=1)
    assert inputs.skipped == ()
    assert inputs.calls == (
        pi.ProofCall("src.purge", "run", "call", "purge", "event"),
    )  # one run, no arguments, no SQL text
    assert set(inputs.setup.tables) == {"src.orders", "src.archive"}  # the tables it touches
    assert inputs.setup.missing == ()


def test_a_disabled_event_is_run_too_and_its_status_stays_out_of_the_call():
    enabled = build_proof_inputs([_event()], TABLES, dialect="mysql")
    for status in ("DISABLED", "SLAVESIDE_DISABLED"):
        off = build_proof_inputs([_event(status=status)], TABLES, dialect="mysql")
        assert off.calls == enabled.calls


def test_an_event_runs_after_the_row_changes_and_beside_other_calls():
    proc = RoutineProofInput(routine_id="src.a_proc", kind="procedure", name="a_proc")
    inputs = build_proof_inputs([_event(), proc], TABLES, dialect="mysql")
    assert [(c.routine_id, c.kind) for c in inputs.calls] == [
        ("src.a_proc", "procedure"), ("src.purge", "event")]  # fmt: skip


def test_the_same_event_gives_the_same_bytes():
    a = build_proof_inputs([_event()], TABLES, dialect="mysql", seed=4).to_json()
    assert a == build_proof_inputs([_event()], TABLES, dialect="mysql", seed=4).to_json()


def test_an_event_is_skipped_on_postgresql_with_its_reason():
    inputs = build_proof_inputs([_event()], TABLES, dialect="postgresql")
    assert inputs.calls == ()
    assert [(s.routine_id, "MySQL" in s.reason) for s in inputs.skipped] == [("src.purge", True)]


def test_an_event_with_an_unsafe_name_is_not_run_as_sql():
    handle = SimpleNamespace(engine="mysql", stopped=False)
    for bad in ("x`; DROP TABLE orders; --", "a b", ""):
        call = pi.ProofCall("e", "run", "call", bad, "event")
        with pytest.raises(ValueError):
            pi.run_call(handle, call)  # type: ignore[arg-type]


def test_run_call_hands_an_event_to_the_engine_once_with_no_arguments(monkeypatch):
    seen = []
    monkeypatch.setattr(
        de, "call", lambda h, routine, args, modes, casts: seen.append((routine, args, modes))
        or de.CallResult())  # fmt: skip
    handle = SimpleNamespace(engine="mysql", stopped=False)
    pi.run_call(handle, pi.ProofCall("e", "run", "call", "purge", "event"))  # type: ignore[arg-type]
    assert seen == [(de.SourceRoutine("purge", "event", ""), [], [])]


def test_run_call_refuses_an_event_on_postgresql_and_one_with_arguments():
    pg = SimpleNamespace(engine="postgresql", stopped=False)
    with pytest.raises(ValueError, match="MySQL"):
        pi.run_call(pg, pi.ProofCall("e", "run", "call", "purge", "event"))  # type: ignore[arg-type]
    my = SimpleNamespace(engine="mysql", stopped=False)
    with pytest.raises(ValueError, match="argument"):
        pi.run_call(my, pi.ProofCall("e", "run", "call", "purge", "event", (1,), ("IN",)))  # type: ignore[arg-type]


def test_the_engine_names_each_events_runner_the_same_way_and_within_the_name_limit():
    a, b = de.event_runner_name("purge"), de.event_runner_name("src.purge")
    assert a == b == de.event_runner_name("purge")  # the schema part is not part of the name
    assert de.event_runner_name("other") != a
    assert len(de.event_runner_name("x" * 64)) <= 64
    assert de._IDENT.match(a)
    with pytest.raises(ValueError):
        de.event_runner_name("bad name")


@pytest.mark.passes_on_main  # the engine already starts with the scheduler off (#1942)
def test_the_disposable_mysql_server_keeps_the_event_scheduler_off():
    import inspect

    assert "--event-scheduler=OFF" in inspect.getsource(de)
