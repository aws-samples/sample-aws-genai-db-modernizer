# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine proof's Data API side on the Aurora test cluster (#1945).

Every Data API call goes to :class:`FakeDataApi`: nothing is sent. The modules under test
are imported inside each test, so on a branch without them a test fails in its body."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.proof_inputs import (
    ProofCall,
    ProofSetup,
    RoutineParameter,
    RoutineProofInput,
)
from tests.unit.tools.routines.fake_data_api import FakeDataApi, db_error, runner_for

ARNS = {"cluster_arn": "arn:aws:rds:us-east-1:111122223333:cluster:c",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s"}  # fmt: skip  # pragma: allowlist secret
JOB = "job1"
TABLES = [{"table_id": "public.t", "table_name": "t", "row_count": 3, "primary_key": ["id"],
           "columns": [{"column_name": "id", "data_type": "integer", "nullable": False}]}]  # fmt: skip
FN = "CREATE OR REPLACE FUNCTION public.f(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$"
BROKEN = ("CREATE OR REPLACE FUNCTION public.g(p integer) RETURNS integer LANGUAGE sql "
          "AS $$ SELECT broken_marker $$")  # fmt: skip


def _mod(name: str) -> Any:
    """The module under test, imported here so a branch without it fails in the test."""
    if name == "proof_run":
        return importlib.import_module("src.tools.routines.proof_run")
    return importlib.import_module("src.tools.routines.data_api_target")


RUNNER_ARN = (
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:runner"  # pragma: allowlist secret
)


def _runner() -> Any:
    dat = _mod("data_api_target")
    return dat.RunnerAccount(dat.runner_prefix(JOB) + "0a0b0c0d", RUNNER_ARN)


def _factory(client: FakeDataApi, engine: str = "postgresql", **kw: Any) -> Any:
    if engine == "mysql":
        kw.setdefault("runner", _runner())
    elif "runner" not in kw:  # made as the proof makes it, so the fake checks its sign-in
        kw["runner"] = runner_for(client, **ARNS, job_id=JOB)
    return _mod("data_api_target").data_api_target(
        client,
        **ARNS,
        engine=engine,
        version="16.15",
        job_id=JOB,
        admin_database="routine_proof",
        **kw,
    )


def _routine(rid: str, name: str, text: str, deps: tuple[str, ...] = ()) -> Any:
    from src.tools.routines.comparison import ComparedRoutine

    proof = RoutineProofInput(routine_id=rid, kind="function", name=name, returns="integer",
                              parameters=(RoutineParameter(name="p", data_type="integer"),), tables=("public.t",))  # fmt: skip
    statement = (de.SourceRoutine(name, "function", text),)
    return ComparedRoutine(proof, statement, statement, deps)


# --------------------------------------------------------------------------- scratch database


def test_a_target_creates_a_job_scoped_scratch_database_loads_runs_and_drops_it():
    """Create, load, run and drop: every statement after the create names the scratch
    database, the rows go as bound parameters, the call binds its argument, and closing
    drops the database (the run's runner role is dropped by the run, #2352)."""
    dat = _mod("data_api_target")
    client = FakeDataApi(calls={'"public"."f"': [7]})
    factory = _factory(client)
    start = len(client.sent)
    target = factory()
    name = target.database
    assert name.startswith(dat.scratch_prefix(JOB)) and dat.scratch_name(JOB) != name
    assert (
        client.sent[start].sql == f'CREATE DATABASE "{name}"'
        and client.sent[start].database == "routine_proof"
    )
    setup = ProofSetup(dialect="postgresql", ddl=('CREATE TABLE "t" ("id" integer PRIMARY KEY)',),
                       rows=(("t", ({"id": 4242},)),), tables=("public.t",))  # fmt: skip
    target.load_setup(setup)
    assert target.create(de.SourceRoutine("public.f", "function", FN)) is None
    target.prepare_calls()
    target.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    result = target.run(
        ProofCall("f", "baseline", "call", "public.f", "function", ("5",), ("IN",), ("integer",))
    )
    target.end_call()
    target.close()
    assert result.error is None and result.rows == [[7]] and result.column_types == (("integer",),)
    batch = next(s for s in client.sent if s.op == "batch")
    assert "4242" not in batch.sql and json.loads(
        batch.parameters[0][0]["value"]["stringValue"]
    ) == {"id": 4242}
    assert "json_populate_record" in batch.sql and batch.database == name
    call = next(s for s in client.sent if '"public"."f"(' in s.sql)
    assert "CAST(:p0 AS integer)" in call.sql and call.parameters[0]["value"] == {
        "stringValue": "5"
    }
    assert call.transaction is not None and call.database == name
    role = f'"{target._runner.username}"'
    assert not [s for s in client.sent if "SET LOCAL ROLE" in s.sql]  # signed in as the role
    admin = [s.sql for s in client.sent[start:] if s.op == "execute" and s.database != name]
    assert admin == [f'CREATE DATABASE "{name}"',
                     f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC',
                     f'GRANT CONNECT, TEMPORARY ON DATABASE "{name}" TO {role}',
                     f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)']  # fmt: skip
    assert client.sent[-1].sql == f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
    assert not client.databases


def test_a_mysql_target_uses_its_scratch_database_as_the_namespace():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")
    t = target()
    assert t.namespace == t.database and client.sent[0].sql == f"CREATE DATABASE `{t.database}`"
    runner = _runner()
    harness = f"`{t.database}_harness`"  # the harness's own database first (#2560)
    assert [s.sql for s in client.sent[1:4]] == [
        f"CREATE DATABASE {harness}",
        f"CREATE TABLE {harness}.`modernizer_sentinel` (id INTEGER PRIMARY KEY)",
        f"GRANT SELECT, INSERT ON {harness}.`modernizer_sentinel` TO '{runner.username}'@'%'",
    ]
    assert (
        client.sent[4].sql
        == f"GRANT ALL PRIVILEGES ON `{t.database.replace('_', chr(92) + '_')}`.* TO '{runner.username}'@'%'"
    )
    t.close()
    assert [s.sql for s in client.sent[-2:]] == [f"DROP DATABASE IF EXISTS `{t.database}`",
                                                 f"DROP DATABASE IF EXISTS {harness}"]  # fmt: skip
    assert not client.databases


def test_the_scratch_database_is_dropped_when_its_setup_fails():
    """Drop on failure: a target whose grant cannot be made drops the database it made, and
    a load that fails leaves nothing once the side closes."""
    client = FakeDataApi()
    factory = _factory(client)
    client.fail_on = {"GRANT CONNECT": "42501"}
    with pytest.raises(Exception, match="scratch database was not created"):
        factory()
    assert client.databases == set() and any(s.sql.startswith("DROP DATABASE") for s in client.sent)

    from src.tools.routines.comparison import run_routines

    client = FakeDataApi(fail_on={'CREATE TABLE "t"': "42P07"})
    runs = run_routines([_routine("public.f(integer)", "public.f", FN)], TABLES, target=_factory(client),
                        dialect="postgresql")  # fmt: skip
    assert runs[0].outcome == "not_run" and runs[0].reason_code == "engine_failed"
    assert "synthetic rows did not load" in runs[0].reason
    assert client.databases == set()
    assert len([s for s in client.sent if s.sql.startswith("CREATE DATABASE")]) == len(
        [s for s in client.sent if s.sql.startswith("DROP DATABASE")])  # fmt: skip


def test_drop_scratch_drops_only_this_jobs_scratch_databases():
    dat = _mod("data_api_target")
    client = FakeDataApi()
    mine = dat.scratch_name(JOB)
    client.databases |= {
        mine,
        dat.scratch_name("other-job"),
        "routine_proof",
        dat.scratch_prefix(JOB) + "nothex!",
    }
    dropped, left = dat.drop_scratch(
        client, **ARNS, engine="postgresql", job_id=JOB, admin_database="routine_proof"
    )
    assert left == []
    assert (
        dropped == [mine] and mine not in client.databases and "routine_proof" in client.databases
    )
    assert len(client.databases) == 3


# --------------------------------------------------------------------------- calls


def test_one_routine_that_does_not_load_never_stops_the_others():
    from src.tools.routines.comparison import run_routines

    client = FakeDataApi(calls={'"public"."f"': [1]}, fail_on={"broken_marker": "42703"})
    runs = {r.routine_id: r for r in run_routines(
        [_routine("public.g(integer)", "public.g", BROKEN), _routine("public.f(integer)", "public.f", FN)],
        TABLES, target=_factory(client), dialect="postgresql")}  # fmt: skip
    assert runs["public.g(integer)"].outcome == "does_not_load"
    assert runs["public.g(integer)"].error_states == ("42703",)
    assert (
        runs["public.f(integer)"].outcome == "ran"
        and runs["public.f(integer)"].calls_succeeded >= 1
    )
    assert "broken_marker" not in json.dumps([r.model_dump() for r in runs.values()])
    assert not client.databases


def test_one_routine_whose_calls_break_the_data_api_never_stops_the_others():
    """An infrastructure failure (no SQL error) on every call of one routine fails that
    routine only: the side is opened again and the next routine runs."""
    from src.tools.routines.comparison import run_routines

    client = FakeDataApi(calls={'"public"."f"': [1]}, internal_on=('"public"."g"(',))
    g = _routine("public.g(integer)", "public.g", FN.replace("public.f(", "public.g("))
    runs = {r.routine_id: r for r in run_routines(
        [g, _routine("public.f(integer)", "public.f", FN)], TABLES, target=_factory(client),
        dialect="postgresql")}  # fmt: skip
    assert runs["public.g(integer)"].outcome == "not_run"
    assert runs["public.g(integer)"].reason_code == "engine_failed"
    assert (
        runs["public.f(integer)"].outcome == "ran"
        and runs["public.f(integer)"].calls_succeeded >= 1
    )
    assert not client.databases


def test_a_failed_call_is_an_error_result_with_its_sqlstate_and_the_side_goes_on():
    client = FakeDataApi(fail_on={'"public"."f"(': "22012"})
    target = _factory(client)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.create(de.SourceRoutine("public.f", "function", FN.replace("f(", "f2(")))
    target.prepare_calls()
    target.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    result = target.run(
        ProofCall("f", "baseline", "call", "public.f", "function", ("1",), ("IN",), ("integer",))
    )
    assert result.sqlstate == "22012" and result.error is not None
    assert [s.sql for s in client.sent[-2:]] == [
        "ROLLBACK TO SAVEPOINT modernizer_call", "SELECT current_user::text, session_user::text"]  # fmt: skip
    target.end_call()
    target.close()


def test_the_data_api_error_text_gives_the_sqlstate_errno_and_first_line():
    parse = _mod("data_api_target").parse_error
    pg = parse(db_error("23505", 'ERROR: duplicate key value violates unique constraint "t_pkey"\n'
                        "  Detail: Key (id)=(1) already exists."))  # fmt: skip
    assert (
        pg.sqlstate == "23505"
        and pg.message == 'duplicate key value violates unique constraint "t_pkey"'
    )
    my = parse(db_error("42S02", "Database error code: 1146. Message: Table 'x.t' doesn't exist"))
    assert my.sqlstate == "42S02" and my.errno == 1146 and my.message == "Table 'x.t' doesn't exist"


def test_errors_that_differ_only_in_detail_match_when_a_side_reports_no_detail():
    from src.tools.routines.comparison import CallRun, compare_call

    a = CallRun(
        de.CallResult(error="e", sqlstate="23505", message="dup\nKey (id)=(1)", constraint="p/t//"),
        {},
        {},
    )
    b = CallRun(de.CallResult(error="e", sqlstate="23505", message="dup"), {}, {})
    assert compare_call(a, b, call="baseline", index=1, tables=())  # every field counts by default
    assert not compare_call(a, b, call="baseline", index=1, tables=(), error_fields=False)
    c = CallRun(de.CallResult(error="e", sqlstate="23505", message="other"), {}, {})
    assert compare_call(a, c, call="baseline", index=1, tables=(), error_fields=False)


def test_a_postgresql_row_change_binds_its_row_and_is_read_by_the_columns_types():
    client = FakeDataApi()
    target = _factory(client)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.prepare_calls()
    target.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    target.run(ProofCall("trg", "update", "row_change", "t", "UPDATE", ("2024-01-02", 31337),
                         columns=("d",), where=("id",)))  # fmt: skip
    target.end_call()
    target.close()
    sent = next(s for s in client.sent if s.sql.startswith('UPDATE "t"'))
    assert "31337" not in sent.sql and "2024-01-02" not in sent.sql
    assert [json.loads(p["value"]["stringValue"]) for p in sent.parameters] == [
        {"d": "2024-01-02"},
        {"id": 31337},
    ]


def test_the_strict_check_records_plpgsql_check_errors_by_routine_id():
    strict: dict[str, list[tuple[str, str]]] = {}
    ran: list[str] = []
    client = FakeDataApi(
        plpgsql_check=True, strict={"public.f(integer)": [("error", "42703"), ("warning", "00000")]}
    )
    target = _factory(client, strict=strict, strict_check=ran)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.prepare_calls()
    target.close()
    assert ran == ["plpgsql_check"] and strict == {"public.f(integer)": [("error", "42703")]}
    assert any(s.sql == 'CREATE EXTENSION IF NOT EXISTS plpgsql_check SCHEMA "modernizer_harness"'
               and s.secret == ARNS["secret_arn"] for s in client.sent)  # fmt: skip

    strict2: dict[str, list[tuple[str, str]]] = {}
    ran2: list[str] = []
    target = _factory(FakeDataApi(), strict=strict2, strict_check=ran2)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.prepare_calls()
    target.close()
    assert ran2 == ["plpgsql_extra_errors"] and strict2 == {}  # no PL/pgSQL routine was created


def test_without_plpgsql_check_each_plpgsql_routine_is_compiled_with_extra_errors():
    """Aurora PostgreSQL does not offer plpgsql_check: each PL/pgSQL routine is compiled again
    with plpgsql.extra_errors = 'all', in a transaction that is rolled back."""
    dat = _mod("data_api_target")
    plpgsql = ("CREATE OR REPLACE FUNCTION public.k(p integer) RETURNS integer LANGUAGE plpgsql "
               "AS $$ DECLARE p integer; BEGIN RETURN shadow_marker; END $$")  # fmt: skip
    strict: dict[str, list[tuple[str, str]]] = {}
    ran: list[str] = []
    client = FakeDataApi(extra_errors_fail={"shadow_marker": "42702"})
    ids = {dat.text_key(plpgsql): "public.k(integer)", dat.text_key(FN): "public.f(integer)"}
    target = _factory(client, strict=strict, strict_check=ran, routine_ids=ids)()
    assert target.create(de.SourceRoutine("public.k", "function", plpgsql)) is None  # it loads
    assert target.create(de.SourceRoutine("public.f", "function", FN)) is None  # SQL: not checked
    target.close()
    assert ran == ["plpgsql_extra_errors"] and strict == {"public.k(integer)": [("error", "42702")]}
    checked = [s for s in client.sent if s.sql == "SET LOCAL plpgsql.extra_errors = 'all'"]
    assert len(checked) == 1
    # then the routines' path (#2337) and the CREATE, then the rollback: never kept
    assert client.sent[client.sent.index(checked[0]) + 3].op == "rollback"


def test_a_paused_cluster_is_retried_while_it_resumes(monkeypatch):
    dat = _mod("data_api_target")
    client = FakeDataApi()
    real = client.execute_statement
    state = {"n": 0}

    def resuming(**kw: Any) -> dict[str, Any]:
        state["n"] += 1
        if state["n"] < 3:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}},
                "ExecuteStatement",
            )
        got: dict[str, Any] = real(**kw)
        return got

    monkeypatch.setattr(client, "execute_statement", resuming)
    monkeypatch.setattr(dat.time, "sleep", lambda _s: None)
    target = _factory(client)()
    assert target.database in client.databases
    target.close()


