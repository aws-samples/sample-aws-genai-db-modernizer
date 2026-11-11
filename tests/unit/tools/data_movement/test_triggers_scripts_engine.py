# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The triggers-enable scripts on a real PostgreSQL (#1948), run as the customer runs them.

The same ``pgserver`` PostgreSQL (a Unix socket in a temp folder, no TCP listener) as step
1's routine tests plays the Aurora target: step 1's deploy script creates the routines with
the trigger disabled, then ``psql -X -f triggers-enable.sql`` and ``psql -X -f
triggers-enable-check.sql -o triggers-enable-check.tsv`` run as ``triggers.md`` says. The trigger must be
enabled and fire, and the check must confirm it; then the drift is made by hand and named.
The MySQL twin needs a local ``mysqld``, so it lives in
``tests/integration/test_triggers_scripts_mysql.py``.
"""

from __future__ import annotations

from src.tools.data_movement.routine_scripts import DEPLOY_FILE
from src.tools.data_movement.triggers_check import read_triggers_check
from src.tools.data_movement.triggers_scripts import (
    TRIGGERS_CHECK_FILE,
    TRIGGERS_CHECK_OUTPUT,
    TRIGGERS_FILE,
    write_triggers_scripts,
)
from tests.unit.tools.data_movement import test_routine_scripts_engine as twin
from tests.unit.tools.data_movement.routine_fixtures import (
    contract_with_routines,
    dep_routine,
    migration,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job, _write

# Importing the twin skips this module too when pgserver is not installed.
_DROP, _definitions, _psql = twin._DROP, twin._definitions, twin._psql
server = twin.server  # the module-scoped PostgreSQL fixture, one server for this module


_STATES = {"trg": "O", "trg_a": "A", "trg_r": "R", "trg_d": "D"}


def _tgenabled(server) -> dict[str, str]:
    out = server.psql("\\set QUIET on\n\\pset format unaligned\n\\t on\nSELECT tgname || ':' || "
                      "tgenabled::text FROM pg_trigger WHERE tgrelid = 'public.orders'::regclass "
                      "AND NOT tgisinternal ORDER BY 1;")  # fmt: skip
    return dict(line.split(":") for line in out.split())


def test_the_triggers_enable_script_enables_the_trigger_step_1_created_disabled_and_the_check_confirms_it(
    server, tmp_path
):
    defs = _definitions(server)
    server.psql(_DROP)
    rows = [row("public.audit()", specific="audit_3", signature="")]
    sources = [pg_source("audit_3", defs["audit:"])]
    for name in _STATES:
        rows.append(row(f"public.orders.{name}", "trigger", name=name, table="public.orders",
                        specific=f"orders.{name}",
                        dependencies=[dep_routine("function", "public.audit()")]))  # fmt: skip
        text = defs["trg:"].replace("TRIGGER trg ", f"TRIGGER {name} ")
        sources.append(pg_source(f"orders.{name}", text, kind="trigger"))
    artifact = migration(rows)
    store = _job(tmp_path, artifact, sources)
    contract = contract_with_routines(artifact, triggers={
        f"public.orders.{n}": {"source_state": state} for n, state in _STATES.items()})  # fmt: skip
    assert _write(tmp_path, store, contract).refused == []
    step = tmp_path / "step"
    deployed = _psql(server, "-f", str(step / DEPLOY_FILE))
    assert deployed.returncode == 0, deployed.stderr
    assert set(_tgenabled(server).values()) == {"D"}  # step 1 created every one disabled
    scripts = write_triggers_scripts(store, "shop", "job1", contract, SHA, step)
    assert (scripts.expected, scripts.in_script, scripts.stays_disabled) == (4, 4, 1)

    def check():
        ran = _psql(
            server, "-f", str(step / TRIGGERS_CHECK_FILE), "-o", str(step / TRIGGERS_CHECK_OUTPUT)
        )
        assert ran.returncode == 0, ran.stderr
        return read_triggers_check(step / TRIGGERS_CHECK_OUTPUT, contract, store, job_id="job1",
                                  database_name="shop", contract_sha=SHA, target_database=None)  # fmt: skip

    assert {m.routine_id: m.problem for m in check().mismatches} == {
        f"public.orders.{n}": "trigger_state_differs" for n in ("trg", "trg_a", "trg_r")}  # fmt: skip
    ran = _psql(server, "-f", str(step / TRIGGERS_FILE))
    assert ran.returncode == 0, ran.stderr
    assert _tgenabled(server) == _STATES  # exactly the source state, each one
    after = check()
    assert (after.status, after.confirmed, after.mismatches) == ("pass", 4, [])
    fired = server.psql("\\set QUIET on\n\\t on\nINSERT INTO public.orders (id) VALUES (1948) "
                        "RETURNING status;")  # fmt: skip
    assert fired.strip() == "new"  # the O and A triggers fire for an application's write
    # Running it again is harmless: setting a trigger to the state it is in changes nothing.
    assert _psql(server, "-f", str(step / TRIGGERS_FILE)).returncode == 0
    server.psql("ALTER TABLE public.orders ENABLE TRIGGER trg_a;")
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "public.orders.trg_a": "trigger_state_differs"}  # fmt: skip
    server.psql("DROP TRIGGER trg ON public.orders;")
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "public.orders.trg": "missing", "public.orders.trg_a": "trigger_state_differs"}  # fmt: skip
    # With a trigger gone the script fails as a whole, in its one transaction.
    failed = _psql(server, "-f", str(step / TRIGGERS_FILE))
    assert failed.returncode != 0 and "does not exist" in failed.stderr
    assert _tgenabled(server)["trg_a"] == "O"  # nothing changed
    server.psql("DELETE FROM public.orders WHERE id = 1948; DROP FUNCTION public.audit() CASCADE;")
