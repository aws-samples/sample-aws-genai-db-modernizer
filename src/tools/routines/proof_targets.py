# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where each side of a side-by-side routine comparison runs (#1944).

The comparison (:mod:`src.tools.routines.comparison`) runs the same generated calls on
two engines: the original routines on a disposable engine of the source's engine and
version, and the carried routines on the test target. It talks to both through
:class:`ProofTarget`, so where a side runs is pluggable. :class:`DisposableEngineTarget`
is the source side's implementation; the carried side on the throwaway Aurora test cluster
(#1945, #1946) is :class:`~src.tools.routines.data_api_target.DataApiTarget`, a scratch
database reached through the RDS Data API. Nothing here talks to AWS.

Namespace. Every target owns one schema, ``namespace`` (on a disposable engine
PostgreSQL ``public`` and the MySQL database; on the test cluster, the scratch database's
``public`` schema on PostgreSQL and the scratch database on MySQL). Tables and routines are loaded into it, and ``create`` refuses, before running
it, any text :func:`~src.tools.routines.proof_screen.create_refusal` refuses: more than
one statement, a header or ``DEFINER`` outside the expected form, a name qualified with
another schema, a ``search_path`` setting, a body naming another schema's objects. So a
carried ``CREATE FUNCTION app.f`` never writes outside the target's schema. A dynamic
statement in a body is not read: on the test cluster the routines run as a role (PostgreSQL)
or a user (MySQL) that can write only to the scratch database.

What a target does, in the order the comparison calls it:

1. ``load_setup(setup)``: create the synthetic tables and rows
   (:class:`~src.tools.routines.proof_inputs.ProofSetup`), each table in its source schema
   (PostgreSQL) or database (MySQL), those made first (#1970). A body may then name one of
   them; a ``CREATE`` still never writes outside the target's own schema.
2. ``create(routine)``, once per ``CREATE`` statement: None when it loads, else the
   engine's SQLSTATE (``"?????"`` when it gave none); :class:`CreateRefused` for a text
   it refuses. The engine's message is never returned, because it can quote the text.
   On a disposable MySQL engine, a ``DEFINER`` naming another account is created signed in
   as that account, made for the run with the scratch database only
   (:func:`~src.tools.database.disposable_engine.load_as_definer`, #2426), the way a
   PostgreSQL original is owned by the role that creates it there. A name the engine must
   not make is :class:`DefinerRefused`. A routine that then runs as its definer (``SQL
   SECURITY DEFINER``, the default, and every trigger and event) gets a note in
   ``routine_notes`` (:data:`DEFINER_NOTE`), which the comparison adds to its reason.
3. ``prepare_calls()``: create the sentinel table, record every counter a rollback does
   not undo (sequences, ``AUTO_INCREMENT``), and find out whether the random seed is
   taken (``random_seeded``).
4. Per call: ``begin_call(frozen_at, seed)`` (reset the session, apply the session
   settings, put the counters back, freeze what the engine lets us freeze, open a
   transaction and write a sentinel row in it), ``now()`` (the instant the call's clock
   reads), ``tables()`` and ``snapshot(tables)`` before and after ``run(call)``,
   ``transaction_state()`` (whether a second connection sees the sentinel row: the call
   committed, even with nothing else pending or through a DDL statement's implicit
   commit; or whether its own session lost it: the call rolled back), then
   ``end_call()`` (roll back).
5. ``close()``.

A target that cannot run at all raises :class:`ProofTargetUnavailable` from its
constructor (no engine of that version here: the comparison reports *could not
compare*); any other failure is a :class:`ProofTargetError`, an infrastructure failure
rather than a verdict about a routine. Neither message holds customer text.

What each engine freezes, and nothing more:

- **MySQL:** ``SET TIMESTAMP`` to ``frozen_at``, so ``NOW()``, ``CURRENT_TIMESTAMP``,
  ``CURDATE()``, ``UTC_TIMESTAMP()`` and ``UNIX_TIMESTAMP()`` read the same instant on
  both sides, and ``rand_seed1``/``rand_seed2`` for ``RAND()`` when the server takes them.
  The session is reset before every call (``COM_RESET_CONNECTION``: user variables,
  temporary tables, session settings).
- **PostgreSQL:** ``setseed`` for ``random()``, the same draws only on the same major.
  ``now()`` and ``CURRENT_TIMESTAMP`` read the transaction's start; the comparison
  writes a value relative to it with its exact offset. Every other clock and every
  random UUID is left as the engine gives it: the comparison does not compare a routine
  that calls one (:func:`~src.tools.routines.proof_screen.nondeterministic_calls`).

Session time zone. Both sides run with the session time zone at ``+05:47``, an offset
that is neither UTC nor a whole hour, so a routine that mixes local and UTC time (``NOW()``
against ``UTC_TIMESTAMP()``, ``now()::timestamp`` against ``now() AT TIME ZONE 'UTC'``)
shows the difference instead of hiding it at UTC.

Time limits: each call runs under :func:`~src.tools.database.disposable_engine.time_limit`
(``call_timeout_s``), plus a lock timeout (PostgreSQL ``lock_timeout``, MySQL
``innodb_lock_wait_timeout``) and, on PostgreSQL, a ``statement_timeout``. A call that
runs out of time comes back with ``timed_out`` set; the comparison then does not compare
the routine, and opens the side again.
"""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from src.tools.database import disposable_engine as de
from src.tools.routines import mysql_checks
from src.tools.routines.proof_inputs import ProofCall, ProofSetup, run_call
from src.tools.routines.proof_screen import create_refusal

__all__ = [
    "CALL_TIMEOUT_S",
    "SENTINEL_TABLE",
    "SESSION_TIME_ZONE",
    "DEFINER_NOTE",
    "CreateRefused",
    "DefinerRefused",
    "DisposableEngineTarget",
    "ProofTarget",
    "ProofTargetError",
    "ProofTargetUnavailable",
    "TargetFactory",
    "disposable_target",
]

UNKNOWN_SQLSTATE = "?????"
#: Seconds one call may run before it is interrupted.
CALL_TIMEOUT_S = 30.0
#: Neither UTC nor a whole hour, nor any named zone's offset, so local time, UTC and
#: a named zone's time never coincide.
SESSION_TIME_ZONE = "+05:47"
#: The harness's own table: one row written in each call's transaction.
SENTINEL_TABLE = "modernizer_sentinel"
_SAVEPOINT = "modernizer_call"
# Module constants, never customer text.
_CREATE_SENTINEL = "CREATE TABLE modernizer_sentinel (id INTEGER PRIMARY KEY)"
_WRITE_SENTINEL = "INSERT INTO modernizer_sentinel (id) VALUES (1)"
_READ_SENTINEL = "SELECT count(*) FROM modernizer_sentinel"
#: PostgreSQL: the sentinel in the harness's own schema, which the superuser (on the test
#: cluster the master user) made before the runner owns anything (#2337).
_PG_WRITE_SENTINEL = f"INSERT INTO {de.PG_SENTINEL} (id) VALUES (1)"  # nosec B608 - a constant
_PG_READ_SENTINEL = f"SELECT pg_catalog.count(*) FROM {de.PG_SENTINEL}"  # nosec B608 - a constant
#: Why a call that changed what the harness reads refuses the routine (#2337).
HARNESS_TAMPERED = ("changed what the comparison reads (a cast, a row-level security policy, a "
                    "privilege on a table, or a setup table dropped, moved or replaced by a "
                    "view), so the routine is refused and that call is not counted")  # fmt: skip
_PG_SESSION = (
    "SET TIME ZONE INTERVAL '+05:47' HOUR TO MINUTE",
    "SET lock_timeout = '5s'",
)
_MY_SESSION = (
    "SET time_zone = '+05:47'",
    "SET SESSION innodb_lock_wait_timeout = 5",
)
_MY_SEED = "SET rand_seed1 = %s, rand_seed2 = %s"
# What a call may leave in the session: PostgreSQL settings set in it, MySQL session and
# user variables (its own thread only).
_PG_SESSION_STATE = "SELECT name, setting FROM pg_catalog.pg_settings WHERE source = 'session'"
_MY_SESSION_STATE = "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.session_variables"
_MY_USER_VARIABLES = (
    "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.user_variables_by_thread "
    "WHERE THREAD_ID = PS_CURRENT_THREAD_ID()"
)
# Read per call or per connection, never a routine's doing.
_VOLATILE = {"timestamp", "warning_count", "error_count", "pseudo_thread_id", "statement_id",
             "rand_seed1", "rand_seed2"}  # fmt: skip


class ProofTargetUnavailable(RuntimeError):
    """The target cannot run here at all (no engine of that engine and version)."""


class ProofTargetError(RuntimeError):
    """The target is there but failed (did not start, a read failed, a connection
    dropped). ``sqlstate`` is set when the failure was a SQL error."""

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class RoleChanged(ProofTargetError):
    """A call left the role it was confined to (``current_user`` is no longer the runner
    role, or ``session_user`` changed), read after the call and before its rollback (#1945):
    the routine is refused, and the call is neither counted nor run again."""

    reason_code = "role_changed"


class HarnessTampered(RoleChanged):
    """A call made something that changes what the harness's own reads return without going
    through ``search_path`` (#2337): a cast the read would use, a row-level security policy
    that hides rows (the pinned ``row_security = off`` turns that read into 42501), or a
    privilege taken away. Refused like a role change: the call is neither counted nor run
    again."""

    reason_code = "harness_tampered"


class CreateRefused(ValueError):
    """A ``CREATE`` the target refuses before running it; the message names no text."""

    #: The comparison's reason code for the refusal.
    reason_code = "outside_namespace"


class DefinerRefused(CreateRefused):
    """A MySQL ``CREATE`` whose ``DEFINER`` names an account a disposable engine does not
    make for a run (#2426); the message names no part of it."""

    reason_code = "definer_refused"


#: Added to the reason of a routine whose original ran as an account made for its
#: ``DEFINER`` (#2426): what it may do with the definer's privileges was not compared.
DEFINER_NOTE = (
    "the original ran as the user its DEFINER names, made for the test with the scratch "
    "database's privileges only, so what it may do outside that database with the "
    "definer's privileges on the source was not compared"
)


class ProofTarget(Protocol):
    """One side of the comparison. ``engine`` is ``postgresql`` or ``mysql``,
    ``version`` the major it runs, ``description`` how a result names it, ``namespace``
    the one schema it loads into, ``random_seeded`` whether it seeds the engine's random
    function per call, ``named_time_zones`` whether it knows named zones
    (``Europe/Paris``), ``notes`` what it could not freeze."""

    engine: str
    version: str
    description: str
    namespace: str
    random_seeded: bool
    named_time_zones: bool
    notes: tuple[str, ...]

    def load_setup(self, setup: ProofSetup) -> None: ...

    def create(self, routine: de.SourceRoutine) -> str | None: ...

    def prepare_calls(self) -> None: ...

    def begin_call(self, frozen_at: datetime.datetime, seed: float) -> None: ...

    def now(self) -> datetime.datetime: ...

    def tables(self) -> list[str]: ...

    def run(self, call: ProofCall) -> de.CallResult: ...

    def transaction_state(self) -> str: ...

    def column_types(self, tables: Sequence[str]) -> Mapping[str, Mapping[str, str]]: ...

    def session_state(self) -> Mapping[str, str] | None: ...

    def counter_state(self) -> Mapping[str, str]: ...

    def snapshot(self, tables: Sequence[str]) -> Mapping[str, list[dict[str, Any]]]: ...

    def end_call(self) -> None: ...

    def close(self) -> None: ...


#: Opens a fresh target; the comparison opens one per batch per side, and again when a
#: side has to be reset.
TargetFactory = Callable[[], ProofTarget]


def _utc(text: Any) -> datetime.datetime:
    value = datetime.datetime.fromisoformat(str(text))
    return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)


class DisposableEngineTarget:
    """A side on a throwaway engine (:mod:`src.tools.database.disposable_engine`): the
    source's engine and version for the original routines, and, until #1945 and #1946,
    the target's version for the carried ones."""

    #: The setup's source schemas (#1970), set by ``load_setup``.
    _schemas: tuple[str, ...] = ()

    def __init__(
        self,
        engine: str,
        version: str,
        *,
        call_timeout_s: float = CALL_TIMEOUT_S,
        **start_kwargs: Any,
    ) -> None:
        try:
            self._handle = de.start(engine, version, **start_kwargs)
        except de.EngineUnavailable as exc:
            raise ProofTargetUnavailable(str(exc)) from exc
        except de.EngineError as exc:
            raise ProofTargetError(str(exc)) from exc
        self.engine = self._handle.engine
        self.version = self._handle.version
        self.description = f"a disposable {self.engine} {self.version} engine"
        self.namespace = de.NAMESPACE[self.engine]
        self.random_seeded = self.engine == "postgresql"
        self.named_time_zones = de.time_zones_loaded(self._handle)
        self.notes: tuple[str, ...] = ()
        #: A note per ``CREATE`` text that the comparison adds to that routine's reason.
        self.routine_notes: dict[str, str] = {}
        self._timeout = call_timeout_s
        self._counters: tuple[tuple[str, Any, Any], ...] = ()
        self._open = False

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> de.CallResult:
        result = de.execute(self._handle, sql, params)
        if result.error is not None:
            raise ProofTargetError(
                f"{self.description} refused a comparison statement", result.sqlstate
            )
        return result

    def _session(self) -> None:
        if self.engine == "postgresql":
            for statement in _PG_SESSION:
                self._execute(statement)
            self._execute(f"SET statement_timeout = '{int(self._timeout * 1000)}ms'")
            return
        for statement in _MY_SESSION:
            self._execute(statement)

    def load_setup(self, setup: ProofSetup) -> None:
        try:
            de.create_namespaces(self._handle, setup.schemas)
            self._schemas = tuple(setup.schemas)
            de.load(self._handle, setup.ddl, setup.rows_by_table())
        except de.EngineError as exc:
            raise ProofTargetError(
                f"the synthetic rows did not load on {self.description}", exc.sqlstate
            ) from exc

    def create(self, routine: de.SourceRoutine) -> str | None:
        why = create_refusal(
            routine.create_sql, self.engine, routine.kind, self.namespace, self._schemas
        )
        if why is not None:
            raise CreateRefused(why)
        # Only on this module's own disposable MySQL engine: the Aurora test cluster's target
        # (``DataApiTarget``) inherits this method and creates every routine as its runner.
        account = (
            named_definer(routine)
            if self.engine == "mysql" and de.makes_definers(self._handle)
            else None
        )
        try:
            if account is None:
                de.load(self._handle, (), {}, [routine])
            elif de.load_as_definer(self._handle, routine, *account) and runs_as_definer(routine):
                self.routine_notes[routine.create_sql] = DEFINER_NOTE
        except de.DefinerRefused as exc:
            raise DefinerRefused(str(exc)) from None
        except de.EngineError as exc:
            return exc.sqlstate or UNKNOWN_SQLSTATE
        return None

    def prepare_calls(self) -> None:
        if self.engine == "mysql":  # on PostgreSQL the engine made it, in its own schema
            self._execute(_CREATE_SENTINEL)
        try:
            self._counters = de.counters(self._handle)
        except de.EngineError as exc:
            raise ProofTargetError(
                f"{self.description} could not read its counters", exc.sqlstate
            ) from exc
        self._session()
        if self.engine == "mysql":
            self.random_seeded = de.execute(self._handle, _MY_SEED, [1, 2]).error is None
            if not self.random_seeded:
                self.notes = ("the server refused the random seed, so RAND() was not frozen",)

    def begin_call(self, frozen_at: datetime.datetime, seed: float) -> None:
        try:
            de.reset_session(self._handle)
            de.restore_counters(self._handle, self._counters)
        except de.EngineError as exc:
            raise ProofTargetError(
                f"{self.description} could not reset its session", exc.sqlstate
            ) from exc
        if self.engine == "postgresql":
            self._execute("BEGIN")  # on the harness's path (#2337)
            self._open = True
            self._execute("SELECT pg_catalog.setseed(%s)", [seed])
            self._execute(_PG_WRITE_SENTINEL)
            return
        self._session()
        # to the microsecond, so a carry that loses the fraction of a second differs
        self._execute("SET TIMESTAMP = %s", [round(frozen_at.timestamp(), 6)])
        if self.random_seeded:
            whole = int(abs(seed) * 1_000_000) + 1
            self._execute(_MY_SEED, [whole, whole + 1])
        self._execute("START TRANSACTION")
        self._open = True
        self._execute(_WRITE_SENTINEL)

    def now(self) -> datetime.datetime:
        sql = "SELECT pg_catalog.now()" if self.engine == "postgresql" else "SELECT NOW(6)"
        return _utc(self._execute(sql).rows[0][0])

    def tables(self) -> list[str]:
        try:
            return [t for t in de.tables(self._handle) if t != SENTINEL_TABLE]
        except de.EngineError as exc:
            raise ProofTargetError(
                f"{self.description} could not list its tables", exc.sqlstate
            ) from exc

    def run(self, call: ProofCall) -> de.CallResult:
        postgres = self.engine == "postgresql"
        if postgres:
            # A failed statement aborts a PostgreSQL transaction: the savepoint keeps it
            # usable for the snapshot after the call (the failed statement left no change).
            self._execute(f"SAVEPOINT {_SAVEPOINT}")
            de.routine_path(self._handle)  # the call is the routines' own (#2337)
        with de.time_limit(self._handle, self._timeout) as limit:
            result = run_call(self._handle, call)
        if limit.fired:
            self._open = postgres  # MySQL: the connection, and its transaction, are gone
            return de.CallResult(error="the call ran past its time limit", timed_out=True)
        if postgres and result.error is not None:
            self._execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            de.after_rollback_to_savepoint(self._handle)
        if postgres:
            de.pin(self._handle)
        return result

    def transaction_state(self) -> str:
        """``open`` (the call left the transaction as it found it), ``committed`` (a second
        connection sees the sentinel row) or ``rolled back`` (the call's own session no
        longer sees it: a ``ROLLBACK``, ``ROLLBACK AND CHAIN``)."""
        read = _PG_READ_SENTINEL if self.engine == "postgresql" else _READ_SENTINEL
        aside = de.execute_aside(self._handle, read)
        own = de.execute(self._handle, read)
        for seen in (aside, own):
            if seen.error is not None:
                raise ProofTargetError(
                    f"{self.description} could not read its sentinel", seen.sqlstate
                )
        if aside.rows and aside.rows[0][0]:
            return "committed"
        if not (own.rows and own.rows[0][0]):
            return "rolled back"
        return "open"

    def column_types(self, tables: Sequence[str]) -> Mapping[str, Mapping[str, str]]:
        try:
            return de.column_types(self._handle, tables)
        except de.EngineError as exc:
            raise ProofTargetError(
                f"{self.description} could not read its column types", exc.sqlstate
            ) from exc

    def snapshot(self, tables: Sequence[str]) -> Mapping[str, list[dict[str, Any]]]:
        try:
            return de.snapshot(self._handle, tables)
        except de.EngineError as exc:
            if self.engine == "postgresql" and exc.sqlstate == "42501":
                # A policy the pinned row_security = off refuses, or a privilege taken away
                raise HarnessTampered(f"{self.description} could not read its tables: a call "
                                      f"{HARNESS_TAMPERED}", exc.sqlstate) from None  # fmt: skip
            raise ProofTargetError(
                f"{self.description} could not read its tables", exc.sqlstate
            ) from exc

    def end_call(self) -> None:
        if self._open:
            self._open = False
            self._execute("ROLLBACK")

    def session_state(self) -> Mapping[str, str] | None:
        """The session's settings and (MySQL) user variables as the call left them, or
        None when the engine does not let the runner read them."""
        if self.engine == "postgresql":
            reads = [_PG_SESSION_STATE]
        else:
            reads = [_MY_SESSION_STATE, _MY_USER_VARIABLES]
        out: dict[str, str] = {}
        for sql, prefix in zip(reads, ("", "@"), strict=False):
            got = de.execute(self._handle, sql)
            if got.error is not None:
                return None
            out.update(
                {prefix + str(k): str(v) for k, v in got.rows if str(k).lower() not in _VOLATILE}
            )
        return out

    def counter_state(self) -> Mapping[str, str]:
        """Each sequence's last value (None when never drawn) and each MySQL table's next
        ``AUTO_INCREMENT``, as the call left them."""
        try:
            return {str(name): str(value) for name, value, _ in de.counters(self._handle)}
        except de.EngineError as exc:
            raise ProofTargetError(
                f"{self.description} could not read its counters", exc.sqlstate
            ) from exc

    def close(self) -> None:
        with contextlib.suppress(Exception):
            de.stop(self._handle)


def _header(routine: de.SourceRoutine) -> mysql_checks.MySqlHeader | None:
    return mysql_checks.mysql_header(
        mysql_checks.MySqlRoutine(
            routine_id="proof.routine", kind=routine.kind, create_sql=routine.create_sql
        )
    )


def named_definer(routine: de.SourceRoutine) -> tuple[str, str | None] | None:
    """The ``(user, host)`` a MySQL ``CREATE``'s own ``DEFINER`` clause names (host None
    when the clause gives none), or None: no clause, a ``CURRENT_USER`` form, or a text
    whose header does not read (it then loads as the runner, as before #2426)."""
    header = _header(routine)
    account = header.account if header is not None else None
    return account if isinstance(account, tuple) else None


def runs_as_definer(routine: de.SourceRoutine) -> bool:
    """Whether a MySQL routine runs with its definer's privileges: every trigger and event,
    and a procedure or function unless it says ``SQL SECURITY INVOKER``."""
    if routine.kind in ("trigger", "event"):
        return True
    header = _header(routine)
    return header is None or header.security != "INVOKER"


def disposable_target(
    engine: str, version: str, *, call_timeout_s: float = CALL_TIMEOUT_S, **start_kwargs: Any
) -> TargetFactory:
    """A factory for :class:`DisposableEngineTarget` of this engine and major version."""

    def open_target() -> ProofTarget:
        return DisposableEngineTarget(
            engine, version, call_timeout_s=call_timeout_s, **start_kwargs
        )

    return open_target