def test_a_text_that_resets_its_role_is_refused():
    """Each call runs under SET LOCAL ROLE on the test cluster: a text that would undo it
    is never created there."""
    from src.tools.routines.proof_screen import create_refusal

    for body in ("RESET ROLE", "RESET ALL", "DISCARD ALL"):
        text = f"CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN {body}; END $$"
        assert (
            create_refusal(text, "postgresql", "function", "public")
            == "it changes the role it runs as"
        ), body
    plain = (
        "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN RESET lock_timeout; END $$"
    )
    assert create_refusal(plain, "postgresql", "function", "public") is None


def test_a_postgresql_value_read_as_text_is_converted_by_its_type_as_the_driver_does():
    """The Data API returns no interval, NaN or infinite value: a snapshot reads each column
    as text and converts it here, as psycopg2 converts that type on the disposable engine."""
    text = _mod("data_api_target")._pg_text
    assert text("true", "bool", "B") is True and text("false", "bool", "B") is False
    assert text("42", "int8", "N") == 42 and text("NaN", "float8", "N") != text(
        "NaN", "float8", "N"
    )
    assert (
        text("Infinity", "float8", "N") == float("inf") and text("1.50", "numeric", "N") == "1.50"
    )
    assert (
        text("1 mon", "interval", "T") == "1 mon"
        and text("infinity", "timestamp", "D") == "9999-12-31T23:59:59.999999"
    )
    assert text("2024-01-01 10:00:00+00", "timestamptz", "D") == "2024-01-01T15:47:00+05:47"
    assert text("2024-01-01 10:00:00.5", "timestamp", "D") == "2024-01-01T10:00:00.500000"
    assert text("\\x0102", "bytea", "U") == "0102" and text("[1, 2]", "_int4", "A") == [1, 2]
    assert text('{"b": 1, "a": 2}', "jsonb", "U") == {"b": 1, "a": 2}


