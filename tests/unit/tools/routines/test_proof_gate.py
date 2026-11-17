# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's deploy gate on the routine test (#2411, the maintainer decision of 2026-11-17 on
#1925): ``routines-deploy.sql`` and the triggers-enable script carry a routine only when the
latest test on a test Aurora, on the job's routines file as it is now, found no difference on
the calls made; every other routine is held back with its reason."""

from __future__ import annotations

import pytest

from src.agents.collector.routine_sources import routine_sources_digest, write_routine_sources
from src.tools.data_movement.routine_scripts import DEPLOY_FILE, INSTRUCTIONS_FILE
from src.tools.data_movement.routine_test_gate import held_routines, proof_state
from src.tools.routines.migration import read_routine_migration, routine_migration_path
from tests.unit.tools.data_movement import test_routine_scripts as rs
from tests.unit.tools.data_movement import test_triggers_scripts as ts
from tests.unit.tools.data_movement.routine_fixtures import mark_tested, pg_source, with_test_result

ALL = {"public.audit()", "public.orders.trg", "public.later(integer)"}


def _untested(store) -> None:
    """The job's artifact as the assessment writes it: no test has run on it."""
    artifact = read_routine_migration(store, "shop", "job1", 2)
    assert artifact is not None
    plain = artifact.model_copy(
        update={"proof_run": None,
                "routines": [r.model_copy(update={"proof": None}) for r in artifact.routines]}
    )  # fmt: skip
    store.write_json(routine_migration_path("shop", "job1", 2), plain.model_dump(mode="json"))


def _refused(scripts) -> dict[str, str]:
    return {i: c for _, i, c in scripts.refused}


def test_no_test_holds_every_carried_routine_back_as_not_tested(tmp_path):
    """No test ran (the customer declined it, or never ran /test-routines): none deployed."""
    store, contract = rs._pg_case(tmp_path)
    _untested(store)
    scripts = rs._write(tmp_path, store, contract)
    assert (scripts.status, scripts.in_script, scripts.proof, scripts.held) == (
        "ready", 0, "not_run", 3)  # fmt: skip
    assert _refused(scripts) == dict.fromkeys(ALL, "not_tested")
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "CREATE OR REPLACE FUNCTION" not in text and "CREATE TRIGGER" not in text
    assert "-- Not tested yet: no test on a test Aurora has run on these routines" in text
    assert "with /test-routines" in (tmp_path / "step" / INSTRUCTIONS_FILE).read_text()


@pytest.mark.parametrize("outcome", ["not_proved", "needs_attention", "not_tested"])
def test_only_proved_routines_reach_the_deploy_script_and_the_rest_are_held_with_their_reason(
    tmp_path, outcome
):
    store, contract = rs._pg_case(tmp_path)
    mark_tested(store, {"public.later(integer)": outcome, "public.orders.trg": "needs_attention"})
    scripts = rs._write(tmp_path, store, contract)
    assert (scripts.in_script, scripts.proof, scripts.held) == (1, "current", 2)
    assert _refused(scripts) == {"public.later(integer)": outcome,
                                 "public.orders.trg": "needs_attention"}  # fmt: skip
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "FUNCTION public.audit(" in text
    assert "FUNCTION public.later(" not in text and "CREATE TRIGGER trg" not in text
    assert "Held back: 2" in text


def test_every_proved_routine_is_deployed(tmp_path):
    store, contract = rs._pg_case(tmp_path)  # every carried routine tested, none differs
    scripts = rs._write(tmp_path, store, contract)
    assert (scripts.in_script, scripts.held, scripts.refused) == (3, 0, [])


def test_a_routine_naming_a_held_routine_is_left_out_too(tmp_path):
    store, contract = rs._pg_case(tmp_path)
    mark_tested(store, {"public.audit()": "not_proved"})
    refused = _refused(rs._write(tmp_path, store, contract))
    assert refused == {"public.audit()": "not_proved", "public.orders.trg": "depends_on_held",
                       "public.later(integer)": "depends_on_held"}  # fmt: skip


