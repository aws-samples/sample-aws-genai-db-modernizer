# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PostgreSQL trigger's collected enable mode reaches the target exactly (#2099).

A ``pgserver`` PostgreSQL (a Unix socket in a temp folder, no TCP listener) first plays the
source: one trigger in each ``tgenabled`` mode (O, A, R, D), read by the live collector
(#2071). The routines are then dropped and the same server plays the Aurora target: step
1's deploy script creates every trigger disabled, then ``psql -X -f cutover-routines.sql``
and ``psql -X -f cutover-check.sql -o cutover-check.tsv`` run as ``cutover.md`` says. With
the letters collected, each trigger ends in its own mode and the files name no hand check;
with a collection that predates the letter, the cutover keeps the enabled / disabled
fallback and names the hand check. ``pgserver`` is a dev dependency: this module fails,
never skips, when it is missing or cannot start.
"""

from __future__ import annotations

import pgserver  # noqa: F401 -- imported first: a missing pgserver fails, never skips
import pytest

from src.agents.collector.postgres_collector import _build_triggers
from src.tools.data_movement.cutover_check import read_cutover_check
from src.tools.data_movement.cutover_scripts import (
    CUTOVER_CHECK_FILE,
    CUTOVER_CHECK_OUTPUT,
    CUTOVER_FILE,
    CUTOVER_INSTRUCTIONS,
    SOURCE_ALWAYS_REPLICA_QUERY,
    write_cutover_scripts,
)
from src.tools.data_movement.routine_scripts import DEPLOY_FILE
from src.tools.data_movement.step1_routines import attach_step_routines
from tests.unit.tools.data_movement import test_routine_scripts_engine as twin
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract
from tests.unit.tools.data_movement.routine_fixtures import (
    dep_routine,
    migration,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job, _write
from tests.unit.tools.database.test_postgres_trigger_enable_mode_live_server import _live

_definitions, _psql = twin._definitions, twin._psql
# The source's mode for each trigger, and the ALTER clause that sets it there.
_MODES = {"trg": ("O", "ENABLE"), "trg_a": ("A", "ENABLE ALWAYS"),
          "trg_r": ("R", "ENABLE REPLICA"), "trg_d": ("D", "DISABLE")}  # fmt: skip
_TABLE = "CREATE TABLE public.orders (id int PRIMARY KEY, status text);"
_ROUTINES = """CREATE FUNCTION public.audit() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN NEW.status := coalesce(NEW.status, 'new'); RETURN NEW; END $$;
CREATE TRIGGER trg BEFORE INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION public.audit();
"""
_DROP = "DROP FUNCTION public.audit() CASCADE;"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    srv.psql("\\set ON_ERROR_STOP on\n" + _TABLE)
    yield srv
    srv.cleanup()


def _tgenabled(server) -> dict[str, str]:
    out = server.psql("\\set QUIET on\n\\pset format unaligned\n\\t on\nSELECT tgname || ':' || "
                      "tgenabled::text FROM pg_trigger WHERE tgrelid = 'public.orders'::regclass "
                      "AND NOT tgisinternal ORDER BY 1;")  # fmt: skip
    return dict(line.split(":") for line in out.split())


def _source_then_target(server, tmp_path, *, keep_letter: bool):
    """Collect the source's triggers, empty it, and give the job's store and contract."""
    server.psql("\\set ON_ERROR_STOP on\n" + _ROUTINES)
    defs = _definitions(server)
    for name, (_, clause) in _MODES.items():
        if name != "trg":
            server.psql(defs["trg:"].replace("TRIGGER trg ", f"TRIGGER {name} ") + ";")
        server.psql(f"ALTER TABLE public.orders {clause} TRIGGER {name};")
    assert _tgenabled(server) == {n: m for n, (m, _) in _MODES.items()}
    collected = [t.model_dump(mode="json")
                 for t in _build_triggers(_live(server).collect_triggers()) or []]  # fmt: skip
    if not keep_letter:  # a collection written before #2071 (collector_output 5.16)
        collected = [{k: v for k, v in t.items() if k != "enabled_mode"} for t in collected]
    server.psql(_DROP)  # the target starts empty
    rows = [row("public.audit()", specific="audit_3", signature="")]
    sources = [pg_source("audit_3", defs["audit:"])]
    for name in _MODES:
        rows.append(row(f"public.orders.{name}", "trigger", name=name, table="public.orders",
                        specific=f"orders.{name}",
                        dependencies=[dep_routine("function", "public.audit()")]))  # fmt: skip
        text = defs["trg:"].replace("TRIGGER trg ", f"TRIGGER {name} ")
        sources.append(pg_source(f"orders.{name}", text, kind="trigger"))
    store = _job(tmp_path, migration(rows), sources)
    version = "5.17" if keep_letter else "5.16"
    store.write_json("shop/job1/collector/output.json",
                     {"contract_version": version, "database_schema": {"triggers": collected}})  # fmt: skip
    contract = attach_step_routines(store, "shop", "job1", make_contract("aurora_postgresql"))
    return store, contract


def _deploy_cutover_check(server, tmp_path, store, contract):
    assert _write(tmp_path, store, contract).refused == []
    step = tmp_path / "step"
    deployed = _psql(server, "-f", str(step / DEPLOY_FILE))
    assert deployed.returncode == 0, deployed.stderr
    assert set(_tgenabled(server).values()) == {"D"}  # step 1 created every one disabled
    scripts = write_cutover_scripts(store, "shop", "job1", contract, SHA, step)
    ran = _psql(server, "-f", str(step / CUTOVER_FILE))
    assert ran.returncode == 0, ran.stderr
    checked = _psql(
        server, "-f", str(step / CUTOVER_CHECK_FILE), "-o", str(step / CUTOVER_CHECK_OUTPUT)
    )
    assert checked.returncode == 0, checked.stderr
    check = read_cutover_check(step / CUTOVER_CHECK_OUTPUT, contract, store, job_id="job1",
                               database_name="shop", contract_sha=SHA, target_database=None)  # fmt: skip
    files = {n: (step / n).read_text() for n in (CUTOVER_FILE, CUTOVER_INSTRUCTIONS)}
    return scripts, check, files


def test_collected_always_and_replica_triggers_end_in_their_own_mode(server, tmp_path):
    store, contract = _source_then_target(server, tmp_path, keep_letter=True)
    states = {r.name: r.source_state for r in contract.routines.routines if r.kind == "trigger"}
    assert states == {n: m for n, (m, _) in _MODES.items()}
    try:
        scripts, check, files = _deploy_cutover_check(server, tmp_path, store, contract)
        assert _tgenabled(server) == {n: m for n, (m, _) in _MODES.items()}  # A and R exactly
        assert (check.status, check.confirmed, check.mismatches) == ("pass", 4, [])
        assert (scripts.in_script, scripts.stays_disabled, scripts.state_folded) == (4, 1, 0)
        sql = files[CUTOVER_FILE]
        assert 'ENABLE ALWAYS TRIGGER "trg_a";' in sql and 'ENABLE REPLICA TRIGGER "trg_r";' in sql
        assert "(source state A)" in sql and "(source state R)" in sql
        assert "created disabled to their source state;" in sql
        # Every letter is known: no file claims a hand check is needed.
        for text in files.values():
            assert SOURCE_ALWAYS_REPLICA_QUERY not in text and "check those by hand" not in text
        # The check reads the exact mode back: an A trigger set to O is drift.
        server.psql("ALTER TABLE public.orders ENABLE TRIGGER trg_a;")
        step = tmp_path / "step"
        assert _psql(server, "-f", str(step / CUTOVER_CHECK_FILE), "-o",
                     str(step / CUTOVER_CHECK_OUTPUT)).returncode == 0  # fmt: skip
        drift = read_cutover_check(step / CUTOVER_CHECK_OUTPUT, contract, store, job_id="job1",
                                   database_name="shop", contract_sha=SHA, target_database=None)  # fmt: skip
        assert {m.routine_id: m.problem for m in drift.mismatches} == {
            "public.orders.trg_a": "trigger_state_differs"}  # fmt: skip
    finally:
        server.psql(_DROP)


@pytest.mark.passes_on_main  # guards the fallback main already has
def test_a_collection_without_the_letter_keeps_the_fallback_and_the_hand_check(server, tmp_path):
    store, contract = _source_then_target(server, tmp_path, keep_letter=False)
    states = {r.name: r.source_state for r in contract.routines.routines if r.kind == "trigger"}
    assert states == {"trg": "enabled", "trg_a": "enabled", "trg_r": "disabled",
                      "trg_d": "disabled"}  # fmt: skip
    try:
        scripts, check, files = _deploy_cutover_check(server, tmp_path, store, contract)
        # Never guessed: A is enabled as an ordinary trigger, R stays disabled ...
        assert _tgenabled(server) == {"trg": "O", "trg_a": "O", "trg_r": "D", "trg_d": "D"}
        assert (check.status, check.confirmed, check.mismatches) == ("pass", 4, [])
        assert (scripts.in_script, scripts.stays_disabled, scripts.state_folded) == (4, 2, 4)
        # ... and the files say so, with the read-only source query for the hand check.
        assert SOURCE_ALWAYS_REPLICA_QUERY in files[CUTOVER_INSTRUCTIONS]
        assert "check those by hand" in files[CUTOVER_FILE]
        assert "(enabled on the source)" in files[CUTOVER_FILE]
        assert (
            "created disabled enabled or disabled as the collection records;" in files[CUTOVER_FILE]
        )
        assert "to their source state" not in files[CUTOVER_FILE]
        assert "(source state " not in files[CUTOVER_FILE]  # no trigger claims a letter
    finally:
        server.psql(_DROP)