def test_a_mysql_load_runs_outside_a_transaction():
    """A MySQL DDL statement commits implicitly, which ends the Data API's own savepoints
    inside a transaction: the setup and the routines load statement by statement."""
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")()
    target.load_setup(ProofSetup(dialect="mysql", ddl=("CREATE TABLE `t` (`id` int PRIMARY KEY)",),
                                 rows=(("t", ({"id": 1},)),)))  # fmt: skip
    target.create(de.SourceRoutine("f", "function", "CREATE FUNCTION f() RETURNS int RETURN 1"))
    target.prepare_calls()
    target.close()
    ddl = [
        s
        for s in client.sent
        if s.op in ("execute", "batch") and s.sql.startswith(("CREATE", "INSERT"))
    ]
    assert ddl and all(s.transaction is None for s in ddl)


def test_a_mysql_value_read_as_text_is_converted_by_its_type_as_the_driver_does():
    text = _mod("data_api_target")._my_text
    assert text("18446744073709551615", "bigint") == 18446744073709551615
    assert text("1.50", "decimal") == "1.50" and text("2.5", "double") == 2.5
    assert text("01:00:01.5", "time") == 3601.5 and text("0A", "bit") == 10
    assert (
        text("00FF", "varbinary") == "00ff"
        and text("2024-01-01 12:00:00", "datetime") == "2024-01-01T12:00:00"
    )


@pytest.mark.parametrize(
    "body",
    ['RESET "role"', 'RESET "ROLE"', 'SET "role" TO DEFAULT', 'SET LOCAL "Role" = postgres',
     "SET SESSION AUTHORIZATION DEFAULT", 'SET SESSION "authorization" DEFAULT',
     "RESET SESSION AUTHORIZATION", "PERFORM set_config('role', 'postgres', true)",
     "RESET/*x*/role", 'RESET "session_authorization"', "SET session_authorization = DEFAULT",
     "RESET -- a comment\n role", "SET LOCAL session_authorization TO DEFAULT"],
)  # fmt: skip
def test_every_spelling_of_a_role_change_is_refused(body):
    """Review of #2297: the screen reads each token as one normalised word, so a quoted or
    otherwise spelled role change is refused like the plain one."""
    from src.tools.routines.proof_screen import create_refusal

    text = f"CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $$ BEGIN {body}; RETURN 1; END $$"
    assert create_refusal(text, "postgresql", "function", "public") is not None, body


HEADER = "CREATE FUNCTION public.g() RETURNS text LANGUAGE {lang} {clause} AS $$ {body} $$"


@pytest.mark.parametrize(
    "text",
    [HEADER.format(lang="sql", clause="SET role = 'master'", body="SELECT current_user::text"),
     HEADER.format(lang="sql", clause="SET role TO master", body="SELECT current_user::text"),
     HEADER.format(lang="sql STABLE", clause="SET role = 'master'", body="SELECT 1"),
     HEADER.format(lang="plpgsql", clause="SET session_authorization = 'master'",
                   body="BEGIN RETURN 1; END"),
     HEADER.format(lang="sql", clause="SET SESSION AUTHORIZATION master", body="SELECT 1"),
     "CREATE PROCEDURE public.p() LANGUAGE sql SET role = 'master' AS $$ SELECT 1 $$",
     "CREATE PROCEDURE public.p() LANGUAGE plpgsql SECURITY INVOKER SET role TO master "
     "AS $$ BEGIN NULL; END $$",
     HEADER.format(lang="plpgsql", clause="", body="BEGIN ALTER FUNCTION public.f() SET role = "
                   "'master'; RETURN 1; END"),
     HEADER.format(lang="plpgsql", clause="", body="BEGIN SET role = 'master'; RAISE EXCEPTION "
                   "'boom'; END"),
     "CREATE FUNCTION public.g(\"update\" int) RETURNS int LANGUAGE sql SET role = 'master' "
     "AS $$ VALUES (1) $$",
     "CREATE FUNCTION public.g(\"do\" \"update\") RETURNS int LANGUAGE sql SET role = 'master' "
     "AS $$ VALUES (1) $$"],
)  # fmt: skip
def test_a_role_switch_inside_a_call_is_refused_wherever_it_is_written(text):
    """Review round 3 of #2297: a function's own ``SET role`` clause switches the role on
    entry and back on return, so the read after the call never sees it, and a call that
    switches and then fails is rolled back to its savepoint before any read can run (the
    transaction is aborted until then). The screen refuses the clause wherever it is
    written: a header clause after LANGUAGE or STABLE, ``SET role TO``, a procedure header,
    ``ALTER FUNCTION ... SET role`` in a body, and a switch followed by an error."""
    from src.tools.routines.proof_screen import create_refusal

    assert create_refusal(text, "postgresql", "function", "public") is not None, text


def test_a_column_named_role_is_not_a_role_change():
    from src.tools.routines.proof_screen import create_refusal

    text = ("CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN "
            "UPDATE t SET role = 'x' WHERE id = 1; END $$")  # fmt: skip
    assert create_refusal(text, "postgresql", "function", "public") is None
    upsert = ("CREATE FUNCTION f() RETURNS void LANGUAGE sql AS $$ INSERT INTO t (id, role) "
              "VALUES (1, 'x') ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role $$")  # fmt: skip
    assert create_refusal(upsert, "postgresql", "function", "public") is None
    mysql = "CREATE PROCEDURE p() BEGIN UPDATE t SET password = 'x', user = 'y' WHERE id = 1; END"
    assert create_refusal(mysql, "mysql", "procedure", "src") is None


