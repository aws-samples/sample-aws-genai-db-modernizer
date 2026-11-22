# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine proof's harness SQL never runs a function, an operator or a relation the
routines define, and a call cannot change what the harness reads unseen (#2337).

The runner role owns ``public`` (on the Aurora test cluster's scratch database and on the
disposable engine), so a carried routine, or one a call made, can plant a better-matching
overload of a built-in (``json_populate_record`` of a table's row type, ``setseed(numeric)``),
an ``=`` operator, or a temporary view named like a catalog view or a table. Each harness
statement runs with ``search_path`` pinned to ``pg_catalog, pg_temp`` (PostgreSQL's
CVE-2018-1058 guidance) and ``row_security`` off, names every built-in with ``pg_catalog.``
and every table with its schema, and the harness's own objects live in a schema the
superuser owns. A cast (``pg_cast`` is global) or a row-level security policy a call made
refuses the routine (``harness_tampered``) instead of changing the read.

The live tests run :class:`DataApiTarget` against a local PostgreSQL through
:class:`LocalDataApi` (no AWS call) and the disposable engine's own target; each fails on
main's code. The fake-client tests run everywhere."""

from __future__ import annotations

import contextlib
import datetime
import re
from collections.abc import Iterator
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines import data_api_target as dat
from src.tools.routines.proof_inputs import ProofCall, ProofSetup
from src.tools.routines.proof_targets import DisposableEngineTarget
from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets
from tests.unit.tools.routines.test_data_api_target import ARNS, JOB, _factory

needs_pg = pytest.mark.skipif(
    "16" not in de.available_majors("postgresql"), reason="pgserver (PostgreSQL 16) is needed"
)

_SETUP = ProofSetup(
    "postgresql",
    (
        "CREATE TYPE mood AS ENUM ('a', 'b')",
        "CREATE TABLE t (id integer PRIMARY KEY, n serial, m mood NOT NULL DEFAULT 'a')",
    ),
    (("t", ({"id": 1}, {"id": 2})),),
    ("public.t",),
)
_ROWS = [{"id": 1, "n": 1, "m": "a"}, {"id": 2, "n": 2, "m": "a"}]
_ROW_3 = {"id": 3, "n": 3, "m": "a"}

#: Objects that beat what an unqualified or unpinned harness statement means: each raises, so
#: a harness statement that reached one fails. Each of them alone fails the test on main.
_PLANTED = (
    "CREATE FUNCTION public.json_populate_record(base public.t, from_json json) RETURNS public.t "
    "LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'HIJACKED json_populate_record'; END$$",
    "CREATE FUNCTION public.hijacked_eq(oid, regclass) RETURNS boolean LANGUAGE plpgsql AS "
    "$$BEGIN RAISE EXCEPTION 'HIJACKED ='; END$$",
    "CREATE OPERATOR public.= (LEFTARG = oid, RIGHTARG = regclass, FUNCTION = public.hijacked_eq)",
)
#: A call that leaves temporary views named like what the harness reads, and inserts row 3.
_LEAVE_TEMP = (
    "CREATE FUNCTION public.leave_temp() RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
    "INSERT INTO public.t (id) VALUES (3); "
    "CREATE TEMP VIEW pg_sequences AS SELECT 'hijacked'::name AS schemaname, "
    "'seq'::name AS sequencename, 1::bigint AS last_value, 1::bigint AS start_value; "
    "CREATE TEMP VIEW pg_tables AS SELECT 'public'::name AS schemaname, 'hijacked'::name AS tablename; "
    "CREATE TEMP VIEW pg_settings AS SELECT 'hijacked'::text AS name, 'x'::text AS setting, "
    "'session'::text AS source; "
    "CREATE TEMP VIEW t AS SELECT 1 AS id, 1 AS n, 'a'::mood AS m "
    "UNION ALL SELECT 2, 2, 'a'::mood; "
    "CREATE TEMP VIEW modernizer_sentinel AS SELECT 0 AS id WHERE false; "
    "RETURN 1; END$$"
)
#: A call that makes a cast the Data API's ``::text`` read would run (pg_cast is global).
_PLANT_CAST = (
    "CREATE FUNCTION public.forge(public.mood) RETURNS text LANGUAGE sql AS $$ SELECT 'FORGED' $$",
    "CREATE FUNCTION public.plant_cast() RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
    "CREATE CAST (public.mood AS text) WITH FUNCTION public.forge(public.mood); RETURN 1; END$$",
)
#: A call that inserts row 3 and hides it behind a forced row-level security policy.
_HIDE_ROW = (
    "CREATE FUNCTION public.hide_row() RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
    "INSERT INTO public.t (id) VALUES (3); "
    "ALTER TABLE public.t ENABLE ROW LEVEL SECURITY; ALTER TABLE public.t FORCE ROW LEVEL SECURITY; "
    "CREATE POLICY hide ON public.t USING (id <> 3); RETURN 1; END$$"
)
#: ``SELECT setseed(%s)`` with a numeric literal reaches this one over pg_catalog's float8.
_SETSEED = (
    "CREATE FUNCTION public.setseed(numeric) RETURNS void LANGUAGE plpgsql AS "
    "$$BEGIN RAISE EXCEPTION 'HIJACKED setseed'; END$$"
)


def _tampered() -> Any:
    """``HarnessTampered``, looked up when a test runs, so this file imports on main too."""
    from src.tools.routines import proof_targets

    return proof_targets.HarnessTampered


def _frozen() -> datetime.datetime:
    return datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)


def _call(name: str) -> ProofCall:
    return ProofCall("r", "r", "call", name, "function")


@contextlib.contextmanager
def _data_api_target() -> Iterator[tuple[Any, Any]]:
    """A Data API target on a local PostgreSQL, set up, and a way to plant as the runner."""
    import psycopg2

    from tests.pg_server import start_pg_server
    from tests.unit.tools.routines.local_data_api import LocalDataApi

    server = start_pg_server()
    secrets = FakeSecrets()
    client = LocalDataApi(server, secrets)
    target = None
    try:
        arns = {"cluster_arn": ARNS["cluster_arn"], "secret_arn": ARNS["secret_arn"]}
        runner = dat.create_runner(client, secrets, cluster_arn=arns["cluster_arn"],
                                   secret_arn=arns["secret_arn"], job_id=JOB,
                                   admin_database="postgres", tags=[], engine="postgresql")  # fmt: skip
        target = dat.DataApiTarget(client, cluster_arn=arns["cluster_arn"],
                                   secret_arn=arns["secret_arn"], engine="postgresql", version="16",
                                   job_id=JOB, admin_database="postgres", runner=runner)  # fmt: skip
        target.load_setup(_SETUP)

        def plant(statements: tuple[str, ...]) -> None:
            conn = psycopg2.connect(host=str(server.workdir), dbname=target.database,
                                    user=runner.username)  # fmt: skip
            try:
                with conn, conn.cursor() as cur:
                    for statement in statements:
                        cur.execute(statement)
            finally:
                conn.close()

        yield target, plant
    finally:
        with contextlib.suppress(Exception):
            if target is not None:
                target.close()
        client.close_all()
        server.cleanup()


@contextlib.contextmanager
def _local_target() -> Iterator[tuple[Any, Any]]:
    """The disposable engine's own target, set up, and a way to plant as its runner."""
    target = DisposableEngineTarget("postgresql", "16")
    try:
        target.load_setup(_SETUP)

        def plant(statements: tuple[str, ...]) -> None:
            for statement in statements:
                got = de.execute(target._handle, statement)
                assert got.error is None, got.error

        yield target, plant
    finally:
        target.close()


_TARGETS = [pytest.param(_data_api_target, id="data_api"), pytest.param(_local_target, id="local")]


def _skip_as_root() -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("the disposable engine refuses root")


@needs_pg
@pytest.mark.parametrize("opened", _TARGETS)
def test_a_planted_overload_operator_or_temporary_view_never_reaches_a_harness_read(opened):
    _skip_as_root()
    with opened() as (target, plant):
        plant((*_PLANTED, _LEAVE_TEMP))
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        before = target.session_state()
        assert target.snapshot(["t"]) == {"t": _ROWS}
        result = target.run(_call("leave_temp"))
        assert result.error is None, result.error
        assert target.tables() == ["t"]
        assert target.snapshot(["t"]) == {"t": [*_ROWS, _ROW_3]}  # not the temporary view
        assert target.column_types(["t"])["t"]["id"] == "integer"
        assert target.transaction_state() == "open"
        assert target.session_state() == before
        assert target.counter_state() == {"public.t_n_seq": "3"}
        target.end_call()
        # A row change: its row is read by pg_catalog's json_populate_record.
        target.begin_call(_frozen(), 0.5)
        row = ProofCall("r", "r", "row_change", "t", "INSERT", (3,), columns=("id",))
        assert target.run(row).error is None
        assert target.snapshot(["t"]) == {"t": [*_ROWS, _ROW_3]}
        target.end_call()
        assert target.snapshot(["t"]) == {"t": _ROWS}


@needs_pg
@pytest.mark.parametrize("opened", _TARGETS)
def test_a_row_level_security_policy_a_call_made_refuses_the_routine(opened):
    _skip_as_root()
    with opened() as (target, plant):
        plant((_HIDE_ROW,))
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        assert target.run(_call("hide_row")).error is None
        with pytest.raises(_tampered()) as raised:
            target.snapshot(["t"])  # never the baseline rows with row 3 hidden
        assert raised.value.sqlstate == "42501"
        target.end_call()


@needs_pg
def test_a_cast_a_call_made_refuses_the_data_api_read():
    _skip_as_root()
    with _data_api_target() as (target, plant):
        plant(_PLANT_CAST)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        assert target.run(_call("plant_cast")).error is None
        with pytest.raises(_tampered()):
            target.snapshot(["t"])  # never 'FORGED' for every mood
        target.end_call()
        assert target.snapshot(["t"]) == {"t": _ROWS}  # the rollback took the cast away


@needs_pg
@pytest.mark.passes_on_main  # main has no cast check to refuse it; this guards the deptype filter
def test_a_range_type_a_call_made_is_no_tampering_on_the_data_api():
    """A range type brings its own cast to its multirange (``deptype 'i'``), which runs no
    function a routine wrote: the read is not refused."""
    _skip_as_root()
    with _data_api_target() as (target, plant):
        plant(("CREATE FUNCTION public.make_range() RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
               "CREATE TYPE public.r AS RANGE (subtype = int4); RETURN 1; END$$",))  # fmt: skip
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        assert target.run(_call("make_range")).error is None
        assert target.snapshot(["t"]) == {"t": _ROWS}
        target.end_call()


@needs_pg
def test_a_setseed_overload_in_public_never_takes_the_harness_seed():
    """On the disposable engine the seed goes as a numeric literal, which a ``numeric``
    overload would win (the Data API binds a ``float8``, an exact match already)."""
    _skip_as_root()
    with _local_target() as (target, plant):
        plant((_SETSEED,))
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)  # pg_catalog.setseed, whatever the literal's type
        target.end_call()


