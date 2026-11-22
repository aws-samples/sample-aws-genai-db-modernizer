# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""On MySQL the routine proof's harness reads only objects no routine can create, replace or
drop, and a call cannot change what the harness reads unseen (#2560, the MySQL half of #2337).

The runner has every privilege in the routines' own database, so a call could drop a table
the harness reads there and put a view of the same name in its place, or hide a base table
behind a ``TEMPORARY`` table of the same name (MySQL reads a temporary table first, in the
session that made it), or leave a session setting that changes how the harness's reads come
back (``time_zone``, the result character set). The sentinel lives in
a harness database the server's admin (on the test cluster the master user) makes and owns,
where the runner may only read and insert the sentinel's rows; a setup table a temporary
table shadows refuses the routine (``harness_tampered``); and the harness puts its own read
settings back after each call. On the Data API, whose pooled sessions have no reset between
calls, every session variable goes back to the run's baseline after and before every call, and
one that does not refuses the routine (#2574).

The live tests run on a disposable ``mysqld``: the disposable engine's own target, and
:class:`DataApiTarget` through :class:`LocalMySqlDataApi` (no AWS call). They are marked
``integration``, since the gated CI run has no ``mysqld``; each fails on main's code unless
it is marked ``passes_on_main``. The fake-client tests run everywhere."""

from __future__ import annotations

import contextlib
import datetime
import functools
import os
from collections.abc import Iterator
from typing import Any

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines import data_api_target as dat
from src.tools.routines.proof_inputs import ProofCall, ProofSetup
from src.tools.routines.proof_targets import DisposableEngineTarget, ProofTargetError
from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets
from tests.unit.tools.routines.test_data_api_target import ARNS, JOB, _factory

needs_mysql = pytest.mark.skipif(not de.available_majors("mysql"), reason="no mysqld binary")

_SETUP = ProofSetup(
    "mysql",
    ("CREATE TABLE t (id INT PRIMARY KEY, ts TIMESTAMP NULL, note VARCHAR(10) NULL) "
     "DEFAULT CHARSET = utf8mb4",),
    (("t", ({"id": 1, "ts": "2024-01-01 00:00:00", "note": "café"},
            {"id": 2, "ts": "2024-01-02 00:00:00", "note": "plain"})),),
    ("src.t",),
)  # fmt: skip
#: Swaps the routines' database's sentinel for a view that counts one row on the call's own
#: connection only (it holds the lock) and none on any other, so on main the harness reads a
#: committed call as still open.
_SWAP_SENTINEL = (
    "CREATE PROCEDURE swap() BEGIN INSERT INTO t (id) VALUES (3); "
    "DROP TABLE IF EXISTS modernizer_sentinel; "
    "CREATE VIEW modernizer_sentinel AS SELECT 1 AS id FROM DUAL "
    "WHERE IS_USED_LOCK('mark') = CONNECTION_ID(); DO GET_LOCK('mark', 0); END"
)
#: Inserts row 3, then hides it behind a temporary copy of the seeded rows, and hides the
#: routines' database's sentinel (which main reads) behind an empty temporary table.
_SHADOW = (
    "CREATE PROCEDURE shadow() BEGIN INSERT INTO t (id) VALUES (3); "
    "CREATE TEMPORARY TABLE t SELECT * FROM t WHERE id <> 3; "
    "CREATE TEMPORARY TABLE modernizer_sentinel (id INT); END"
)
#: Leaves settings that change how the harness's reads come back. MySQL puts a routine's
#: caller's sql_mode back on return; the time zone and the result character set stay set.
_SETTINGS = (
    "CREATE PROCEDURE settings() BEGIN SET time_zone = '+00:00'; "
    "SET character_set_results = 'latin1'; "
    "SET SESSION sql_mode = 'ANSI_QUOTES,NO_BACKSLASH_ESCAPES'; END"
)
#: What a runner may try on the harness's own database (``{h}``) and on the routines' one.
_TAMPERING = (
    "DELETE FROM `{h}`.`modernizer_sentinel`",
    "UPDATE `{h}`.`modernizer_sentinel` SET id = 2",
    "DROP TABLE `{h}`.`modernizer_sentinel`",
    "TRUNCATE TABLE `{h}`.`modernizer_sentinel`",
    "ALTER TABLE `{h}`.`modernizer_sentinel` ADD COLUMN x INT",
    "RENAME TABLE `{h}`.`modernizer_sentinel` TO `{h}`.`moved`",
    "CREATE TABLE `{h}`.`other` (id INT)",
    "CREATE VIEW `{h}`.`v` AS SELECT 1 AS id",
    "CREATE TEMPORARY TABLE `{h}`.`modernizer_sentinel` (id INT)",
    "CREATE TRIGGER tg BEFORE INSERT ON `{h}`.`modernizer_sentinel` FOR EACH ROW SET NEW.id = 9",
    "CREATE PROCEDURE `{h}`.`p`() SELECT 1",
    "CREATE EVENT `{h}`.`e` ON SCHEDULE EVERY 1 HOUR DO SELECT 1",
    "LOCK TABLES `{h}`.`modernizer_sentinel` WRITE",
    "GRANT SELECT ON `{h}`.`modernizer_sentinel` TO CURRENT_USER",
    "CREATE DEFINER = 'root'@'localhost' SQL SECURITY DEFINER VIEW v AS SELECT 1 AS id",
)
#: Access denied: to a table (1142), to a database (1044), for want of a privilege (1227,
#: a DEFINER naming another account), a GRANT with no grant option (1410).
_DENIED = {1142, 1044, 1227, 1410}


def _tampered() -> Any:
    from src.tools.routines import proof_targets

    return proof_targets.HarnessTampered


def _frozen() -> datetime.datetime:
    return datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)


def _call(name: str) -> ProofCall:
    return ProofCall("r", "r", "call", name, "procedure")


def _major() -> str:
    majors = de.available_majors("mysql")
    return "8.0" if "8.0" in majors else majors[0]


def _errno(exc: Exception) -> int | None:
    found = getattr(exc, "errno", None)
    return int(found) if found is not None else None


@contextlib.contextmanager
def _local_target() -> Iterator[tuple[Any, Any]]:
    """The disposable engine's own MySQL target, set up, and a way to run a statement as its
    runner on a connection of its own (``None``, or the error number)."""
    target = DisposableEngineTarget("mysql", _major())
    try:
        target.load_setup(_SETUP)
        backend: Any = target._handle._backend

        def as_runner(sql: str) -> int | None:
            conn = backend._aside()
            try:
                cur = conn.cursor()
                cur.execute(sql)
                if cur.description:
                    cur.fetchall()
                return None
            except backend._mc.Error as exc:
                return _errno(exc)
            finally:
                conn.close()

        yield target, as_runner
    finally:
        target.close()


@contextlib.contextmanager
def _data_api_target(*, pooled: bool = False) -> Iterator[tuple[Any, Any]]:
    """A MySQL Data API target on a disposable ``mysqld``, set up, and the same helper. With
    ``pooled`` the stand-in reuses one session across transactions, as a pooled Data API
    session is reused (#2574)."""
    from tests.unit.tools.routines.local_mysql_data_api import LocalMySqlDataApi

    handle = de.start("mysql", _major())
    secrets = FakeSecrets()
    client = LocalMySqlDataApi(handle, secrets, pooled=pooled)
    target = None
    try:
        runner = dat.create_runner(client, secrets, cluster_arn=ARNS["cluster_arn"],
                                   secret_arn=ARNS["secret_arn"], job_id=JOB,
                                   admin_database=None, tags=[], engine="mysql")  # fmt: skip
        target = dat.DataApiTarget(client, cluster_arn=ARNS["cluster_arn"],
                                   secret_arn=ARNS["secret_arn"], engine="mysql", version="8.0",
                                   job_id=JOB, admin_database=None, runner=runner)  # fmt: skip
        target.load_setup(_SETUP)

        def as_runner(sql: str) -> int | None:
            conn = client.connect(runner.secret_arn, target.database)
            try:
                cur = conn.cursor()
                cur.execute(sql)
                if cur.description:
                    cur.fetchall()
                return None
            except Exception as exc:  # noqa: BLE001 - the error number is the answer
                return _errno(exc)
            finally:
                conn.close()

        yield target, as_runner
    finally:
        with contextlib.suppress(Exception):
            if target is not None:
                target.close()
        client.close_all()
        de.stop(handle)


_TARGETS = [
    pytest.param(_local_target, id="local"),
    pytest.param(_data_api_target, id="data_api"),
    pytest.param(functools.partial(_data_api_target, pooled=True), id="data_api_pooled"),
]


def _skip_as_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("the disposable engine refuses root")


def _plant(as_runner: Any, text: str) -> None:
    """A procedure made as the runner, past the screen: a routine's runtime is the threat."""
    assert as_runner(text) is None


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_the_sentinel_sits_in_a_harness_database_the_runner_cannot_change(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        harness = target.harness_database
        assert harness != target.namespace
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        assert target.transaction_state() == "open"  # the runner writes and reads its row
        target.end_call()
        assert as_runner(f"SELECT COUNT(*) FROM `{harness}`.`modernizer_sentinel`") is None
        refused = {sql.format(h=harness): as_runner(sql.format(h=harness)) for sql in _TAMPERING}
        assert {sql: n for sql, n in refused.items() if n not in _DENIED} == {}
        assert target.tables() == ["t"]  # nothing of the harness's is among the routines' tables


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_a_call_that_swaps_the_sentinel_for_a_view_never_fools_the_transaction_state(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        _plant(as_runner, _SWAP_SENTINEL)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        assert target.run(_call("swap")).error is None
        # Its DDL committed the call's transaction, sentinel row included: never "open".
        assert target.transaction_state() == "committed"
        target.end_call()


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_a_temporary_table_a_call_made_never_stands_in_for_a_harness_read(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        _plant(as_runner, _SHADOW)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        seeded = target.snapshot(["t"])
        assert [r["id"] for r in seeded["t"]] == [1, 2]
        assert target.run(_call("shadow")).error is None
        assert target.transaction_state() == "open"  # the harness's sentinel, not the temporary one
        with pytest.raises(_tampered()):
            target.snapshot(["t"])  # never the seeded rows with row 3 hidden
        target.end_call()


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_session_settings_a_call_left_never_change_a_harness_read(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        _plant(as_runner, _SETTINGS)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        before = target.snapshot(["t"])
        assert len(before["t"]) == 2
        assert target.run(_call("settings")).error is None
        assert target.snapshot(["t"]) == before  # the same instants, the same text
        left = target.session_state()
        if left is not None:  # the comparison still sees what the call changed
            assert (left["time_zone"], left["character_set_results"]) == ("+00:00", "latin1")
        target.end_call()


#: A stored function keeps ``sql_select_limit`` after ``SELECT f()`` (a procedure does not), so
#: on main every read after it, the session's own read included, stops at the seeded rows.
_LIMIT = (
    "CREATE FUNCTION f() RETURNS INT DETERMINISTIC MODIFIES SQL DATA BEGIN "
    "INSERT INTO t (id) VALUES (3); SET SESSION sql_select_limit = 2; RETURN 1; END"
)
#: Settings a function can leave that change what an expression or a sort returns.
_WIDEN = (
    "CREATE FUNCTION widen() RETURNS INT DETERMINISTIC BEGIN SET SESSION group_concat_max_len = 4; "
    "SET SESSION div_precision_increment = 0; SET SESSION sql_auto_is_null = 1; "
    "SET SESSION max_sort_length = 4; SET SESSION max_join_size = 1; "
    "SET SESSION transaction_isolation = 'READ-UNCOMMITTED'; SET SESSION transaction_read_only = 1; "
    "RETURN 1; END"
)
_READ_SETTINGS = (
    "SELECT @@SESSION.sql_select_limit, @@SESSION.group_concat_max_len, "
    "@@SESSION.div_precision_increment, @@SESSION.sql_auto_is_null, @@SESSION.max_sort_length, "
    "@@SESSION.max_join_size, @@SESSION.transaction_isolation, @@SESSION.transaction_read_only"
)


def _function(name: str) -> ProofCall:
    return ProofCall("r", "r", "call", name, "function")


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_a_row_limit_a_function_left_refuses_the_routine(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        _plant(as_runner, _LIMIT)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        with pytest.raises(_tampered()):
            target.run(_function("f"))  # never the seeded rows with row 3 hidden
        # the refused call's session gets the harness's settings back before it is handed back
        kept = de.execute(
            target._handle, "SELECT @@SESSION.sql_select_limit = @@GLOBAL.sql_select_limit"
        )
        assert kept.rows == [[1]]
        target.end_call()


@pytest.mark.integration
@needs_mysql
@pytest.mark.parametrize("opened", _TARGETS)
def test_settings_a_function_left_are_put_back_before_a_harness_read(opened):
    _skip_as_root()
    with opened() as (target, as_runner):
        _plant(as_runner, _WIDEN)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        before = de.execute(target._handle, _READ_SETTINGS).rows
        assert target.run(_function("widen")).error is None
        assert de.execute(target._handle, _READ_SETTINGS).rows == before
        target.end_call()


#: Leaves session settings no harness read depends on, which a pooled Data API session would
#: carry into the next call's routine (#2574). A procedure keeps them after its ``CALL``.
_LEAVE = (
    "CREATE PROCEDURE leave_settings() BEGIN SET SESSION foreign_key_checks = 0; "
    "SET SESSION unique_checks = 0; SET SESSION sql_safe_updates = 1; "
    "SET SESSION innodb_lock_wait_timeout = 1; SET SESSION optimizer_switch = 'index_merge=off'; "
    "DO LAST_INSERT_ID(7); END"
)
_LEFT = (
    "SELECT CONNECTION_ID(), @@SESSION.foreign_key_checks, @@SESSION.unique_checks, "
    "@@SESSION.sql_safe_updates, @@SESSION.innodb_lock_wait_timeout, "
    "@@SESSION.optimizer_switch, LAST_INSERT_ID()"
)


@pytest.mark.integration
@needs_mysql
def test_a_setting_a_call_left_never_reaches_the_next_call_on_a_pooled_session():
    """The stand-in hands the next call the same session; every setting the call left is back
    at the run's baseline after the call and when the next call starts."""
    _skip_as_root()
    with _data_api_target(pooled=True) as (target, as_runner):
        _plant(as_runner, _LEAVE)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        before = de.execute(target._handle, _LEFT).rows
        assert target.run(_call("leave_settings")).error is None
        assert de.execute(target._handle, _LEFT).rows == before
        target.end_call()
        target.begin_call(_frozen(), 0.5)
        again = de.execute(target._handle, _LEFT).rows
        assert again[0][0] == before[0][0]  # the same pooled session
        assert again == before
        target.end_call()


@contextlib.contextmanager
def _pooled_factory(**pool: Any) -> Iterator[Any]:
    """A factory of MySQL Data API targets on one disposable ``mysqld``, through a stand-in
    that pools one session per secret across scratch databases (``times_out`` as the stand-in
    takes it), as a target opened again after a timed-out call would find it."""
    from tests.unit.tools.routines.local_mysql_data_api import LocalMySqlDataApi

    handle = de.start("mysql", _major())
    secrets = FakeSecrets()
    client = LocalMySqlDataApi(handle, secrets, pooled=True, per_secret=True, **pool)
    try:
        runner = dat.create_runner(client, secrets, cluster_arn=ARNS["cluster_arn"],
                                   secret_arn=ARNS["secret_arn"], job_id=JOB,
                                   admin_database=None, tags=[], engine="mysql")  # fmt: skip
        yield dat.data_api_target(client, cluster_arn=ARNS["cluster_arn"],
                                  secret_arn=ARNS["secret_arn"], engine="mysql", version="8.0",
                                  job_id=JOB, admin_database=None, runner=runner)  # fmt: skip
    finally:
        client.close_all()
        de.stop(handle)


def _opened(factory: Any, *texts: str) -> Any:
    """A target from ``factory``, its setup loaded, the procedures ``texts`` created."""
    target = factory()
    target.load_setup(_SETUP)
    for i, text in enumerate(texts):
        assert target.create(de.SourceRoutine(f"src.r{i}", "procedure", text)) is None
    target.prepare_calls()
    return target


#: Leaves settings that change what the next setup makes, then runs past its time limit.
_SLOW = (
    "CREATE PROCEDURE slow_settings() BEGIN SET SESSION default_storage_engine = 'MEMORY'; "
    "SET SESSION foreign_key_checks = 0; END"
)
_ENGINE = (
    "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
    "AND TABLE_NAME = 't'"
)


@pytest.mark.integration
@needs_mysql
def test_a_timed_out_calls_session_never_reaches_the_next_targets_setup():
    """The timed-out call's session goes back to the pool as the call left it; the side opened
    again after it gets that session for its setup, and a ``MEMORY`` table would keep a
    later call's writes past its rollback. The setup's table is the baseline's engine."""
    _skip_as_root()
    with _pooled_factory(times_out=("slow_settings",)) as factory:
        first = _opened(factory, _SLOW)
        first.begin_call(_frozen(), 0.25)
        assert first.run(_call("slow_settings")).timed_out
        first.end_call()
        first.close()
        second = _opened(factory)
        assert de.execute(second._handle, _ENGINE).rows == [["InnoDB"]]
        second.begin_call(_frozen(), 0.5)
        assert de.execute(second._handle, "SELECT @@SESSION.foreign_key_checks").rows == [[1]]
        second.end_call()
        second.close()


#: Leaves a named lock held, a temporary table and a prepared statement in its session.
_OBJECTS = (
    "CREATE PROCEDURE leave_objects() BEGIN SELECT GET_LOCK('mdz_lock', 0) INTO @l; "
    "CREATE TEMPORARY TABLE mdz_tmp (id INT); PREPARE mdz_s FROM 'SELECT 42'; END"
)


@pytest.mark.integration
@needs_mysql
def test_a_lock_a_temporary_table_or_a_prepared_statement_a_call_left_never_reaches_the_next():
    """On the same pooled session: the lock is released after the call, and when the next
    call starts the prepared statement is gone and the temporary table too, so the same
    procedure runs again as on a reset connection (no 1050)."""
    _skip_as_root()
    with _pooled_factory() as factory:
        target = _opened(factory, _OBJECTS)
        target.begin_call(_frozen(), 0.25)
        first = de.execute(target._handle, "SELECT CONNECTION_ID()").rows
        assert target.run(_call("leave_objects")).error is None
        assert de.execute(target._handle, "SELECT IS_FREE_LOCK('mdz_lock')").rows == [[1]]
        target.end_call()
        target.begin_call(_frozen(), 0.5)
        assert de.execute(target._handle, "SELECT CONNECTION_ID()").rows == first  # pooled
        assert de.execute(target._handle, "EXECUTE mdz_s").error is not None
        assert target.run(_call("leave_objects")).error is None
        target.end_call()
        target.close()


#: Refused for its row limit, it also leaves a setting no harness read depends on (a function
#: keeps ``lc_time_names``).
_LIMIT_AND_LOCALE = (
    "CREATE FUNCTION g() RETURNS INT DETERMINISTIC BEGIN SET SESSION lc_time_names = 'de_DE'; "
    "SET SESSION sql_select_limit = 2; RETURN 1; END"
)


@pytest.mark.integration
@needs_mysql
def test_a_refused_calls_pooled_session_is_set_back_before_it_is_handed_back():
    _skip_as_root()
    with _data_api_target(pooled=True) as (target, as_runner):
        _plant(as_runner, _LIMIT_AND_LOCALE)
        target.prepare_calls()
        target.begin_call(_frozen(), 0.25)
        with pytest.raises(_tampered()):
            target.run(_function("g"))
        assert de.execute(target._handle, "SELECT @@SESSION.lc_time_names").rows == [["en_US"]]
        target.end_call()


@pytest.mark.integration
@needs_mysql
@pytest.mark.passes_on_main  # the setup-table check of #2337 is engine-agnostic; guarded here
def test_a_setup_table_swapped_for_a_view_refuses_the_routine_on_mysql():
    """Side by side on two disposable engines: the original inserts a row, the carried one
    inserts two and then puts a view in the table's place. Never proved, never different:
    refused as ``harness_tampered``."""
    _skip_as_root()
    from src.tools.routines.comparison import compare_routines
    from src.tools.routines.proof_targets import disposable_target
    from tests.unit.tools.routines.test_routine_comparison_engine import _col, _pair, _proof

    tables = [{"table_id": "src.t", "table_name": "t", "row_count": 2, "primary_key": ["id"],
               "columns": [_col("id", "int")]}]  # fmt: skip
    proof = _proof("src.put", "procedure", "put", tables=("t",))
    original = "CREATE PROCEDURE put() BEGIN INSERT INTO t (id) VALUES (1000); END"
    carried = ("CREATE PROCEDURE put() BEGIN INSERT INTO t (id) VALUES (1000), (1001); "
               "DROP TABLE t; CREATE VIEW t AS SELECT 1 AS id; END")  # fmt: skip
    factory = disposable_target("mysql", _major())
    (r,) = compare_routines([_pair(proof, original, carried)], tables, source=factory,
                            target=factory, dialect="mysql").results  # fmt: skip
    assert (r.outcome, r.reason_code) == ("could_not_compare", "harness_tampered"), r


@pytest.mark.integration
@needs_mysql
@pytest.mark.passes_on_main  # the server's own rules and the engine's start flags
def test_a_routine_cannot_make_an_event_or_drop_a_procedure_and_the_local_scheduler_is_off():
    """A stored program can neither create an event nor drop or replace a procedure (the event
    runner procedures stay in the routines' database), and the local scheduler is off."""
    _skip_as_root()
    with _local_target() as (target, as_runner):
        event = ("CREATE PROCEDURE later() BEGIN CREATE EVENT e ON SCHEDULE EVERY 1 HOUR "
                 "DO DELETE FROM t; END")  # fmt: skip
        assert as_runner(event) == 1576  # recursion of EVENT DDL statements is forbidden
        drop = "CREATE PROCEDURE undo() BEGIN DROP PROCEDURE IF EXISTS other; END"
        # refused when it is parsed (1064) or created (1357, from within another routine)
        assert as_runner(drop) in (1064, 1357)
        assert de.execute(target._handle, "SELECT @@GLOBAL.event_scheduler").rows == [["OFF"]]


@pytest.mark.passes_on_main  # a marker guard: it reads markers, not behaviour
def test_the_mysql_round_trips_are_deselected_not_skipped_in_a_gated_run():
    for test in (
        test_the_sentinel_sits_in_a_harness_database_the_runner_cannot_change,
        test_a_call_that_swaps_the_sentinel_for_a_view_never_fools_the_transaction_state,
        test_a_temporary_table_a_call_made_never_stands_in_for_a_harness_read,
        test_session_settings_a_call_left_never_change_a_harness_read,
        test_a_row_limit_a_function_left_refuses_the_routine,
        test_settings_a_function_left_are_put_back_before_a_harness_read,
        test_a_setting_a_call_left_never_reaches_the_next_call_on_a_pooled_session,
        test_a_refused_calls_pooled_session_is_set_back_before_it_is_handed_back,
        test_a_timed_out_calls_session_never_reaches_the_next_targets_setup,
        test_a_lock_a_temporary_table_or_a_prepared_statement_a_call_left_never_reaches_the_next,
        test_a_setup_table_swapped_for_a_view_refuses_the_routine_on_mysql,
        test_a_routine_cannot_make_an_event_or_drop_a_procedure_and_the_local_scheduler_is_off,
        test_a_source_database_named_like_the_harness_database_is_refused,
    ):
        assert "integration" in {m.name for m in test.pytestmark}


# --------------------------------------------------------------------------- fake client


def _mysql_lifecycle(client: FakeDataApi) -> Any:
    t = _factory(client, "mysql")()
    t.load_setup(ProofSetup("mysql", ("CREATE TABLE t (id INT PRIMARY KEY)",),
                            (("t", ({"id": 1},)),), ("src.t",)))  # fmt: skip
    t.prepare_calls()
    t.begin_call(_frozen(), 0.25)
    t.snapshot(["t"])
    t.run(ProofCall("r", "r", "call", "f", "function", (1,)))
    t.snapshot(["t"])
    t.transaction_state()
    t.end_call()
    return t


def _mysql_started(client: FakeDataApi, factory: Any = None) -> Any:
    """A MySQL target on the fake, its first call begun (the run's session baseline read)."""
    t = (factory or _factory(client, "mysql"))()
    t.load_setup(ProofSetup("mysql", ("CREATE TABLE t (id INT PRIMARY KEY)",),
                            (("t", ({"id": 1},)),), ("src.t",)))  # fmt: skip
    t.prepare_calls()
    t.begin_call(_frozen(), 0.25)
    return t


def _set_back(client: FakeDataApi, name: str) -> list[Any]:
    """The statements sent to set ``name`` back, with their parameters."""
    return [(s.sql, s.parameters) for s in client.sent
            if s.op == "execute" and s.sql.startswith(f"SET SESSION `{name}` =")]  # fmt: skip


_F = ProofCall("r", "r", "call", "f", "function", (1,))


def test_a_setting_a_call_left_is_set_back_to_the_baseline_after_the_call():
    client = FakeDataApi("mysql")
    client.session["innodb_lock_wait_timeout"] = "5"  # as the harness sets it, not the global
    t = _mysql_started(client)
    client.session.update(foreign_key_checks="OFF", innodb_lock_wait_timeout="1")  # the call's
    assert t.run(_F).error is None
    assert client.session["foreign_key_checks"] == "ON"
    assert client.session["innodb_lock_wait_timeout"] == "5"
    assert _set_back(client, "foreign_key_checks") == [
        ("SET SESSION `foreign_key_checks` = @@GLOBAL.`foreign_key_checks`", [])
    ]
    assert _set_back(client, "innodb_lock_wait_timeout") == [
        (
            "SET SESSION `innodb_lock_wait_timeout` = :p0",
            [{"name": "p0", "value": {"longValue": 5}}],
        )
    ]
    t.end_call()


def test_a_setting_the_harness_cannot_put_back_refuses_the_routine():
    client = FakeDataApi("mysql")
    t = _mysql_started(client)
    client.session["sql_safe_updates"] = "ON"
    client.stuck = {"sql_safe_updates"}
    with pytest.raises(_tampered()):
        t.run(_F)
    assert _set_back(client, "sql_safe_updates")  # tried, and read again
    t.end_call()


@pytest.mark.parametrize(
    ("held", "left"),
    [({}, {"autocommit": "ON"}), ({}, {"weird`name": "1"}),
     ({"weird`name": "0"}, {"weird`name": "1"})],
    ids=["autocommit", "an_unplain_name", "an_unplain_name_the_baseline_holds"],
)  # fmt: skip
def test_a_session_variable_the_harness_never_sets_refuses_the_routine(held, left):
    """``autocommit`` set in the call's transaction would commit it, a variable the baseline
    does not hold has nothing to go back to, and a name that is not plain never goes into a
    statement, even one the baseline holds: none is set, all refuse."""
    client = FakeDataApi("mysql")
    client.session.update(held)
    t = _mysql_started(client)
    client.session.update(left)
    with pytest.raises(_tampered()):
        t.run(_F)
    assert not [s for s in client.sent if s.sql.startswith("SET SESSION `")]
    t.end_call()


def test_a_target_opened_again_compares_with_the_runs_first_baseline():
    """A target the same factory opens after another (a refused or timed-out call reopens the
    side) puts a session a call left back to the run's first baseline before its setup, never
    takes it as its own, and fails (the harness's failure, no routine's) on one it cannot put
    back, its transaction rolled back."""
    client = FakeDataApi("mysql")
    factory = _factory(client, "mysql")
    first = _mysql_started(client, factory)
    first.end_call()
    first.close()
    client.session["foreign_key_checks"] = "OFF"  # a pooled session a timed-out call left
    second = _mysql_started(client, factory)
    assert client.session["foreign_key_checks"] == "ON"
    second.end_call()
    second.close()
    client.session["foreign_key_checks"] = "OFF"
    client.stuck = {"foreign_key_checks"}
    with pytest.raises(ProofTargetError) as refused:
        _mysql_started(client, factory)
    assert not isinstance(refused.value, _tampered())
    assert client.sent[-1].op == "rollback"


def test_a_target_opened_again_resets_the_session_before_each_create_and_its_counters():
    client = FakeDataApi("mysql")
    factory = _factory(client, "mysql")
    first = _mysql_started(client, factory)
    first.end_call()
    first.close()
    second = factory()
    second.load_setup(ProofSetup("mysql", ("CREATE TABLE t (id INT PRIMARY KEY)",),
                                 (("t", ({"id": 1},)),), ("src.t",)))  # fmt: skip
    client.session["foreign_key_checks"] = "OFF"
    assert (
        second.create(
            de.SourceRoutine("src.f", "function", "CREATE FUNCTION f() RETURNS INT RETURN 1")
        )
        is None
    )
    assert client.session["foreign_key_checks"] == "ON"
    client.session["foreign_key_checks"] = "OFF"
    second.prepare_calls()
    assert client.session["foreign_key_checks"] == "ON"
    second.close()


def test_a_session_left_stuck_is_the_harnesss_failure_when_a_call_starts():
    """A setting found when a call starts was not set by that call's routine: never a
    refusal of it, the call's transaction rolled back."""
    client = FakeDataApi("mysql")
    t = _mysql_started(client)
    t.end_call()
    client.session["foreign_key_checks"] = "OFF"
    client.stuck = {"foreign_key_checks"}
    with pytest.raises(ProofTargetError) as refused:
        t.begin_call(_frozen(), 0.5)
    assert not isinstance(refused.value, _tampered())
    assert client.sent[-1].op == "rollback"


def test_the_insert_ids_start_every_call_at_zero():
    """As on a reset connection on the source side, whatever the first call's session held."""
    client = FakeDataApi("mysql")
    client.session.update(last_insert_id="5", insert_id="5")
    t = _mysql_started(client)
    assert (client.session["last_insert_id"], client.session["insert_id"]) == ("0", "0")
    assert _set_back(client, "last_insert_id") == [
        ("SET SESSION `last_insert_id` = :p0", [{"name": "p0", "value": {"longValue": 0}}])
    ]
    t.end_call()


@pytest.mark.parametrize(
    "made",
    [{"engines": {"t": "MEMORY"}}, {"program_modes": [("ANSI_QUOTES", "utf8mb4", "utf8mb4_0900_ai_ci")]},
     {"program_modes": [("STRICT_TRANS_TABLES", "latin1", "latin1_swedish_ci")]}],
    ids=["a_table_engine", "a_programs_sql_mode", "a_programs_character_set"],
)  # fmt: skip
def test_a_setup_made_on_a_session_a_call_left_fails_the_target(made):
    """A statement outside a transaction may run on any pooled session: a table or a stored
    program made with other settings than the baseline's fails the target before any call."""
    client = FakeDataApi("mysql")
    t = _factory(client, "mysql")()
    t.load_setup(ProofSetup("mysql", ("CREATE TABLE t (id INT PRIMARY KEY)",),
                            (("t", ({"id": 1},)),), ("src.t",)))  # fmt: skip
    for name, value in made.items():
        setattr(client, name, value)
    with pytest.raises(ProofTargetError, match="made on a session a call left"):
        t.prepare_calls()
    t.close()


@pytest.mark.passes_on_main  # the negative case: what must keep loading
def test_a_setup_made_as_the_baseline_has_it_passes_the_check():
    client = FakeDataApi("mysql")
    client.program_modes = [("STRICT_TRANS_TABLES", "utf8mb4", "utf8mb4_0900_ai_ci")]
    t = _mysql_started(client)
    t.end_call()
    t.close()


def test_the_master_user_makes_the_mysql_harness_database_before_the_runner_gets_anything():
    client = FakeDataApi("mysql")
    t = _mysql_lifecycle(client)
    harness = f"{t.database}_harness"
    assert t.harness_database == harness
    master = [s.sql for s in client.sent if s.op == "execute" and s.secret == ARNS["secret_arn"]]
    made = [f"CREATE DATABASE `{harness}`",
            f"CREATE TABLE `{harness}`.`modernizer_sentinel` (id INTEGER PRIMARY KEY)",
            f"GRANT SELECT, INSERT ON `{harness}`.`modernizer_sentinel` TO "
            f"'{t._runner.username}'@'%'"]  # fmt: skip
    grant_all = next(i for i, s in enumerate(master) if s.startswith("GRANT ALL"))
    assert master[grant_all - len(made) : grant_all] == made
    runner = [s.sql for s in client.sent if s.op in ("execute", "batch")
              and s.secret != ARNS["secret_arn"]]  # fmt: skip
    assert not [s for s in runner if "CREATE TABLE" in s and "sentinel" in s]
    sentinel = [s for s in runner if "modernizer_sentinel" in s]
    assert sentinel and all(f"`{harness}`.`modernizer_sentinel`" in s for s in sentinel)
    t.close()
    assert harness not in client.databases and t.database not in client.databases


def test_every_scratch_harness_database_of_a_job_is_dropped():
    client = FakeDataApi("mysql")
    t = _factory(client, "mysql")()
    t._created = False  # left as a killed run leaves it
    dropped, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                     admin_database="routine_proof")  # fmt: skip
    assert sorted(dropped) == sorted([t.database, f"{t.database}_harness"]) and left == []
    assert client.databases == set()


def test_the_mysql_sweep_drops_only_this_jobs_scratch_and_harness_databases():
    client = FakeDataApi("mysql")
    mine = dat.scratch_name(JOB)
    others = {dat.scratch_name("other-job") + "_harness", "customer_harness",
              mine + "_harness_x", dat.scratch_prefix(JOB) + "nothex_harness", "routine_proof"}  # fmt: skip
    client.databases |= {mine, mine + "_harness", *others}
    dropped, left = dat.drop_scratch(client, **ARNS, engine="mysql", job_id=JOB,
                                     admin_database="routine_proof")  # fmt: skip
    assert sorted(dropped) == sorted([mine, mine + "_harness"]) and left == []
    assert client.databases == others


@pytest.mark.passes_on_main  # main's sweep never matched it; keeps the MySQL pattern its own
def test_the_postgresql_sweep_never_drops_a_harness_named_database():
    client = FakeDataApi()
    mine = dat.scratch_name(JOB)
    client.databases |= {mine, mine + "_harness"}
    dropped, _ = dat.drop_scratch(client, **ARNS, engine="postgresql", job_id=JOB,
                                  admin_database="routine_proof")  # fmt: skip
    assert dropped == [mine] and mine + "_harness" in client.databases


@pytest.mark.integration
@needs_mysql
def test_a_source_database_named_like_the_harness_database_is_refused():
    from src.tools.routines.proof_targets import ProofTargetError

    _skip_as_root()
    target = DisposableEngineTarget("mysql", _major())
    try:
        with pytest.raises(ProofTargetError):
            target.load_setup(ProofSetup("mysql", (), (), (), schemas=("modernizer_harness",)))
    finally:
        target.close()