@pytest.mark.parametrize(
    "body",
    ["CREATE USER 'x'@'%' IDENTIFIED BY 'y'", "GRANT ALL ON *.* TO 'x'@'%'",
     "REVOKE ALL ON *.* FROM 'x'@'%'", "CREATE DATABASE evil", "DROP DATABASE other",
     "CREATE SCHEMA evil", "SET GLOBAL event_scheduler = ON", "SET PERSIST max_connections = 10",
     "SET @@GLOBAL.event_scheduler = ON", "DROP USER 'x'@'%'", "RENAME USER a TO b",
     "RENAME USER 'a'@'%' TO 'b'@'%'", "SET PASSWORD = 'x'", "SET PASSWORD FOR 'x'@'%' = 'y'",
     "INSTALL PLUGIN p SONAME 'p.so'", "UNINSTALL PLUGIN p", "KILL 5", "KILL QUERY 5"],
)  # fmt: skip
def test_a_mysql_body_that_acts_outside_the_scratch_database_is_refused(body):
    """Review of #2297: a MySQL DDL or account statement commits on its own and outlives
    the call's rollback, so the screen refuses it (defence in depth; the calls also run as
    a user granted the scratch database only)."""
    from src.tools.routines.proof_screen import create_refusal

    text = f"CREATE PROCEDURE p() BEGIN {body}; END"
    assert create_refusal(text, "mysql", "procedure", "src") is not None, body


def test_a_mysql_target_runs_everything_but_its_own_database_as_the_restricted_runner():
    """Review of #2297: on MySQL the setup, the routines and the calls run as the run's
    restricted user, through its own secret; the master user only creates, grants and drops
    the scratch database."""
    client = FakeDataApi("mysql", calls={"`f`(": [1]})
    t = _factory(client, engine="mysql")()
    t.load_setup(
        ProofSetup(dialect="mysql", ddl=("CREATE TABLE `t` (`id` int PRIMARY KEY)",), rows=())
    )
    t.create(de.SourceRoutine("f", "function", "CREATE FUNCTION f() RETURNS int RETURN 1"))
    t.prepare_calls()
    t.close()
    master = [s.sql for s in client.sent if s.op == "execute" and s.secret == ARNS["secret_arn"]]
    # the scratch database, the harness's database, its sentinel and grant (#2560)
    assert [m.split()[0] for m in master] == ["CREATE", "CREATE", "CREATE", "GRANT", "GRANT",
                                              "DROP", "DROP"]  # fmt: skip
    assert all(s.secret == RUNNER_ARN for s in client.sent
               if s.op in ("execute", "batch") and s.sql not in master)  # fmt: skip


def test_a_mysql_target_needs_a_runner():
    dat = _mod("data_api_target")
    with pytest.raises(ValueError, match="restricted runner"):
        dat.DataApiTarget(FakeDataApi("mysql"), **ARNS, engine="mysql", version="8.0", job_id=JOB,
                          admin_database=None)  # fmt: skip


def test_the_runner_user_and_its_secret_are_made_and_both_removed():
    """#2298: the runner user is created from a caching_sha2_password hash computed here
    (the Data API takes no bound password in CREATE USER), so its plain password is never in
    any statement and is kept only in its own secret, tagged with the job id; the Data API
    then signs in as the user through that secret before the user is handed out; dropping it
    drops the user and deletes the secret with no recovery window."""
    from src.tools.routines.mysql_auth import AUTH_STRING, matches
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets)  # it signs in only with a matching hash
    tags = [{"Key": "modernizer:job-id", "Value": JOB}]
    runner = dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=tags)
    assert runner.username in client.users and runner.username.startswith(dat.runner_prefix(JOB))
    stored = secrets.secrets[runner.secret_arn]
    password = json.loads(stored["SecretString"])["password"]
    assert stored["Tags"] == tags and stored["Name"] == dat.runner_secret_name(JOB, runner.username)
    create = next(s for s in client.sent if s.sql.startswith("CREATE USER"))
    head = f"CREATE USER '{runner.username}'@'%' IDENTIFIED WITH caching_sha2_password AS '"
    assert create.sql.startswith(head) and create.sql.endswith("'") and create.parameters == []
    auth = create.sql[len(head) : -1]
    assert AUTH_STRING.fullmatch(auth) and matches(password, auth)
    assert create.secret == ARNS["secret_arn"]  # the master user creates it
    assert all(password not in s.sql and password not in json.dumps(s.parameters)
               for s in client.sent)  # fmt: skip
    # The sign-in check: the runner's own secret, read back as the runner, with no database.
    who = next(s for s in client.sent if s.sql == "SELECT CURRENT_USER()")
    assert who.secret == runner.secret_arn and not who.named_database
    # No privilege yet: each scratch database grants its own, escaped.
    assert not [s for s in client.sent if s.sql.startswith("GRANT")]
    assert dat.drop_runner(client, secrets, **ARNS, admin_database=None, account=runner) == []
    assert not client.users and secrets.deleted == [(runner.secret_arn, True)]


def test_the_runner_is_granted_its_scratch_database_only_and_runs_as_itself():
    """#2298: through the run, every statement in the scratch database is the runner's
    (its secret), the master user only creates, grants and drops; the grant names the
    scratch database alone, ``_`` escaped so it is no wildcard."""
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets)
    runner = dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=[])
    t = _factory(client, "mysql", runner=runner)()
    grants = [s for s in client.sent if s.sql.startswith("GRANT")]
    assert [g.sql for g in grants] == [
        f"GRANT SELECT, INSERT ON `{t.database}_harness`.`modernizer_sentinel` TO "
        f"'{runner.username}'@'%'",  # the harness's sentinel rows only (#2560)
        f"GRANT ALL PRIVILEGES ON `{t.database.replace('_', chr(92) + '_')}`.* TO "
        f"'{runner.username}'@'%'"]  # fmt: skip
    assert all(g.secret == ARNS["secret_arn"] for g in grants)
    t.load_setup(ProofSetup(dialect="mysql", ddl=("CREATE TABLE `t` (id int)",), rows=()))
    in_scratch = [s for s in client.sent if s.database == t.database]
    assert in_scratch and all(s.secret == runner.secret_arn for s in in_scratch)
    t.close()
    dat.drop_runner(client, secrets, **ARNS, admin_database=None, account=runner)
    assert not client.users and not client.databases and not secrets.secrets


def test_a_runner_the_data_api_cannot_sign_in_as_is_removed_with_its_secret():
    """#2298: when the Data API cannot sign in as the new user (the plugin, the hash), the
    user is dropped and its secret deleted, and the proof never goes on as the master user."""
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets, sign_in_fails=True)
    with pytest.raises(dat.RunnerNotCreated) as refused:
        dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=[])
    assert refused.value.left == [] and refused.value.sqlstate == "28000"
    assert "could not sign in" in str(refused.value)
    assert not client.users and not secrets.secrets and secrets.deleted[0][1] is True


def test_a_sign_in_that_reports_another_user_is_no_runner():
    """#2298: the sign-in check reads CURRENT_USER() back; a secret that signs in as anyone
    but the new user (the master user, say) is refused, and the user and secret removed."""
    from tests.unit.tools.routines.fake_data_api import MASTER, FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets, reports_user=f"{MASTER}@%")
    with pytest.raises(dat.RunnerNotCreated) as refused:
        dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=[])
    assert "another user" in str(refused.value) and refused.value.left == []
    assert not client.users and not secrets.secrets