def test_the_screen_refuses_a_body_that_makes_a_cast_or_a_policy():
    from src.tools.routines.proof_screen import create_refusal

    for body in (
        "CREATE CAST (public.mood AS text) WITH FUNCTION public.forge(public.mood)",
        "CREATE POLICY hide ON public.t USING (id <> 3)",
        "ALTER TABLE public.t FORCE ROW LEVEL SECURITY",
    ):
        text = (f"CREATE FUNCTION public.g() RETURNS integer LANGUAGE plpgsql AS "
                f"$$BEGIN {body}; RETURN 1; END$$")  # fmt: skip
        assert create_refusal(text, "postgresql", "function", "public") is not None, body
    plain = ("CREATE FUNCTION public.g() RETURNS integer LANGUAGE plpgsql AS "
             "$$BEGIN UPDATE public.t SET id = id; RETURN 1; END$$")  # fmt: skip
    assert create_refusal(plain, "postgresql", "function", "public") is None


# --------------------------------------------------------------------------- fake client


def _lifecycle(client: FakeDataApi) -> Any:
    """One PostgreSQL target through every step the comparison takes, on the fake client,
    with an enum argument whose label the harness looks up."""
    t = _factory(client)()
    t.load_setup(ProofSetup("postgresql", ("CREATE TABLE t (id integer PRIMARY KEY)",),
                            (("t", ({"id": 1},)),), ("public.t",)))  # fmt: skip
    t.create(de.SourceRoutine("f", "function", FN_TEXT))
    t.prepare_calls()
    t.begin_call(_frozen(), 0.25)
    t.now()
    t.session_state()
    t.snapshot(["t"])
    t.run(ProofCall("r", "r", "call", "f", "function", (1,), casts=("integer",)))
    t.tables()
    t.snapshot(["t"])
    t.column_types(["t"])
    t.transaction_state()
    t.session_state()
    t.counter_state()
    t.end_call()
    t.begin_call(_frozen(), 0.375)
    t.run(
        ProofCall(
            "r",
            "r",
            "call",
            "f",
            "function",
            ({"enum_label": "last"},),
            casts=(de.pg_cast_type("mood"),),
        )
    )
    t.end_call()
    t.begin_call(_frozen(), 0.5)
    t.run(ProofCall("r", "r", "row_change", "t", "INSERT", (2,), columns=("id",)))
    t.end_call()
    t.tables()
    t.snapshot(["t"])
    t.close()
    return t


