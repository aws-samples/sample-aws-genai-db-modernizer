# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MySQL events in the routine proof (#1971): the typed input, the one generated run of
an event's body, and how that run is recorded (a call, so the later comparison reads its
table changes as data). The scheduler stays off: nothing here waits for an event to fire.

Only modules are imported and inputs are plain dicts, so this file imports on main and the
generation and ``run_call`` tests fail there on behaviour, not on a missing name."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.tools.database import disposable_engine as de
from src.tools.routines import proof_inputs as pi

TABLES = [
    {"table_id": "src.orders", "table_name": "orders", "row_count": 5, "primary_key": ["id"],
     "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]},
    {"table_id": "src.archive", "table_name": "archive", "row_count": 5, "primary_key": ["id"],
     "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]},
]  # fmt: skip
HOURLY = {"schedule": "RECURRING", "interval_value": 1, "interval_field": "HOUR"}


def _event(name="purge", status="ENABLED", tables=("orders", "archive")):
    return pi.RoutineProofInput.model_validate(
        {"routine_id": f"src.{name}", "kind": "event", "name": name,
         "event": {**HOURLY, "status": status}, "tables": list(tables)})  # fmt: skip


def _event_call(name="purge", **kw):
    return pi.ProofCall("e", "run", "call", name, "event", **kw)


def _my_handle(engine="mysql"):
    return SimpleNamespace(engine=engine, stopped=False)


def _no_engine(monkeypatch):
    """A ``de.call`` that must never be reached."""
    seen: list = []
    monkeypatch.setattr(de, "call", lambda *a, **k: seen.append(a) or de.CallResult())
    return seen


# ------------------------------------------------------------------ the typed input


def test_an_event_spec_is_normalised_and_read_as_enabled_or_not():
    spec = pi.EventSpec.model_validate(
        {"schedule": "recurring", "interval_value": 2, "interval_field": "day"}
    )
    assert (spec.schedule, spec.interval_field, spec.status, spec.enabled) == (
        "RECURRING", "DAY", "ENABLED", True)  # fmt: skip
    assert (
        pi.EventSpec.model_validate({"schedule": "ONE TIME", "status": "DISABLED"}).enabled is False
    )


@pytest.mark.parametrize(
    "old", ["SLAVESIDE_DISABLED", "slaveside_disabled", "REPLICA_SIDE_DISABLED"]
)
def test_both_spellings_of_the_replica_side_status_are_one_status(old):
    spec = pi.EventSpec.model_validate({"schedule": "ONE TIME", "status": old})
    assert (spec.status, spec.enabled) == ("REPLICA_SIDE_DISABLED", False)


@pytest.mark.parametrize(
    "bad",
    [
        {"schedule": "RECURRING"},  # a recurring event has an interval
        {"schedule": "RECURRING", "interval_value": 1},  # an interval has both parts
        {"schedule": "RECURRING", "interval_value": 0, "interval_field": "HOUR"},
        {"schedule": "RECURRING", "interval_value": 1, "interval_field": "FORTNIGHT"},
        {"schedule": "ONE TIME", "interval_value": 1, "interval_field": "HOUR"},
        {"schedule": "ONE TIME", "interval_value": 1},  # half an interval on a one-time event
        {"schedule": "ONE TIME", "interval_field": "HOUR"},
        {"schedule": "SOMETIMES"},
        {"schedule": "ONE TIME", "status": "PAUSED"},
        {"schedule": "ONE TIME", "extra": 1},
    ],
)
def test_an_event_spec_refuses_what_no_schedule_can_be(bad):
    with pytest.raises(ValidationError):
        pi.EventSpec.model_validate(bad)


def test_an_event_has_its_spec_and_only_an_event_does():
    base = {"routine_id": "a", "name": "a"}
    with pytest.raises(ValidationError, match="event"):
        pi.RoutineProofInput.model_validate({**base, "kind": "event"})
    with pytest.raises(ValidationError, match="event"):
        pi.RoutineProofInput.model_validate(
            {**base, "kind": "procedure", "event": {"schedule": "ONE TIME"}}
        )
    with pytest.raises(ValidationError):
        pi.RoutineProofInput.model_validate(
            {**base, "kind": "event", "event": {"schedule": "ONE TIME"},
             "trigger": {"table": "t", "timing": "AFTER", "events": ["INSERT"]}})  # fmt: skip


# ------------------------------------------------------------------ generation


def test_an_event_gets_one_run_of_its_body_recorded_as_a_call():
    inputs = pi.build_proof_inputs([_event()], TABLES, dialect="mysql", seed=1)
    assert inputs.skipped == ()
    assert inputs.calls == (pi.ProofCall("src.purge", "run", "call", "purge", "event"),)
    assert set(inputs.setup.tables) == {"src.orders", "src.archive"}  # the tables it touches
    assert inputs.setup.missing == ()


@pytest.mark.parametrize("status", ["DISABLED", "REPLICA_SIDE_DISABLED", "SLAVESIDE_DISABLED"])
def test_a_disabled_event_is_run_too_and_its_status_stays_out_of_the_call(status):
    enabled = pi.build_proof_inputs([_event()], TABLES, dialect="mysql")
    off = pi.build_proof_inputs([_event(status=status)], TABLES, dialect="mysql")
    assert off.calls == enabled.calls != ()


def test_an_event_runs_after_the_row_changes_and_beside_other_calls():
    proc = pi.RoutineProofInput.model_validate(
        {"routine_id": "src.a_proc", "kind": "procedure", "name": "a_proc"}
    )
    inputs = pi.build_proof_inputs([_event(), proc], TABLES, dialect="mysql")
    assert [(c.routine_id, c.kind) for c in inputs.calls] == [
        ("src.a_proc", "procedure"), ("src.purge", "event")]  # fmt: skip


def test_the_same_event_gives_the_same_bytes():
    a = pi.build_proof_inputs([_event()], TABLES, dialect="mysql", seed=4).to_json()
    assert a == pi.build_proof_inputs([_event()], TABLES, dialect="mysql", seed=4).to_json()
    assert '"event"' in a


def test_an_event_is_skipped_on_postgresql_with_its_reason():
    inputs = pi.build_proof_inputs([_event()], TABLES, dialect="postgresql")
    assert inputs.calls == ()
    assert [(s.routine_id, "MySQL" in s.reason) for s in inputs.skipped] == [("src.purge", True)]


@pytest.mark.parametrize("name", ["my-ev", "a b", "x`y"])
def test_an_event_whose_name_is_not_plain_is_skipped_alone(name):
    other = pi.RoutineProofInput.model_validate(
        {"routine_id": "src.p", "kind": "procedure", "name": "p"}
    )
    inputs = pi.build_proof_inputs([_event(name), other], TABLES, dialect="mysql")
    assert [c.routine_id for c in inputs.calls] == ["src.p"]
    assert [(s.routine_id, "plain" in s.reason) for s in inputs.skipped] == [(f"src.{name}", True)]


# ------------------------------------------------------------------ run_call


@pytest.mark.parametrize("bad", ["x`; DROP TABLE orders; --", "a b", "my-ev", ""])
def test_an_event_with_an_unsafe_name_is_not_run(bad, monkeypatch):
    seen = _no_engine(monkeypatch)
    with pytest.raises(ValueError, match="identifier"):
        pi.run_call(_my_handle(), _event_call(bad))  # type: ignore[arg-type]
    assert seen == []


def test_run_call_hands_an_event_to_the_engine_once_with_no_arguments(monkeypatch):
    seen = []
    monkeypatch.setattr(
        de, "call", lambda h, routine, args, modes, casts: seen.append((routine, args, modes))
        or de.CallResult())  # fmt: skip
    pi.run_call(_my_handle(), _event_call())  # type: ignore[arg-type]
    assert seen == [(de.SourceRoutine("purge", "event", ""), [], [])]


def test_run_call_refuses_an_event_on_postgresql(monkeypatch):
    seen = _no_engine(monkeypatch)
    with pytest.raises(ValueError, match="MySQL"):
        pi.run_call(_my_handle("postgresql"), _event_call())  # type: ignore[arg-type]
    assert seen == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"args": (1,)}, "argument$"),
        ({"modes": ("IN",)}, "argument mode"),
        ({"casts": ("integer",)}, "argument cast"),
    ],
)
def test_run_call_refuses_each_argument_part_of_an_event_on_its_own(extra, message, monkeypatch):
    seen = _no_engine(monkeypatch)
    with pytest.raises(ValueError, match=message):
        pi.run_call(_my_handle(), _event_call(**extra))  # type: ignore[arg-type]
    assert seen == []


# ------------------------------------------------------------------ the runner's name


def test_the_engine_names_each_events_runner_the_same_way_and_within_the_name_limit():
    a, b = de.event_runner_name("purge"), de.event_runner_name("src.purge")
    assert a == b == de.event_runner_name("purge")  # the schema part is not part of the name
    assert de.event_runner_name("other") != a
    assert len(de.event_runner_name("x" * 64)) <= 64
    assert de._IDENT.match(a)
    with pytest.raises(ValueError):
        de.event_runner_name("bad name")


def test_an_events_runner_name_ignores_case_as_mysql_does():
    assert de.event_runner_name("NightJob") == de.event_runner_name("nightjob")