@pytest.mark.passes_on_main  # the cleanup after a failed CREATE USER is #2297's; kept as a guard
def test_a_runner_user_that_cannot_be_made_leaves_no_secret():
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets, fail_on={"CREATE USER": "HY000"})
    with pytest.raises(dat.RunnerNotCreated) as refused:
        dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=[])
    assert not secrets.secrets and secrets.deleted and secrets.deleted[0][1] is True
    assert refused.value.left == [] and refused.value.sqlstate == "HY000"


@pytest.mark.passes_on_main  # naming a secret the cleanup could not delete is #2297's; kept as a guard
def test_a_runner_whose_secret_cannot_be_deleted_says_so():
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    secrets = FakeSecrets(delete_fails=True)
    client = FakeDataApi("mysql", secrets=secrets, fail_on={"CREATE USER": "HY000"})
    with pytest.raises(dat.RunnerNotCreated) as refused:
        dat.create_runner(client, secrets, **ARNS, job_id=JOB, admin_database=None, tags=[])
    assert refused.value.left and "secret" in refused.value.left[0]


@pytest.mark.passes_on_main  # the fake's 1064 is #2297's; kept so no test can pass on a bound password
def test_the_fake_rejects_a_bound_create_user_password_as_the_real_data_api_does():
    """Found on Aurora MySQL 3.14.0: ``CREATE USER ... IDENTIFIED BY ?`` is error 1064 at
    the ``?``. The fake answers the same, so no test can pass on a bound password (#2298)."""
    client = FakeDataApi("mysql")
    with pytest.raises(Exception) as raised:
        client.execute_statement(sql="CREATE USER 'u'@'%' IDENTIFIED BY :p0", database="",
                                 parameters=[{"name": "p0", "value": {"stringValue": "x"}}])  # fmt: skip
    assert "1064" in str(raised.value) and "42000" in str(raised.value)
    assert not client.users


def test_the_sweep_drops_a_killed_runs_runner_users_and_secrets_and_nothing_else():
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    mine, theirs = dat.runner_prefix(JOB) + "0f0f0f0f", dat.runner_prefix("job2") + "0f0f0f0f"
    client, secrets = FakeDataApi("mysql"), FakeSecrets()
    client.users |= {mine, theirs}
    for job, user in ((JOB, mine), ("job2", theirs)):
        secrets.create_secret(Name=dat.runner_secret_name(job, user), SecretString="{}")
    dropped, left = dat.sweep_runners(client, secrets, **ARNS, job_id=JOB, admin_database=None)
    assert left == [] and client.users == {theirs}
    assert [v["Name"] for v in secrets.secrets.values()] == [dat.runner_secret_name("job2", theirs)]
    assert set(dropped) == {f"user {mine}", f"secret {dat.runner_secret_name(JOB, mine)}"}


@pytest.mark.passes_on_main  # the per-database drop loop is #2297's; kept as a guard
def test_the_sweep_keeps_going_past_a_failed_drop():
    """Review of #2297: one drop that fails never stops the others."""
    dat = _mod("data_api_target")
    prefix = dat.scratch_prefix(JOB)  # the stuck name sorts first, and two come after it
    stuck, after, last = prefix + "00000000", prefix + "77777777", prefix + "ffffffff"
    client = FakeDataApi(drop_fails=(stuck,))
    client.databases |= {stuck, after, last}
    dropped, left = dat.drop_scratch(client, **ARNS, engine="postgresql", job_id=JOB,
                                     admin_database="routine_proof")  # fmt: skip
    assert left == [stuck] and set(dropped) == {after, last}


def test_the_baseline_names_what_a_run_left_outside_the_scratch_databases():
    dat = _mod("data_api_target")
    client = FakeDataApi("mysql")
    before = dat.cluster_baseline(client, **ARNS, engine="mysql", admin_database=None)
    client.users.add("evil")
    client.databases.add("evil_db")
    after = dat.cluster_baseline(client, **ARNS, engine="mysql", admin_database=None)
    assert dat.baseline_changes(before, before) == {}
    assert dat.baseline_changes(before, after) == {
        "databases": {"added": ["evil_db"], "removed": []},
        "users": {"added": ["evil@%"], "removed": []},
    }


def test_a_signed_64_bit_integer_is_bound_as_a_long_and_a_bigger_one_as_text():
    param = _mod("data_api_target")._param
    assert param("p", 2**63 - 1)["value"] == {"longValue": 2**63 - 1}
    assert param("p", -(2**63))["value"] == {"longValue": -(2**63)}
    assert param("p", 2**63)["value"] == {"stringValue": str(2**63)}
    assert param("p", float("nan"))["value"] == {"stringValue": "nan"}


def test_a_plpgsql_check_that_cannot_be_created_is_not_available():
    strict: dict[str, list[tuple[str, str]]] = {}
    ran: list[str] = []
    client = FakeDataApi(plpgsql_check=True, fail_on={"CREATE EXTENSION": "42501"},
                         strict={"public.f(integer)": [("error", "42703")]})  # fmt: skip
    target = _factory(client, strict=strict, strict_check=ran)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.prepare_calls()
    target.close()
    assert ran == ["not_available"] and strict == {}


# --------------------------------------------------------------------------- role, at run time

PLPGSQL = ("CREATE OR REPLACE FUNCTION public.f(p integer) RETURNS integer LANGUAGE plpgsql "
           "AS $$ BEGIN {body}; RETURN 1; END $$")  # fmt: skip
ROLE_ESCAPES = ["RESET/*x*/role", 'RESET "session_authorization"', "EXECUTE 'RESET ' || 'role'"]


def _one_call(client: FakeDataApi, body: str) -> Any:
    target = _factory(client)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.create(de.SourceRoutine("public.f", "function", PLPGSQL.format(body=body)))
    target.prepare_calls()
    target.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    call = ProofCall("f", "baseline", "call", "public.f", "function", ("5",), ("IN",), ("integer",))
    return target, call


@pytest.mark.parametrize("body", ROLE_ESCAPES)
def test_a_call_that_leaves_the_runner_role_is_refused_whatever_the_spelling(body, monkeypatch):
    """Review round 2 of #2297: after each PostgreSQL call and before its rollback, the
    target reads current_user and session_user in the call's transaction; a call that left
    the runner role is refused, and its transaction is still rolled back. The screen is a
    first line only: with it turned off (a spelling it would miss), the read still holds,
    and dynamic SQL, which the screen cannot read, is caught the same way."""
    from src.tools.routines import proof_targets
    from src.tools.routines.proof_targets import RoleChanged

    if body != ROLE_ESCAPES[-1]:
        with pytest.raises(proof_targets.CreateRefused):  # the screen sees these two
            _one_call(FakeDataApi(), body)
    monkeypatch.setattr(proof_targets, "create_refusal", lambda *_a, **_k: None)
    client = FakeDataApi(calls={'"public"."f"': [1]}, role_reset_on=('"public"."f"(',))
    target, call = _one_call(client, body)
    with pytest.raises(RoleChanged):
        target.run(call)
    tx = next(s.transaction for s in client.sent if '"public"."f"(' in s.sql)
    check = [
        s
        for s in client.sent
        if s.sql.startswith("SELECT current_user::text") and s.database == target.database
    ]  # the runner's sign-in check is not a call's
    assert check and all(s.transaction == tx for s in check)
    target.end_call()
    assert client.sent[-1].op == "rollback" and client.sent[-1].transaction == tx
    target.close()


def test_a_failing_call_is_read_too_right_after_its_savepoint_rollback():
    """A failing call's transaction is aborted until ``ROLLBACK TO SAVEPOINT``, so the role
    is read right after that rollback, in the same transaction, and a call that left the
    role is still refused (a switch the rollback undoes is the screen's to refuse)."""
    from src.tools.routines.proof_targets import RoleChanged

    client = FakeDataApi(fail_on={'"public"."f"(': "22012"}, role_reset_on=('"public"."f"(',))
    target, call = _one_call(client, "NULL")
    with pytest.raises(RoleChanged):
        target.run(call)
    sent = [s.sql for s in client.sent]
    back = sent.index("ROLLBACK TO SAVEPOINT modernizer_call")
    assert sent[back + 1] == "SELECT current_user::text, session_user::text"
    target.end_call()
    target.close()