def test_a_stale_test_counts_as_not_tested(tmp_path):
    """A test on another routines file (a new collection since) is no test of this one."""
    store, contract = rs._pg_case(tmp_path)
    before = routine_sources_digest(store, "shop", "job1")
    write_routine_sources(store, "shop", "job1",
                          {"metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}}},
                          [pg_source("audit_1", rs._PG_FN.format(name="audit")),
                           pg_source("later_2", rs._PG_FN.format(name="later"))])  # fmt: skip
    assert routine_sources_digest(store, "shop", "job1") not in (None, before)
    migration = read_routine_migration(store, "shop", "job1", 2)
    assert migration is not None and proof_state(store, "shop", "job1", migration) == "stale"
    scripts = rs._write(tmp_path, store, contract)
    assert (scripts.in_script, scripts.proof) == (0, "stale")
    assert set(_refused(scripts).values()) <= {"not_tested", "source_missing"}
    assert _refused(scripts)["public.audit()"] == "not_tested"
    assert "is not on these routines as they are" in (tmp_path / "step" / DEPLOY_FILE).read_text()


def test_a_test_written_before_the_routines_file_was_named_counts_as_not_tested(tmp_path):
    store, contract = rs._pg_case(tmp_path)
    artifact = read_routine_migration(store, "shop", "job1", 2)
    assert artifact is not None
    old = with_test_result(
        artifact, None
    )  # a proof_run with no routine_sources_sha256 (before #2411)
    store.write_json(routine_migration_path("shop", "job1", 2), old.model_dump(mode="json"))
    assert held_routines(store, "shop", "job1", old) == {
        ("function", "public.audit()"): "not_tested",
        ("trigger", "public.orders.trg"): "not_tested",
        ("function", "public.later(integer)"): "not_tested",
    }  # public.py_fn() is not carried: the gate holds only carried routines
    assert rs._write(tmp_path, store, contract).in_script == 0


def test_an_old_artifact_with_no_proof_fields_loads_and_holds_every_routine(tmp_path):
    """A job saved before #1945: its artifact has no ``proof`` and no ``proof_run`` keys."""
    store, contract = rs._pg_case(tmp_path)
    key = routine_migration_path("shop", "job1", 2)
    data = store.read_json(key)
    data.pop("proof_run")
    for r in data["routines"]:
        r.pop("proof", None)
    store.write_json(key, data)
    migration = read_routine_migration(store, "shop", "job1", 2)
    assert migration is not None and migration.proof_run is None
    scripts = rs._write(tmp_path, store, contract)
    assert (scripts.in_script, scripts.proof) == (0, "not_run")
    assert _refused(scripts) == dict.fromkeys(ALL, "not_tested")


def test_the_scratch_schema_test_counts_like_the_test_cluster(tmp_path):
    """The gate reads the job's recorded test whatever ran it (#1945's scratch-schema test)."""
    store, contract = rs._pg_case(tmp_path)
    artifact = read_routine_migration(store, "shop", "job1", 2)
    assert artifact is not None
    scratch = with_test_result(artifact, routine_sources_digest(store, "shop", "job1"),
                     target="a job-scoped scratch schema on Aurora PostgreSQL 16")  # fmt: skip
    store.write_json(routine_migration_path("shop", "job1", 2), scratch.model_dump(mode="json"))
    assert rs._write(tmp_path, store, contract).in_script == 3


def test_the_postgresql_triggers_enable_script_enables_only_proved_triggers(tmp_path):
    store, contract = ts._pg_case(tmp_path)
    mark_tested(store, {"public.orders.trg_b": "not_tested"})
    scripts = ts._write_scripts(tmp_path, store, contract)
    assert (scripts.in_script, scripts.held, scripts.proof) == (1, 1, "current")
    assert ("trigger", "public.orders.trg_b", "not_tested") in scripts.excluded
    text = ts._read(tmp_path, ts.TRIGGERS_FILE)
    assert 'ENABLE TRIGGER "trg_a"' in text and 'ENABLE TRIGGER "trg_b"' not in text


def test_the_mysql_triggers_enable_script_creates_only_proved_triggers_and_events(tmp_path):
    store, contract = ts._mysql_case(tmp_path, {"nightly": "ENABLE"})
    mark_tested(store, {"shop.nightly": "needs_attention"})
    scripts = ts._write_scripts(tmp_path, store, contract)
    assert ("event", "shop.nightly", "needs_attention") in scripts.excluded
    assert scripts.held == 1
    text = ts._read(tmp_path, ts.TRIGGERS_FILE)
    assert "TRIGGER `orders_bi`" in text and "EVENT `nightly`" not in text
    _untested(store)
    scripts = ts._write_scripts(tmp_path, store, contract)
    assert scripts.in_script == 0 and scripts.proof == "not_run"


def test_a_trigger_whose_function_is_held_is_left_out_of_the_postgresql_triggers_enable_script(
    tmp_path,
):
    store, contract = ts._pg_case(tmp_path)
    mark_tested(store, {"public.audit()": "needs_attention"})
    scripts = ts._write_scripts(tmp_path, store, contract)
    assert scripts.in_script == 0 and scripts.held == 2
    assert ("trigger", "public.orders.trg_a", "depends_on_held") in scripts.excluded
    assert ("trigger", "public.orders.trg_b", "depends_on_held") in scripts.excluded
    assert "ENABLE TRIGGER" not in ts._read(tmp_path, ts.TRIGGERS_FILE).split("BEGIN;", 1)[1]


def test_a_trigger_whose_procedure_is_held_is_left_out_of_the_mysql_triggers_enable_script(
    tmp_path,
):
    store, contract = ts._mysql_case(tmp_path)
    mark_tested(store, {"shop.place_order": "not_proved"})
    scripts = ts._write_scripts(tmp_path, store, contract)
    assert ("trigger", "shop.orders_bi", "depends_on_held") in scripts.excluded
    assert scripts.in_script == 0
    assert "TRIGGER `orders_bi`" not in ts._read(tmp_path, ts.TRIGGERS_FILE)


def test_the_gate_and_the_facts_count_the_same_held_routines(tmp_path):
    """Review of #2420: 2 routines with no difference that call the one that needs
    attention. The deploy script holds all 3, and the line says so."""
    from src.shared.routine_facts import read_routine_facts

    store, contract = rs._pg_case(tmp_path)
    mark_tested(store, {"public.audit()": "needs_attention"})
    scripts = rs._write(tmp_path, store, contract)
    assert scripts.in_script == 0 and scripts.held == 3
    line = read_routine_facts(store, "shop", "job1", 2)["line"]
    assert line.endswith(
        "On a test Aurora, of 3 carried routines: 2 showed no difference on the calls made; "
        "1 needs attention. Step 1 deploys none of them: they are held back, 2 because a "
        "routine they call is held back."
    )
    assert "deploys only" not in line