FN_TEXT = (
    "CREATE OR REPLACE FUNCTION public.f(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$"
)


def _routine_path_statements(client: FakeDataApi, t: Any) -> list[str]:
    """What the runner sent on the routines' path, or outside any transaction."""
    path: dict[str | None, str] = {}
    found = []
    for s in client.sent:
        if s.op not in ("execute", "batch") or s.secret != t._runner.secret_arn:
            continue
        if s.database != t.database:
            continue
        if s.sql == dat._PIN:
            path[s.transaction] = "harness"
        elif s.sql == dat._bind(de.PG_ROUTINE_PATH, [""])[0]:
            path[s.transaction] = "routine"
        elif s.sql.startswith("SET LOCAL ") or "SAVEPOINT" in s.sql:
            continue
        elif path.get(s.transaction) != "harness":
            found.append(s.sql)
    return found


def test_every_runner_statement_but_the_routines_own_runs_with_the_harness_path_pinned():
    """Walk what the runner sent, with the strict check on and an enum argument: a statement
    outside a transaction, or sent on the routines' path (``public`` first), must be one of
    the routines' own (the setup's ``CREATE TABLE``, a carried ``CREATE``, the call, the row
    change) or one of the two harness statements that plan or name the routines' objects:
    the strict check and the enum label lookup, each naming its function with its schema."""
    client = FakeDataApi(calls={'"f"(': [1]}, plpgsql_check=True, strict={"public.f(integer)": []})
    t = _lifecycle(client)
    found = _routine_path_statements(client, t)
    strict = dat._bind(dat._PG_STRICT, [0, 0])[0]
    assert strict in found  # the walker saw the strict check
    assert any("pg_catalog.enum_last(NULL::" in s for s in found)
    for sql in found:
        assert (
            sql.startswith("CREATE TABLE t ")
            or sql == FN_TEXT
            or sql.startswith('SELECT * FROM "f"(')
            or sql.startswith(
                'INSERT INTO "t" ("id") SELECT "id" FROM pg_catalog.json_populate_record('
            )
            or sql == strict
            or sql.startswith("SELECT pg_catalog.enum_last(NULL::")
        ), sql