def test_a_call_that_keeps_the_runner_role_passes_the_check():
    client = FakeDataApi(calls={'"public"."f"': [7]})
    target, call = _one_call(client, "NULL")
    assert target.run(call).rows == [[7]]
    target.end_call()
    target.close()


def test_a_changed_session_user_is_refused_too():
    """SET SESSION AUTHORIZATION changes session_user: the check compares it with the
    session user read when the call began."""
    from src.tools.routines.proof_targets import RoleChanged

    client = FakeDataApi(calls={'"public"."f"': [1]}, session_reset_on=('"public"."f"(',))
    target, call = _one_call(client, "NULL")
    with pytest.raises(RoleChanged):
        target.run(call)
    target.end_call()
    target.close()


def test_a_role_change_refuses_the_routine_and_the_call_is_not_counted():
    """Load and run: the routine is refused (role_changed) and that call counts neither as
    run nor as succeeded; the next routine still runs."""
    from src.tools.routines.comparison import run_routines

    client = FakeDataApi(calls={'"public"."f"': [1], '"public"."g"': [1]},
                         role_reset_on=('"public"."g"(',))  # fmt: skip
    g = _routine("public.g(integer)", "public.g", FN.replace("public.f(", "public.g("))
    runs = {r.routine_id: r for r in run_routines(
        [g, _routine("public.f(integer)", "public.f", FN)], TABLES, target=_factory(client),
        dialect="postgresql")}  # fmt: skip
    refused = runs["public.g(integer)"]
    assert (refused.outcome, refused.reason_code) == ("not_run", "role_changed")
    assert refused.calls_run == 0 and refused.calls_succeeded == 0
    assert "changed the role it runs as" in refused.reason
    assert sum('"public"."g"(' in q for q in client.statements()) == 1  # never run again
    assert runs["public.f(integer)"].outcome == "ran"
    assert not client.databases


def test_a_role_change_on_the_target_side_is_not_compared():
    """Side by side: a call that left the runner role on the test cluster ends the
    routine's comparison as role_changed, never as a difference or a match."""
    from src.tools.routines.comparison import compare_routines

    client = FakeDataApi(calls={'"public"."f"': [1]}, role_reset_on=('"public"."f"(',))
    source = FakeDataApi(calls={'"public"."f"': [1]})
    report = compare_routines([_routine("public.f(integer)", "public.f", FN)], TABLES,
                              source=_factory(source), target=_factory(client), dialect="postgresql")  # fmt: skip
    (one,) = report.results
    assert (one.outcome, one.reason_code) == ("could_not_compare", "role_changed")
    assert "changed the role it runs as" in one.reason


# --------------------------------------------------------------------------- PostgreSQL runner (#2352)


def _pg_runner(client: FakeDataApi, **kw: Any) -> Any:
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    secrets = client.secrets_store or FakeSecrets()
    client.secrets_store = secrets
    return _mod("data_api_target").create_runner(client, secrets, **ARNS, job_id=JOB,
                                                 admin_database="routine_proof",
                                                 engine="postgresql", **kw)  # fmt: skip


