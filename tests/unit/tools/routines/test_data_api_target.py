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
from tests.unit.tools.routines.fake_data_api import FakeDataApi, db_error

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


def _factory(client: FakeDataApi, engine: str = "postgresql", **kw: Any) -> Any:
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
    drops the database and its role."""
    dat = _mod("data_api_target")
    client = FakeDataApi(calls={'"public"."f"': [7]})
    target = _factory(client)()
    name = target.database
    assert name.startswith(dat.scratch_prefix(JOB)) and dat.scratch_name(JOB) != name
    assert (
        client.sent[0].sql == f'CREATE DATABASE "{name}"'
        and client.sent[0].database == "routine_proof"
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
    role = f'"{name}_runner"'
    assert any(
        s.sql == f"SET LOCAL ROLE {role}" and s.transaction == call.transaction for s in client.sent
    )
    admin = [s.sql for s in client.sent if s.op == "execute" and s.database != name]
    assert admin == [f'CREATE DATABASE "{name}"', f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB "
                     "NOCREATEROLE NOREPLICATION NOBYPASSRLS", f"GRANT {role} TO CURRENT_USER",
                     f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f"DROP ROLE IF EXISTS {role}"]  # fmt: skip
    assert client.sent[-2].sql == f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
    assert client.sent[-1].sql == f"DROP ROLE IF EXISTS {role}"
    assert not client.databases and not client.roles


def test_a_mysql_target_uses_its_scratch_database_as_the_namespace():
    client = FakeDataApi("mysql")
    target = _factory(client, engine="mysql")
    t = target()
    assert t.namespace == t.database and client.sent[0].sql == f"CREATE DATABASE `{t.database}`"
    assert not client.roles  # no role switch on MySQL through the Data API
    t.close()
    assert client.sent[-1].sql == f"DROP DATABASE IF EXISTS `{t.database}`" and not client.databases


def test_the_scratch_database_is_dropped_when_its_setup_fails():
    """Drop on failure: a target whose role cannot be made drops the database it made, and
    a load that fails leaves nothing once the side closes."""
    client = FakeDataApi(fail_on={"NOSUPERUSER": "42501"})
    with pytest.raises(Exception, match="scratch database was not created"):
        _factory(client)()
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
    dropped = dat.drop_scratch(
        client, **ARNS, engine="postgresql", job_id=JOB, admin_database="routine_proof"
    )
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
    assert client.sent[-1].sql == "ROLLBACK TO SAVEPOINT modernizer_call"
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
    assert any(s.sql == "CREATE EXTENSION IF NOT EXISTS plpgsql_check" for s in client.sent)

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
    assert client.sent[client.sent.index(checked[0]) + 2].op == "rollback"  # never kept


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