def test_the_harness_statements_name_every_function_they_call_with_its_schema():
    """No harness statement calls a function by a bare name: a bare name is the one a
    planted overload can win (``public.quote_ident(name)`` beats ``pg_catalog``'s
    ``quote_ident(text)`` for a ``name`` argument)."""
    client = FakeDataApi(calls={'"f"(': [1]}, plpgsql_check=True, strict={"public.f(integer)": []})
    _lifecycle(client)
    keywords = {"cast", "coalesce", "exists", "in", "values", "primary", "key", "integer",
                "select", "from", "and", "or", "not", "as", "on", "where", "set", "with"}  # fmt: skip
    bare = re.compile(r"(?<![\w.\"])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    own = {FN_TEXT, "CREATE TABLE t (id integer PRIMARY KEY)"}
    found = []
    for s in client.sent:
        if (
            s.op not in ("execute", "batch")
            or s.sql in own
            or s.sql.startswith('SELECT * FROM "f"(')
        ):
            continue
        text = re.sub(r"'[^']*'", "''", s.sql)
        found += [(m, s.sql) for m in bare.findall(text) if m.casefold() not in keywords]
    assert not found, found


def test_the_sentinel_and_the_strict_check_live_in_the_harness_schema_the_master_user_owns():
    client = FakeDataApi(calls={'"f"(': [1]}, plpgsql_check=True, strict={"public.f(integer)": []})
    t = _lifecycle(client)
    master = [
        s.sql for s in client.sent if s.secret == ARNS["secret_arn"] and s.database == t.database
    ]
    schema = f'"{dat.HARNESS_SCHEMA}"'
    assert f"CREATE SCHEMA {schema}" in master
    assert f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC" in master
    assert f'GRANT USAGE ON SCHEMA {schema} TO "{t._runner.username}"' in master
    assert f"CREATE EXTENSION IF NOT EXISTS plpgsql_check SCHEMA {schema}" in master
    sentinel = f'{schema}."modernizer_sentinel"'
    assert any(m.startswith(f"CREATE TABLE {sentinel} ") for m in master)
    assert f'GRANT SELECT, INSERT ON {sentinel} TO "{t._runner.username}"' in master
    runner = [s.sql for s in client.sent if s.secret == t._runner.secret_arn]
    assert not [r for r in runner if "modernizer_sentinel" in r and sentinel not in r]
    assert any(f"{schema}.plpgsql_check_function_tb(" in r for r in runner)


def test_a_source_schema_named_as_the_harness_schema_is_refused():
    from src.tools.routines.proof_targets import ProofTargetError

    t = _factory(FakeDataApi())()
    try:
        with pytest.raises(ProofTargetError, match="harness"):
            t._create_schemas([dat.HARNESS_SCHEMA])
    finally:
        t.close()


@needs_pg
def test_a_call_that_changes_what_the_harness_reads_refuses_the_routine():
    """A target whose read after the call is refused (a policy, a cast) refuses the routine
    as ``harness_tampered``, which needs attention, and never compares the call."""
    _skip_as_root()
    from src.tools.routines.comparison import ComparedRoutine, compare_routines
    from src.tools.routines.proof_inputs import RoutineParameter, RoutineProofInput
    from src.tools.routines.proof_run import _entry_of_comparison
    from src.tools.routines.proof_targets import disposable_target
    from tests.unit.tools.routines.test_data_api_target import TABLES

    HarnessTampered = _tampered()

    class Tampered(DisposableEngineTarget):
        ran = False

        def run(self, call: ProofCall) -> Any:
            self.ran = True
            return super().run(call)

        def snapshot(self, tables: Any) -> Any:
            if self.ran:
                raise HarnessTampered("a policy hid its rows", "42501")
            return super().snapshot(tables)

    text = "CREATE FUNCTION public.f(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT p $$"
    proof = RoutineProofInput(routine_id="public.f(integer)", kind="function", name="f",
                              returns="integer", tables=("public.t",),
                              parameters=(RoutineParameter(name="p", data_type="integer"),))  # fmt: skip
    statement = (de.SourceRoutine("f", "function", text),)
    got = compare_routines([ComparedRoutine(proof, statement, statement)], TABLES,
                           source=disposable_target("postgresql", "16"),
                           target=lambda: Tampered("postgresql", "16"), dialect="postgresql")  # fmt: skip
    [one] = got.results
    assert (one.outcome, one.reason_code) == ("could_not_compare", "harness_tampered"), one.reason
    assert _entry_of_comparison(one).outcome == "needs_attention"


# --------------------------------------------------------------------------- a swapped table

#: The original adds row 1000; the carried one adds 1000 and 1001, then swaps the setup table for
#: a view of other rows, so a read by the table's name sees what the routine says.
_ADD_ROW = (
    "CREATE FUNCTION public.f(p integer) RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
    "INSERT INTO public.t (id) VALUES (1000); RETURN 1; END$$"
)
_SWAP_TABLE = (
    "CREATE FUNCTION public.f(p integer) RETURNS integer LANGUAGE plpgsql AS $$BEGIN "
    "INSERT INTO public.t (id) VALUES (1000), (1001); DROP TABLE public.t; "
    "CREATE VIEW public.t AS SELECT * FROM (VALUES (1), (2), (3)) v(id); RETURN 1; END$$"
)


def _swap_routines(original: str, carried: str) -> list[Any]:
    from src.tools.routines.comparison import ComparedRoutine
    from src.tools.routines.proof_inputs import RoutineParameter, RoutineProofInput

    proof = RoutineProofInput(routine_id="public.f(integer)", kind="function", name="f",
                              returns="integer", tables=("public.t",),
                              parameters=(RoutineParameter(name="p", data_type="integer"),))  # fmt: skip
    return [ComparedRoutine(proof, (de.SourceRoutine("f", "function", original),),
                            (de.SourceRoutine("f", "function", carried),))]  # fmt: skip


@contextlib.contextmanager
def _swap_factory(which: str) -> Iterator[Any]:
    """A target factory: the disposable engine's own, or the Data API on a local server."""
    from src.tools.routines.proof_targets import disposable_target

    if which == "local":
        yield disposable_target("postgresql", "16")
        return
    from tests.pg_server import start_pg_server
    from tests.unit.tools.routines.local_data_api import LocalDataApi

    server = start_pg_server()
    secrets = FakeSecrets()
    client = LocalDataApi(server, secrets)
    try:
        arns = {"cluster_arn": ARNS["cluster_arn"], "secret_arn": ARNS["secret_arn"]}
        runner = dat.create_runner(client, secrets, cluster_arn=arns["cluster_arn"],
                                   secret_arn=arns["secret_arn"], job_id=JOB,
                                   admin_database="postgres", tags=[], engine="postgresql")  # fmt: skip
        yield dat.data_api_target(client, cluster_arn=arns["cluster_arn"],
                                  secret_arn=arns["secret_arn"], engine="postgresql",
                                  version="16", job_id=JOB, admin_database="postgres",
                                  runner=runner, strict_check=["not_available"])  # fmt: skip
    finally:
        client.close_all()
        server.cleanup()


@needs_pg
@pytest.mark.parametrize("which", ["local", "data_api"])
def test_a_setup_table_swapped_for_a_view_refuses_the_routine_side_by_side(which):
    """Without the check the swap reads as no difference, so a wrong routine comes out proved."""
    _skip_as_root()
    from src.tools.routines.comparison import compare_routines
    from src.tools.routines.proof_run import _entry_of_comparison
    from src.tools.routines.proof_targets import disposable_target
    from tests.unit.tools.routines.test_data_api_target import TABLES

    with _swap_factory(which) as target:
        report = compare_routines(_swap_routines(_ADD_ROW, _SWAP_TABLE), TABLES,
                                  source=disposable_target("postgresql", "16"), target=target,
                                  dialect="postgresql")  # fmt: skip
    [one] = report.results
    assert (one.outcome, one.reason_code) == ("could_not_compare", "harness_tampered"), one.reason
    assert _entry_of_comparison(one).outcome == "needs_attention"


@needs_pg
@pytest.mark.parametrize("which", ["local", "data_api"])
def test_a_setup_table_swapped_for_a_view_refuses_the_routine_when_loaded_and_run(which):
    """The load-and-run path (the test cluster's, for routines carried as they are)."""
    _skip_as_root()
    from src.tools.routines.comparison import run_routines
    from src.tools.routines.proof_run import _entry_of_run
    from tests.unit.tools.routines.test_data_api_target import TABLES

    with _swap_factory(which) as target:
        [run] = run_routines(_swap_routines(_SWAP_TABLE, _SWAP_TABLE), TABLES, target=target,
                             dialect="postgresql")  # fmt: skip
    assert (run.outcome, run.reason_code) == ("not_run", "harness_tampered"), run.reason
    assert _entry_of_run(run).outcome == "needs_attention"