def test_the_postgresql_runner_role_and_its_secret_are_made_and_both_removed():
    """#2352: the runner is a login role created from a SCRAM-SHA-256 verifier computed
    here, so its plain password is in no statement text or parameter and only in its own
    secret, tagged with the job id; the master user creates it and is made its member (to
    hand it the scratch schemas); the Data API signs in as it once, through that secret, and
    reads it back as both current_user and session_user; dropping it drops the role and
    deletes the secret with no recovery window."""
    from src.tools.routines.postgresql_auth import VERIFIER, matches

    dat = _mod("data_api_target")
    client = FakeDataApi()
    tags = [{"Key": "modernizer:job-id", "Value": JOB}]
    runner = _pg_runner(client, tags=tags)
    secrets = client.secrets_store
    assert runner.engine == "postgresql" and runner.username.startswith(dat.runner_prefix(JOB))
    assert runner.username in client.roles and runner.username in client.logins
    stored = secrets.secrets[runner.secret_arn]
    password = json.loads(stored["SecretString"])["password"]
    assert stored["Tags"] == tags and stored["Name"] == dat.runner_secret_name(JOB, runner.username)
    create = next(s for s in client.sent if s.sql.startswith("CREATE ROLE"))
    head = (f'CREATE ROLE "{runner.username}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10 VALID UNTIL '")  # fmt: skip
    assert create.sql.startswith(head)
    until = create.sql[len(head) :].split("'", 1)[0]
    head += until + "' PASSWORD E'"
    assert create.sql.startswith(head) and create.sql.endswith("'") and create.parameters == []
    verifier = create.sql[len(head) : -1].replace("\\x3a", ":")
    assert VERIFIER.fullmatch(verifier) and matches(password, verifier)
    assert create.secret == ARNS["secret_arn"] and create.database == "routine_proof"
    grant = next(s for s in client.sent if s.sql.startswith("GRANT"))
    assert grant.sql == f'GRANT "{runner.username}" TO CURRENT_USER'
    assert grant.secret == ARNS["secret_arn"]
    assert all(password not in s.sql and password not in json.dumps(s.parameters)
               for s in client.sent)  # fmt: skip
    who = [s for s in client.sent if s.sql == "SELECT current_user::text, session_user::text"]
    assert len(who) == 1 and who[0].secret == runner.secret_arn
    assert who[0].database == "routine_proof"
    assert dat.drop_runner(client, secrets, **ARNS, admin_database="routine_proof",
                           account=runner) == []  # fmt: skip
    assert client.sent[-1].sql == f'DROP ROLE IF EXISTS "{runner.username}"'
    assert (
        not client.roles and not secrets.secrets and secrets.deleted == [(runner.secret_arn, True)]
    )


def test_the_postgresql_runner_is_granted_its_scratch_database_only_and_runs_as_itself():
    """#2352: each scratch database grants the runner CONNECT and TEMPORARY on it and hands
    it the public schema; every load, CREATE and call there is sent with the runner's own
    secret, never under SET LOCAL ROLE; the master user only creates, grants, hands over the
    schema and drops; each call reads the runner back as current_user and session_user."""
    client = FakeDataApi(calls={'"public"."f"': [7]})
    runner = _pg_runner(client, tags=[])
    t = _factory(client, runner=runner)()
    t.load_setup(ProofSetup(dialect="postgresql", ddl=('CREATE TABLE "t" ("id" integer PRIMARY KEY)',),
                            rows=(("t", ({"id": 1},)),), tables=("public.t",)))  # fmt: skip
    assert t.create(de.SourceRoutine("public.f", "function", FN)) is None
    t.prepare_calls()
    t.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    assert t.run(ProofCall("f", "baseline", "call", "public.f", "function", ("5",), ("IN",),
                           ("integer",))).rows == [[7]]  # fmt: skip
    t.end_call()
    role = f'"{runner.username}"'
    master = [s.sql for s in client.sent if s.op == "execute" and s.secret == ARNS["secret_arn"]
              and s.database == t.database]  # fmt: skip
    harness = '"modernizer_harness"'
    assert [m for m in master if m != PIN] == [
        f"CREATE SCHEMA {harness}",
        f"REVOKE ALL ON SCHEMA {harness} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {harness} TO {role}",
        f'CREATE TABLE {harness}."modernizer_sentinel" (id pg_catalog.int4 PRIMARY KEY)',
        f'GRANT SELECT, INSERT ON {harness}."modernizer_sentinel" TO {role}',
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_available_extensions WHERE name = :p0",
        f"ALTER SCHEMA public OWNER TO {role}",
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
    ]
    grants = [s.sql for s in client.sent if s.sql.startswith("GRANT")]
    assert grants == [f"GRANT {role} TO CURRENT_USER",
                      f'GRANT CONNECT, TEMPORARY ON DATABASE "{t.database}" TO {role}',
                      f"GRANT USAGE ON SCHEMA {harness} TO {role}",
                      f'GRANT SELECT, INSERT ON {harness}."modernizer_sentinel" TO {role}']  # fmt: skip
    work = [s for s in client.sent if s.op in ("execute", "batch") and s.database == t.database
            and s.sql not in master]  # fmt: skip
    assert any(s.sql.startswith("CREATE OR REPLACE FUNCTION public.f") for s in work)
    assert any(s.op == "batch" for s in work) and any('"public"."f"(' in s.sql for s in work)
    assert all(s.secret == runner.secret_arn for s in work)
    assert not [s for s in client.sent if "SET LOCAL ROLE" in s.sql or "SET ROLE" in s.sql]
    t.close()
    dat = _mod("data_api_target")
    assert dat.drop_runner(client, client.secrets_store, **ARNS, admin_database="routine_proof",
                           account=runner) == []  # fmt: skip
    assert not client.roles and not client.databases and not client.secrets_store.secrets


def test_a_postgresql_runner_role_is_dropped_only_once_its_scratch_databases_are_gone():
    """A role that still owns objects in a database cannot be dropped: dropping it while a
    scratch database is there is named in what is left, and works once it is gone."""
    dat = _mod("data_api_target")
    client = FakeDataApi()
    runner = _pg_runner(client, tags=[])
    t = _factory(client, runner=runner)()
    left = dat.drop_runner(client, client.secrets_store, **ARNS, admin_database="routine_proof",
                           account=runner)  # fmt: skip
    assert left == [f"role {runner.username}"] and runner.username in client.roles
    t.close()
    assert dat.drop_runner(client, client.secrets_store, **ARNS, admin_database="routine_proof",
                           account=runner) == []  # fmt: skip
    assert not client.roles


def test_a_postgresql_runner_the_data_api_cannot_sign_in_as_is_removed_with_its_secret():
    """#2352: when the Data API cannot sign in as the new role, the role is dropped and its
    secret deleted, and nothing goes on as the master user."""
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    client = FakeDataApi(secrets=FakeSecrets(), sign_in_fails=True)
    with pytest.raises(dat.RunnerNotCreated) as refused:
        _pg_runner(client, tags=[])
    assert refused.value.left == [] and refused.value.sqlstate == "28P01"
    assert "could not sign in as the runner role" in str(refused.value)
    assert not client.roles and not client.secrets_store.secrets
    assert client.secrets_store.deleted[0][1] is True


def test_a_postgresql_sign_in_that_reports_another_role_is_no_runner():
    from tests.unit.tools.routines.fake_data_api import MASTER, FakeSecrets

    dat = _mod("data_api_target")
    client = FakeDataApi(secrets=FakeSecrets(), reports_user=MASTER)
    with pytest.raises(dat.RunnerNotCreated) as refused:
        _pg_runner(client, tags=[])
    assert "another role than the runner" in str(refused.value) and refused.value.left == []
    assert not client.roles and not client.secrets_store.secrets


def test_the_fake_signs_in_as_a_scram_role_only_with_its_password():
    """The fake checks a PostgreSQL sign-in as the server does: the role must exist with
    LOGIN and the secret's password must match its SCRAM verifier, or 28P01."""
    from src.tools.routines.postgresql_auth import scram_verifier, verifier_literal
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    secrets = FakeSecrets()
    client = FakeDataApi(secrets=secrets)
    client.execute_statement(sql=f'CREATE ROLE "r" LOGIN PASSWORD {verifier_literal(scram_verifier("right"))}',
                             secretArn=ARNS["secret_arn"])  # fmt: skip
    good = secrets.create_secret(
        Name="g", SecretString=json.dumps({"username": "r", "password": "right"})
    )
    bad = secrets.create_secret(
        Name="b", SecretString=json.dumps({"username": "r", "password": "wrong"})
    )
    got = client.execute_statement(sql="SELECT current_user::text, session_user::text",
                                   secretArn=good["ARN"])  # fmt: skip
    assert [f["stringValue"] for f in got["records"][0]] == ["r", "r"]
    with pytest.raises(Exception, match="28P01"):
        client.execute_statement(sql="SELECT 1", secretArn=bad["ARN"])
    client.execute_statement(sql='CREATE ROLE "n" NOLOGIN', secretArn=ARNS["secret_arn"])
    no_login = secrets.create_secret(
        Name="n", SecretString=json.dumps({"username": "n", "password": "x"})
    )
    with pytest.raises(Exception, match="28P01"):
        client.execute_statement(sql="SELECT 1", secretArn=no_login["ARN"])
    # A verifier whose colons are left as they are reads as a parameter mark: refused. The
    # salt is fixed so its base64 starts with a letter ("QUFB..."), which is what makes the
    # colon before it a parameter mark; a random salt made this pass by chance (#2449).
    fixed = scram_verifier("right", salt=b"A" * 16)
    with pytest.raises(Exception, match="missing parameter"):
        client.execute_statement(sql=f"CREATE ROLE \"x\" LOGIN PASSWORD '{fixed}'",
                                 secretArn=ARNS["secret_arn"])  # fmt: skip


def test_the_sweep_drops_a_killed_runs_postgresql_runner_roles_and_secrets_and_nothing_else():
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    mine, theirs = dat.runner_prefix(JOB) + "0f0f0f0f", dat.runner_prefix("job2") + "0f0f0f0f"
    client, secrets = FakeDataApi(), FakeSecrets()
    client.roles |= {mine, theirs, "proofadmin"}
    for job, user in ((JOB, mine), ("job2", theirs)):
        secrets.create_secret(Name=dat.runner_secret_name(job, user), SecretString="{}")
    dropped, left = dat.sweep_runners(client, secrets, **ARNS, job_id=JOB,
                                      admin_database="routine_proof", engine="postgresql")  # fmt: skip
    assert left == [] and client.roles == {theirs, "proofadmin"}
    assert f'DROP ROLE IF EXISTS "{mine}"' in client.statements()
    assert [v["Name"] for v in secrets.secrets.values()] == [dat.runner_secret_name("job2", theirs)]
    assert set(dropped) == {f"role {mine}", f"secret {dat.runner_secret_name(JOB, mine)}"}


def test_a_postgresql_sign_in_whose_session_user_is_another_role_is_no_runner():
    """The sign-in check reads both: a current_user of the runner under another session user
    (a SET ROLE from the master's session, say) is no sign-in as the runner."""
    from tests.unit.tools.routines.fake_data_api import MASTER, FakeSecrets

    dat = _mod("data_api_target")
    client = FakeDataApi(secrets=FakeSecrets(), reports_session=MASTER)
    with pytest.raises(dat.RunnerNotCreated, match="another role than the runner"):
        _pg_runner(client, tags=[])
    assert not client.roles and not client.secrets_store.secrets


def test_a_postgresql_target_that_is_not_signed_in_as_its_runner_runs_no_call():
    """Before its first call, a target reads current_user and session_user in the call's
    transaction; any other role than its runner stops it before the call runs."""
    from src.tools.routines.proof_targets import ProofTargetError
    from tests.unit.tools.routines.fake_data_api import MASTER

    client = FakeDataApi(calls={'"public"."f"': [1]})
    target = _factory(client)()
    target.load_setup(ProofSetup(dialect="postgresql", ddl=(), rows=()))
    target.create(de.SourceRoutine("public.f", "function", FN))
    target.prepare_calls()
    client.reports_session = MASTER
    with pytest.raises(ProofTargetError, match="did not sign in as the runner role"):
        target.begin_call(
            __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
        )
    assert not [s for s in client.sent if '"public"."f"(' in s.sql]
    target.end_call()
    target.close()


# --------------------------------------------------------------------------- review of #2417

PIN = getattr(de, "PG_PIN", None)  # the harness path, row_security off (#2337); None on main


def _whole_target(client: FakeDataApi) -> tuple[Any, Any]:
    """A target through its whole life: source schemas, rows, a routine, strict check, a call."""
    runner = _pg_runner(client, tags=[])
    t = _factory(client, runner=runner)()
    t.load_setup(ProofSetup(dialect="postgresql", schemas=("sales",),
                            ddl=('CREATE TABLE "t" ("id" integer PRIMARY KEY)',),
                            rows=(("t", ({"id": 1},)),), tables=("public.t",)))  # fmt: skip
    t.create(de.SourceRoutine("public.f", "function", FN))
    t.prepare_calls()
    t.begin_call(
        __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").UTC), 0.5
    )
    t.run(ProofCall("f", "baseline", "call", "public.f", "function", ("5",), ("IN",), ("integer",)))
    t.end_call()
    t.close()
    return t, runner


def test_no_master_statement_runs_in_a_scratch_database_after_the_runner_loads_into_it():
    """Review of #2417: once the runner has put anything into a scratch database, the master
    user runs nothing there, so it can never call a function the runner made (an extension
    script, say, resolving a built-in to a runner function that out-matches it). The strict
    check's extension is created right after the database, before the schemas are handed over."""
    client = FakeDataApi(plpgsql_check=True, calls={'"public"."f"': [1]})
    t, runner = _whole_target(client)
    scratch = [s for s in client.sent if s.op in ("execute", "batch") and s.database == t.database]
    first = next(i for i, s in enumerate(scratch) if s.secret == runner.secret_arn)
    assert [s.sql for s in scratch[first:] if s.secret == ARNS["secret_arn"]] == []
    master = [s.sql for s in scratch[:first] if s.sql != PIN]
    extension = master.index(
        'CREATE EXTENSION IF NOT EXISTS plpgsql_check SCHEMA "modernizer_harness"'
    )
    assert extension < master.index(f'ALTER SCHEMA public OWNER TO "{runner.username}"')
    assert any(m.startswith('CREATE SCHEMA "sales"') for m in master)


def test_every_master_statement_in_a_scratch_database_runs_with_its_search_path_pinned():
    """Review of #2417: each master statement in a scratch database runs in a transaction
    whose first statement pins ``search_path`` to ``pg_catalog, pg_temp``, so no name resolves to a
    schema the runner owns."""
    client = FakeDataApi(plpgsql_check=True, calls={'"public"."f"': [1]})
    t, _ = _whole_target(client)
    master = [s for s in client.sent if s.op == "execute" and s.database == t.database
              and s.secret == ARNS["secret_arn"] and s.sql != PIN]  # fmt: skip
    assert len(master) >= 6
    for s in master:
        assert s.transaction is not None, s.sql
        first = next(x for x in client.sent if x.op == "execute" and x.transaction == s.transaction)
        assert first.sql == PIN, s.sql


def test_a_scratch_database_is_closed_to_public():
    """Review of #2417: PUBLIC loses its default CONNECT and TEMPORARY on each scratch
    database, before the runner is granted them, so another role cannot connect to it."""
    client = FakeDataApi()
    runner = _pg_runner(client, tags=[])
    t = _factory(client, runner=runner)()
    sent = [s.sql for s in client.sent]
    revoke = sent.index(f'REVOKE ALL ON DATABASE "{t.database}" FROM PUBLIC')
    assert revoke < sent.index(
        f'GRANT CONNECT, TEMPORARY ON DATABASE "{t.database}" TO "{runner.username}"'
    )
    t.close()


@pytest.mark.parametrize("engine", ["postgresql", "mysql"])
def test_the_sweep_never_drops_another_jobs_runner_even_when_the_listing_returns_it(engine):
    """Review of #2417: the sweep checks each listed name against this job's prefix itself,
    not only in the SQL filter: a listing that returns another job's runner (the fake here
    ignores the filter) still drops only this job's."""
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    mine, theirs = dat.runner_prefix(JOB) + "0f0f0f0f", dat.runner_prefix("job2") + "0f0f0f0f"
    client = FakeDataApi(engine, list_ignores_prefix=True)
    (client.roles if engine == "postgresql" else client.users).update({mine, theirs})
    dropped, left = dat.sweep_runners(client, FakeSecrets(), **ARNS, job_id=JOB,
                                      admin_database="routine_proof", engine=engine)  # fmt: skip
    kept = client.roles if engine == "postgresql" else client.users
    assert theirs in kept and mine not in kept and left == []
    assert not [q for q in client.statements() if theirs in q and q.startswith("DROP")]


def test_the_postgresql_runner_role_takes_few_connections_and_expires_on_its_own():
    """Review of #2417: the role takes at most 10 connections and its password stops working
    a few hours after the run starts, so a role a failed DROP ROLE left is soon unusable."""
    import datetime as dt

    dat = _mod("data_api_target")
    # The fake checks the sign-in against the real clock, so the run starts now (#2423).
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    client = FakeDataApi()
    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    client.secrets_store = FakeSecrets()
    dat.create_runner(client, client.secrets_store, **ARNS, job_id=JOB, admin_database="routine_proof",
                      tags=[], engine="postgresql", now=now)  # fmt: skip
    create = next(s.sql for s in client.sent if s.sql.startswith("CREATE ROLE"))
    assert " CONNECTION LIMIT 10 " in create
    until = now + dt.timedelta(hours=dat.RUNNER_VALID_HOURS)
    assert f" VALID UNTIL '{until:%Y-%m-%d %H:%M:%S}+00' " in create


def test_a_postgresql_runner_past_its_valid_until_cannot_sign_in():
    """The fake refuses a sign-in after VALID UNTIL, as PostgreSQL does: a role created long
    before is no runner."""
    import datetime as dt

    from tests.unit.tools.routines.fake_data_api import FakeSecrets

    dat = _mod("data_api_target")
    client = FakeDataApi(secrets=FakeSecrets())
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=dat.RUNNER_VALID_HOURS + 1)
    with pytest.raises(dat.RunnerNotCreated, match="could not sign in"):
        dat.create_runner(client, client.secrets_store, **ARNS, job_id=JOB,
                          admin_database="routine_proof", tags=[], engine="postgresql",
                          now=long_ago)  # fmt: skip
    assert not client.roles and not client.secrets_store.secrets


def test_a_mysql_routine_naming_the_runner_as_definer_loads_on_the_test_cluster_as_on_main():
    """Review of #2433: the carried MySQL routines name the run's runner as their DEFINER.
    The test cluster creates them as the runner, as it always did; making an account for a
    DEFINER is the disposable source engine's alone."""
    client = FakeDataApi("mysql")
    runner = _runner()
    t = _factory(client, engine="mysql", runner=runner)()
    t.load_setup(
        ProofSetup(dialect="mysql", ddl=("CREATE TABLE `t` (`id` int PRIMARY KEY)",), rows=())
    )
    user = runner.definer.split("@")[0]
    text = f"CREATE DEFINER=`{user}`@`%` FUNCTION f() RETURNS int RETURN 1"
    assert t.create(de.SourceRoutine("f", "function", text)) is None
    assert getattr(t, "routine_notes", {}) == {}  # no note: it ran as the runner
    t.close()
    sent = [s for s in client.sent if s.op in ("execute", "batch") and s.sql == text]
    assert sent and all(s.secret == RUNNER_ARN for s in sent)
    assert not any("CREATE USER" in s.sql for s in client.sent if s.op == "execute")
