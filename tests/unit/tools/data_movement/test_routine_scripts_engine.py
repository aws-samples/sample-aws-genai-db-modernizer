# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routine scripts on a real PostgreSQL (#1947), run as the customer runs them.

A PostgreSQL started by ``pgserver`` on a Unix socket in a temp folder (no TCP listener)
plays the Aurora target. The routines' exact text is read from the server itself
(``pg_get_functiondef``, ``pg_get_triggerdef``, as the collector reads it), the routines
dropped, and then ``psql -X -f routines-deploy.sql`` and ``psql -X -f routines-check.sql
-o routines-check.tsv`` run exactly as ``routines.md`` says. The check output must
confirm every carried routine, with the trigger disabled; then each kind of drift is
made by hand and named. The tests skip when ``pgserver`` is not installed or cannot start.
The MySQL twin needs a local ``mysqld`` and ``mysql`` client, so it lives in
``tests/integration/test_routine_scripts_mysql.py``.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs the bundled psql with fixed argv
from pathlib import Path

import pytest

from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.routine_check import read_check
from src.tools.data_movement.routine_scripts import CHECK_FILE, CHECK_OUTPUT, DEPLOY_FILE
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    pg_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job, _write

pgserver = pytest.importorskip("pgserver")

_SETUP = f"""
CREATE TABLE public.orders (id int PRIMARY KEY, status text);
CREATE FUNCTION public.f(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT p + 1 $$;
CREATE FUNCTION public.f(p text) RETURNS text LANGUAGE sql AS $$ SELECT p || '{SENTINEL}' $$;
CREATE FUNCTION public.g(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT public.f(p) $$;
CREATE FUNCTION public.audit() RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN NEW.status := coalesce(NEW.status, 'new'); RETURN NEW; END $$;
CREATE TRIGGER trg BEFORE INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION public.audit();
"""
_DEFS = """\\set QUIET on
\\pset format unaligned
\\pset tuples_only on
SELECT string_agg(k || chr(31) || d, chr(30)) FROM (
  SELECT p.proname || ':' || pg_get_function_identity_arguments(p.oid) AS k, pg_get_functiondef(p.oid) AS d
  FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
  UNION ALL SELECT 'trg:', pg_get_triggerdef(t.oid) FROM pg_trigger t WHERE t.tgname = 'trg') x;
"""
_DROP = """DROP TRIGGER trg ON public.orders; DROP FUNCTION public.g(integer);
DROP FUNCTION public.f(integer); DROP FUNCTION public.f(text); DROP FUNCTION public.audit();"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SETUP)
    yield srv
    srv.cleanup()


def _psql(server, *args: str) -> subprocess.CompletedProcess[str]:
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    return subprocess.run([str(psql), server.get_uri(), "-X", *args], capture_output=True,
                          text=True, check=False)  # fmt: skip


def _definitions(server) -> dict[str, str]:
    out = server.psql(_DEFS)
    pairs = [r.split("\x1f", 1) for r in out.rstrip("\n").split("\x1e")]
    return {k.strip(): v for k, v in pairs}


def test_the_scripts_deploy_and_confirm_on_postgresql_then_name_each_drift(server, tmp_path):
    defs = _definitions(server)
    server.psql(_DROP)
    rows = [
        row("public.f(integer)", name="f", specific="f_1", signature="integer"),
        row("public.f(text)", name="f", specific="f_2", signature="text"),
        row("public.audit()", specific="audit_3", signature=""),
        row("public.g(integer)", name="g", specific="g_4", signature="integer",
            dependencies=[dep_routine("function", "public.f(integer)")]),
        row("public.orders.trg", "trigger", name="trg", table="public.orders", specific="orders.trg",
            dependencies=[dep_routine("function", "public.audit()")]),
    ]  # fmt: skip
    artifact = migration(rows)
    sources = [pg_source("f_1", defs["f:p integer"]), pg_source("f_2", defs["f:p text"]),
               pg_source("audit_3", defs["audit:"]), pg_source("g_4", defs["g:p integer"]),
               pg_source("orders.trg", defs["trg:"], kind="trigger")]  # fmt: skip
    store = _job(tmp_path, artifact, sources)
    contract = contract_with_routines(artifact)
    assert _write(tmp_path, store, contract).in_script == 5
    step = tmp_path / "step"

    deployed = _psql(server, "-f", str(step / DEPLOY_FILE))
    assert deployed.returncode == 0, deployed.stderr
    state = server.psql(
        "\\set QUIET on\n\\t on\nSELECT tgenabled FROM pg_trigger WHERE tgname = 'trg';"
    )
    assert state.strip() == "D"  # created disabled, in the deploy's own transaction

    def check():
        ran = _psql(server, "-f", str(step / CHECK_FILE), "-o", str(step / CHECK_OUTPUT))
        assert ran.returncode == 0, ran.stderr
        return read_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                          contract_sha=SHA, target_database=None)  # fmt: skip

    result = check()
    assert (result.status, result.confirmed, result.mismatches) == ("pass", 5, [])

    server.psql("ALTER TABLE public.orders ENABLE TRIGGER trg;")
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "public.orders.trg": "trigger_enabled"
    }
    server.psql("ALTER TABLE public.orders DISABLE TRIGGER trg; DROP FUNCTION public.g(integer);"
                "DROP FUNCTION public.f(integer);"
                "CREATE FUNCTION public.f(p bigint) RETURNS bigint LANGUAGE sql AS $$ SELECT p $$;")  # fmt: skip
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "public.f(integer)": "signature_mismatch", "public.g(integer)": "missing"}  # fmt: skip
    assert SENTINEL not in (step / CHECK_OUTPUT).read_text()


def test_the_check_script_is_read_only(server, tmp_path):
    artifact = migration([row("public.nothing()", signature="")])
    store = LocalArtifactStore(base_dir=str(tmp_path))
    contract = contract_with_routines(artifact)
    store = _job(tmp_path, artifact, [pg_source("nothing", "CREATE FUNCTION public.nothing() "
                                                "RETURNS int LANGUAGE sql AS $$ SELECT 1 $$")])  # fmt: skip
    _write(tmp_path, store, contract)
    script = (tmp_path / "step" / CHECK_FILE).read_text()
    # A write slipped into its transaction fails: the transaction is READ ONLY.
    tampered = script.replace("COMMIT;", "CREATE TABLE public.written (id int);\nCOMMIT;")
    (tmp_path / "tampered.sql").write_text(tampered)
    ran = _psql(server, "-f", str(tmp_path / "tampered.sql"), "-o", str(tmp_path / "out.tsv"))
    assert ran.returncode != 0 and "read-only transaction" in ran.stderr


def test_the_deploy_creates_a_missing_routine_schema_and_runs_again_unchanged(server, tmp_path):
    # Discourse's routines live in discourse_functions, a schema the step 1 DDL never
    # creates (#2148): the deploy creates it first, and a second run changes nothing.
    server.psql("\\set ON_ERROR_STOP on\nCREATE SCHEMA discourse_functions;\n"
                "CREATE FUNCTION discourse_functions.bump(p integer) RETURNS integer LANGUAGE sql "
                "AS $$ SELECT p + 1 $$;")  # fmt: skip
    text = server.psql("\\set QUIET on\n\\pset format unaligned\n\\pset tuples_only on\n"
                       "SELECT pg_get_functiondef('discourse_functions.bump(integer)'::regprocedure);")  # fmt: skip
    server.psql("DROP SCHEMA discourse_functions CASCADE;")
    rows = [row("discourse_functions.bump(integer)", schema="discourse_functions", name="bump",
                specific="bump_1", signature="integer")]  # fmt: skip
    artifact = migration(rows)
    store = _job(tmp_path, artifact,
                 [pg_source("bump_1", text.strip(), schema="discourse_functions")])  # fmt: skip
    contract = contract_with_routines(artifact)
    assert _write(tmp_path, store, contract).in_script == 1
    step = tmp_path / "step"
    try:
        for _ in range(2):
            deployed = _psql(server, "-f", str(step / DEPLOY_FILE))
            assert deployed.returncode == 0, deployed.stderr
        ran = _psql(server, "-f", str(step / CHECK_FILE), "-o", str(step / CHECK_OUTPUT))
        assert ran.returncode == 0, ran.stderr
        result = read_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                            contract_sha=SHA, target_database=None)  # fmt: skip
        assert (result.status, result.confirmed, result.mismatches) == ("pass", 1, [])
    finally:
        server.psql("DROP SCHEMA IF EXISTS discourse_functions CASCADE;")
