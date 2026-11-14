# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Disposable source engines for running the original routines (#1943).

To compare a carried routine with the original side by side, the original has to run
somewhere, and never on the customer's source. This module starts a throwaway
engine of the source's own engine and version, loads synthetic tables, rows and
the original routines into it, calls them, reads the tables back and removes
everything again::

    handle = start("postgresql", "16")
    try:
        load(handle, ddl, rows, routines)
        result = call(handle, "add_one", [1])
        tables = snapshot(handle, ["t"])
    finally:
        stop(handle)

``running(...)`` wraps the same calls in a context manager.

For the side-by-side comparison (#1944): ``counters(handle)`` and
``restore_counters(handle, state)`` read and put back what a rollback does not undo
(PostgreSQL sequences, MySQL ``AUTO_INCREMENT`` of tables with a plain name);
``tables(handle)`` lists the routines' schema's tables; ``reset_session(handle)`` gives
MySQL a new connection's session state; ``time_limit(handle, seconds)`` interrupts a
statement that runs too long. A failed statement carries its SQLSTATE
(``CallResult.sqlstate``, ``EngineError.sqlstate``) and MySQL's error number
(``CallResult.errno``), and a MySQL procedure's result sets keep their sizes
(``CallResult.result_sets``).

``execute(handle, sql, params)`` runs one more statement with bound values (a test
row change that fires a trigger, #1942); ``row_change_sql`` builds the ``INSERT``,
``UPDATE`` or ``DELETE`` for one with every identifier checked and quoted. ``call``
takes each argument's mode (``IN``, ``OUT``, ``INOUT``): MySQL passes an ``OUT`` or
``INOUT`` argument through a session variable and returns its value as one more row.
On PostgreSQL ``call`` also takes each argument's type, written as a cast
(``%s::integer``) so the call reaches that overload. ``pg_cast_type`` parses the type
first: a built-in type the Aurora PostgreSQL allowlist reads, or a user type of one or two
identifiers written back with each part quoted; anything else is refused. After the rows
load, PostgreSQL sets every identity or serial sequence of a loaded table to its highest
loaded value, as MySQL's ``AUTO_INCREMENT`` does on its own, so a routine's own
``INSERT`` does not collide with a synthetic key and a column already at its type's
highest value does not overflow the load.

Binding. The engine never listens on a network interface. PostgreSQL and MySQL
both use a Unix socket inside a private (0700) temporary folder. Only where Unix
sockets do not exist does MySQL fall back to ``127.0.0.1`` on a random free port.
There is no wildcard bind. PostgreSQL runs with ``listen_addresses`` empty and a
``pg_hba.conf`` that trusts the local socket only: the binary path writes it with
``initdb --auth-local=trust --auth-host=reject``, and on the ``pgserver`` path
(whose own ``initdb`` also writes ``127.0.0.1`` and ``::1`` trust lines) the file
is rewritten to the single ``local`` line right after the server is up. MySQL runs
with ``--skip-networking``.

Privileges. Customer routine text is untrusted, so it never runs as the engine
superuser. Setup (the role, the database) uses the superuser; the ``ddl``, the
rows, every ``CREATE`` and every call run as a plain login role that owns only
its own database: PostgreSQL ``NOSUPERUSER`` without ``pg_execute_server_program``
or the server-file roles, MySQL a user granted its one schema and no global
privilege, so ``FILE`` (``INTO OUTFILE``) and ``SUPER`` are refused. MySQL also
runs with ``--secure-file-priv`` pointing at an empty folder, ``--local-infile=0``,
no binary log and ``--event-scheduler=OFF`` (so a loaded event never fires between a
call and the snapshot). MySQL's ``root`` password is random, taken from
``MODERNIZER_DISPOSABLE_ENGINE_PASSWORD`` when set and generated otherwise; the
runner's password is always generated and never equals it. PostgreSQL has no
superuser password, because its socket is trusted.

Not as root. ``start`` refuses when the process runs as root, because the 0700
guarantee above cannot be kept: ``pgserver`` creates a user and widens the folder
and its parents to ``o+rx``, and ``initdb`` and ``mysqld`` refuse root outright.
The refusal is an ``EngineStartError`` ("running as root"), an environment failure
and not a missing engine; it comes after the missing-version check, so a major no
binary offers is still ``EngineUnavailable`` as root. Run these engines, and the tests that use them, as an
ordinary user.

Version. The engine runs the source's major version (PostgreSQL ``12`` to ``17``,
MySQL ``5.7``, ``8.0``, ``8.4``) and never a different one: the major is a
required argument, and ``start(..., newest=True)`` is the only way to let the
newest present major be picked. The picked major is logged and is
``handle.version``. ``available_majors(engine)`` lists exactly the majors present
and ``available(engine, version)`` tests one.

Errors. ``EngineUnavailable`` means no binary for that engine and major is
present ("no engine for <engine> <version>"), which the comparison reports as
"could not compare". ``EngineStartError`` means the engine is present but did not
start, with the real cause and the last lines of its own log, which is an
infrastructure error and not a verdict about the routine; so is the refusal to run
as root. ``EngineError`` covers a failed load or read.

Where the binaries come from. Any major can come from conda-forge
(``conda install -c conda-forge postgresql=<major>``, ``conda install -c
conda-forge mysql-server=<major>``, or the same with ``micromamba``).

* PostgreSQL: a ``bin`` folder holding ``initdb`` and ``postgres``, found through
  the ``pg_bin_dir`` argument, ``MODERNIZER_PG_BIN_<major>`` (for example
  ``MODERNIZER_PG_BIN_14``), ``MODERNIZER_PG_BIN``, or a ``postgres`` on
  ``PATH``. The ``pgserver`` package (a dev dependency) is the last resort for
  its own major (16).
* MySQL: a ``mysqld`` found through the ``mysqld_path`` argument,
  ``MODERNIZER_MYSQLD_<major>`` (``_8_0`` for 8.0), ``MODERNIZER_MYSQLD`` or
  ``PATH``.

For both engines an explicit argument wins over the environment, and a
``MODERNIZER_..._<major>`` whose binary is a different major is refused rather
than registered under the wrong name.

Cleanup. ``stop`` shuts the engine down the engine's own way first (PostgreSQL
fast shutdown, MySQL ``SIGTERM``) and only escalates to ``SIGKILL`` of that one
pid when that does not finish, because a killed postmaster leaks its System V
shared-memory segment, and removes the temporary folder. ``start`` does the same
when it fails, including on Ctrl-C and including a server that is already up on
either PostgreSQL path, and an ``atexit`` hook stops any engine a caller forgot.

Hard kills. No handler survives ``SIGKILL`` or a hard test timeout, so every folder
gets an ``owner.txt`` the moment it is created, holding the creating process's pid
and start time, and later the server pids. ``start`` sweeps (``sweep_stale()`` does
it on its own): a folder is left alone while its owner process is alive (another
run may be starting an engine in it, which takes seconds), while it is younger than
a couple of minutes, and when it is not a real directory owned by this user. When
the owner is dead, any server still running from that folder (recognised by the
folder in its command line) is stopped the engine's own way first, and then the
folder is removed.

Input shape for routines. ``SourceRoutine`` is the typed input this module takes:
the exact ``CREATE`` text of one routine plus its kind and name. It is the shape
the routine collection (#1920) will provide; this module does not read the
collection itself.
"""

from __future__ import annotations

import atexit
import contextlib
import datetime
import decimal
import hashlib
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess  # nosec B404 - runs the engine binaries, fixed argument lists
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.contracts.aurora_design_delta import validate_aurora_type

logger = logging.getLogger(__name__)

SUPPORTED_ENGINES = ("postgresql", "mysql")
# The name of a variable holding a password, not a password.
PASSWORD_ENV = "MODERNIZER_DISPOSABLE_ENGINE_PASSWORD"  # nosec B105 - a variable name, not a secret  # pragma: allowlist secret
MYSQLD_ENV = "MODERNIZER_MYSQLD"
MYSQLD_PREFIX = "MODERNIZER_MYSQLD_"  # MODERNIZER_MYSQLD_8_0 -> a mysqld 8.0
PG_BIN_ENV = "MODERNIZER_PG_BIN"
PG_BIN_PREFIX = "MODERNIZER_PG_BIN_"  # MODERNIZER_PG_BIN_14 -> the bin folder of PostgreSQL 14
_HOW_TO = {
    "postgresql": (
        "conda install -c conda-forge postgresql=<major>, "
        "then set MODERNIZER_PG_BIN_<major> to its bin folder"
    ),
    "mysql": (
        "conda install -c conda-forge mysql-server=<major>, "
        "then set MODERNIZER_MYSQLD_<major> or put mysqld on PATH"
    ),
}

# Both are well below the 300 s per-test limit in ci/test.sh, so a slow start
# fails here, with the engine's own reason, instead of being killed by pytest.
_INIT_TIMEOUT_S = 120.0
_START_TIMEOUT_S = 30.0
_SHUTDOWN_TIMEOUT_S = 15.0
_TEMP_PREFIX = "modernizer-engine-"
_OWNER_FILE = "owner.txt"  # who created the folder, and which servers run from it
_SWEEP_MIN_AGE_S = 120.0  # a folder younger than this is never swept
_RUNNER = "routine_runner"  # the non-superuser role every routine runs as
_DATABASE = "src"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
#: The schema every loaded table and routine lives in (PostgreSQL schema, MySQL database).
NAMESPACE = {"postgresql": "public", "mysql": _DATABASE}
_LOG_TAIL_BYTES = 4000
# Setup statements. Only the module's own constants, never customer text, and the
# passwords are bound.
_PG_CREATE_ROLE = (
    f"CREATE ROLE {_RUNNER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
    "NOREPLICATION NOBYPASSRLS PASSWORD %s"
)
_PG_CREATE_DB = f"CREATE DATABASE {_DATABASE} OWNER {_RUNNER}"
_MY_CREATE_DB = f"CREATE DATABASE `{_DATABASE}`"
_MY_CREATE_USER = f"CREATE USER '{_RUNNER}'@'localhost' IDENTIFIED BY %s"
# Only this one schema: no FILE, no SUPER, no other global privilege.
_MY_GRANT = f"GRANT ALL PRIVILEGES ON `{_DATABASE}`.* TO '{_RUNNER}'@'localhost'"
_MY_GRANT_USER_VARIABLES = (
    f"GRANT SELECT ON performance_schema.user_variables_by_thread TO '{_RUNNER}'@'localhost'"
)
_MY_ARG_VAR = "@modernizer_arg_%d"  # the session variable an OUT or INOUT argument uses
# A loaded table's identity and serial columns, with their sequences (the table twice:
# pg_get_serial_sequence takes its name as text, the filter as a regclass).
_PG_TABLE_SEQUENCES = (
    "SELECT attname, seq FROM (SELECT a.attname, pg_get_serial_sequence(%s, a.attname) AS seq "
    "FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 "
    "AND NOT a.attisdropped) s WHERE seq IS NOT NULL ORDER BY attname"
)
# Every sequence of the database with its state, and how to put one back (#1944).
_PG_SEQUENCE_STATE = (
    "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename), last_value, "
    "start_value FROM pg_sequences ORDER BY 1"
)
_PG_SET_SEQUENCE = "SELECT setval(%s::regclass, %s, %s)"
_MY_STATS_NOW = "SET SESSION information_schema_stats_expiry = 0"  # 8.0+: no cached counter
_MY_AUTO_INCREMENTS = (
    "SELECT TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND AUTO_INCREMENT IS NOT NULL ORDER BY TABLE_NAME"
)
# The base tables of the routines' own schema (PostgreSQL ``public``, the MySQL database).
_PG_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
_MY_TABLES = (
    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
    "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
)
_PG_COLUMN_TYPES = (
    "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
    "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
)
_MY_COLUMN_TYPES = (
    "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
)
_MY_THREAD_ALIVE = "SELECT ID FROM information_schema.PROCESSLIST WHERE ID = %s"
_MY_SET_ROOT_PASSWORD = "ALTER USER 'root'@'localhost' IDENTIFIED BY %s"  # nosec B105 - a bound parameter, not a password


class EngineUnavailable(RuntimeError):
    """No binary for this engine and major version is present here.

    The message is ``no engine for <engine> <version>`` plus the reason. The
    comparison turns it into "could not compare".
    """


class EngineError(RuntimeError):
    """The engine is present but something went wrong: a failed load or read.

    ``sqlstate`` is the engine's five-character SQLSTATE when the failure was a SQL
    error (a ``CREATE`` the engine refused), None otherwise.
    """

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class EngineStartError(EngineError):
    """The engine is present but did not start (shared memory, permissions, ...).

    Never "no engine": the version is installed, so the comparison must report an
    infrastructure failure, not a verdict about the routine.
    """


@dataclass(frozen=True)
class SourceRoutine:
    """One routine as the collection provides it: plain ``CREATE`` text plus metadata.

    ``create_sql`` is a single statement, exactly as collected, so it needs no
    ``DELIMITER`` handling. ``name`` may be schema-qualified (``app.add_one``).
    """

    name: str
    kind: Literal["function", "procedure", "trigger", "event"]
    create_sql: str = field(repr=False)


@dataclass(frozen=True)
class CallResult:
    """What one routine call gave back: result sets, or the error it raised.

    ``sqlstate`` is the error's five-character SQLSTATE when the engine gave one (a
    side-by-side comparison matches errors on it, #1944); ``error`` is the engine's own
    message, which can quote the routine's text, so it never goes into a report.
    """

    rows: list[list[Any]] = field(default_factory=list)
    error: str | None = None
    sqlstate: str | None = None
    #: MySQL's own error number (``HY000`` alone covers hundreds of errors).
    errno: int | None = None
    #: The number of rows in each result set a MySQL procedure returned (its ``OUT``
    #: arguments' row last), so moving a row between result sets is not hidden by
    #: ``rows``, which holds them all in order; None for a single result.
    result_sets: tuple[int, ...] | None = None
    #: The error's own message (PostgreSQL's primary message, MySQL's text) and, for a
    #: constraint violation, the constraint and its table: the comparison digests them to
    #: tell two errors of one SQLSTATE apart. Both can quote routine text: never report them.
    message: str | None = field(default=None, repr=False)
    constraint: str | None = field(default=None, repr=False)
    #: The call ran past its time limit and was interrupted (:func:`time_limit`).
    timed_out: bool = False
    #: The engine had nothing to run (a MySQL event the server dropped as it was created,
    #: or one with no runnable name, #1971): ``error`` says why, in the engine's own fixed
    #: words, and there is no SQLSTATE because no statement reached the server.
    not_run: bool = False
    #: The engine's type of each result column (PostgreSQL ``format_type``, MySQL's field
    #: type), one tuple per result set, so ``1`` from an ``integer`` and from a
    #: ``boolean`` or ``double`` column are told apart.
    column_types: tuple[tuple[str, ...], ...] = ()
    #: Each result column's name, one tuple per result set: a caller reads columns by
    #: name, so ``total`` and ``amount`` differ. A name can be routine text (MySQL names an
    #: unaliased literal column by the literal): never report one unchecked.
    column_names: tuple[tuple[str, ...], ...] = ()


@dataclass
class EngineHandle:
    """A running disposable engine. Pass it to ``load``, ``call``, ``snapshot``, ``stop``."""

    engine: str
    version: str
    workdir: Path
    endpoint: str
    pid: int | None
    _backend: _Backend = field(repr=False, default=None)  # type: ignore[assignment]
    stopped: bool = False


# --------------------------------------------------------------------------- helpers


def _normalize(value: Any) -> Any:
    """JSON-friendly, engine-independent form of one cell."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (set, frozenset)):
        return sorted(_normalize(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    return str(value)


def _sort_key(row: Mapping[str, Any]) -> str:
    return repr(sorted(row.items()))


def _parts(name: str) -> list[str]:
    parts = name.split(".")
    if not parts or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"not a plain identifier: {name!r}")
    return parts


def _quote(name: str, mark: str) -> str:
    """Quote a validated identifier (``mark`` is ``"`` or a backtick); identifiers cannot be bound."""
    return ".".join(mark + p + mark for p in _parts(name))


def _quote_table(name: str, mark: str) -> str:
    """Quote one table name of any spelling (``odd log``): the mark doubled inside it. A
    NUL or another control character is refused, since no engine names a table so."""
    if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("not a table name a statement may write")
    return mark + name.replace(mark, mark + mark) + mark


_SQL_NOTE = "identifiers are validated by _parts and quoted; values are bound"


def _insert_sql(table: str, cols: Sequence[str], mark: str) -> str:
    names = ", ".join(_quote(c, mark) for c in cols)
    marks = ", ".join(["%s"] * len(cols))
    return f"INSERT INTO {_quote(table, mark)} ({names}) VALUES ({marks})"  # nosec B608 - see _SQL_NOTE


def _select_all_sql(table: str, mark: str) -> str:
    target = (
        _quote(table, mark) if _IDENT.match(table.split(".")[-1]) else _quote_table(table, mark)
    )
    return f"SELECT * FROM {target}"  # nosec B608 - see _SQL_NOTE


_EVENT_RUNNER = "__proof_event_"
_MY_NAME_MAX = 64


def event_runner_name(event: str) -> str:
    """The name of the procedure the MySQL backend makes from an event's own body (#1971):
    ``__proof_event_`` and a hash of the event's plain name (any schema part dropped, case
    folded: MySQL does not tell ``NightJob`` from ``nightjob``), so
    it is deterministic, within MySQL's 64-character limit and never the event's text."""
    name = _parts(event)[-1].casefold()  # event names are case-insensitive
    digest = hashlib.sha256(name.encode()).hexdigest()[:20]
    return f"{_EVENT_RUNNER}{digest}"


def _event_key(name: str) -> str:
    """How the MySQL backend remembers an event it cannot run: the last part of its name,
    case folded, whatever characters it has."""
    return name.split(".")[-1].casefold()


def _call_sql(
    name: str, kind: str, nargs: int, mark: str, engine: str, slots: Sequence[str] | None = None
) -> str:
    marks = ", ".join(slots if slots is not None else ["%s"] * nargs)
    target = _quote(name, mark)
    if kind == "procedure":
        return f"CALL {target}({marks})"
    if engine == "postgresql":
        return f"SELECT * FROM {target}({marks})"  # nosec B608 - see _SQL_NOTE
    return f"SELECT {target}({marks})"  # nosec B608 - see _SQL_NOTE


# A user type a cast may name: one or two identifiers (schema, type), each plain or
# double-quoted ("" inside a quoted one), then array brackets. Parsed, never matched
# loosely: the output is rebuilt from the parts, each one quoted.
_PG_TYPE_PART = re.compile(r'"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_$]*)')
_PG_SERIALS = {"smallserial", "serial", "bigserial", "serial2", "serial4", "serial8"}
_PG_NAME_MAX = 63  # NAMEDATALEN - 1


def _pg_user_type(text: str) -> str:
    parts: list[str] = []
    i = 0
    while True:
        m = _PG_TYPE_PART.match(text, i)
        if m is None:
            raise ValueError("not a type name")
        name = m.group(1).replace('""', '"') if m.group(1) is not None else m.group(2).lower()
        if "\x00" in name or len(name.encode()) > _PG_NAME_MAX:
            raise ValueError("not a type name")
        parts.append(name)
        i = m.end()
        if text.startswith(".", i) and len(parts) < 2:
            i += 1
            continue
        break
    arrays = text[i:]
    if arrays.replace("[]", ""):
        raise ValueError("not a type name")
    return ".".join('"' + n.replace('"', '""') + '"' for n in parts) + arrays


def pg_cast_type(type_name: str) -> str:
    """The type a PostgreSQL cast writes for ``type_name``, or ``ValueError``.

    Either a built-in type, exactly as the Aurora PostgreSQL type allowlist reads it
    (``validate_aurora_type``: a base name from its list, an optional ``(n)`` or
    ``(p, s)``, ``WITH TIME ZONE`` and ``[]``), or a user type, ``name`` or
    ``schema.name`` with each part plain or double-quoted, written back with every part
    quoted (``public."Mood"`` gives ``"public"."Mood"``, ``"char"`` gives ``"char"``).
    Anything else is refused, so no expression can reach the SQL around the cast.
    """
    text = type_name.strip()
    if not text or len(text) > 200:
        raise ValueError(f"not a type name a cast may write: {type_name!r}")
    if text.split("(")[0].split("[")[0].strip().lower() in _PG_SERIALS:
        raise ValueError(f"a serial type is not a cast: {type_name!r}")
    try:
        return validate_aurora_type(text, "aurora_postgresql")
    except ValueError:
        pass
    try:
        return _pg_user_type(text)
    except ValueError:
        raise ValueError(f"not a type name a cast may write: {type_name!r}") from None


RowEvent = Literal["INSERT", "UPDATE", "DELETE"]
ArgMode = Literal["IN", "OUT", "INOUT"]
_MARKS = {"postgresql": '"', "mysql": "`"}


def row_change_sql(
    engine: str,
    event: RowEvent,
    table: str,
    columns: Sequence[str] = (),
    where: Sequence[str] = (),
) -> str:
    """The statement for one row change, values left as ``%s`` marks: ``INSERT`` of
    ``columns``, ``UPDATE`` setting ``columns``, or ``DELETE``, each row matched on
    ``where`` (``column = %s`` each). Every name must be a plain identifier."""
    mark = _MARKS[_canonical(engine)]
    target = _quote(table, mark)
    match = " AND ".join(f"{_quote(c, mark)} = %s" for c in where)
    if event == "INSERT":
        return _insert_sql(table, columns, mark)
    if not where:
        raise ValueError("an UPDATE or DELETE needs the columns that match its row")
    if event == "UPDATE":
        if not columns:
            raise ValueError("an UPDATE needs a column to set")
        sets = ", ".join(f"{_quote(c, mark)} = %s" for c in columns)
        return f"UPDATE {target} SET {sets} WHERE {match}"  # nosec B608 - see _SQL_NOTE
    if event == "DELETE":
        return f"DELETE FROM {target} WHERE {match}"  # nosec B608 - see _SQL_NOTE
    raise ValueError(f"not a row event: {event!r}")


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of a driver error: psycopg2's ``pgcode``, MySQL Connector's ``sqlstate``."""
    code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    return str(code) if code else None


def _my_types(description: Any) -> tuple[str, ...]:
    """MySQL's field type of each column of a result (``LONG``, ``DOUBLE``, ``DATETIME``)."""
    from mysql.connector import FieldType

    return tuple(str(FieldType.get_info(d[1])) for d in description or ())


def _my_names(description: Any) -> tuple[str, ...]:
    return tuple(str(d[0]) for d in description or ())


def _error_result(exc: BaseException) -> CallResult:
    errno = getattr(exc, "errno", None)
    diag = getattr(exc, "diag", None)  # psycopg2
    message = None
    if diag is not None and getattr(diag, "message_primary", None) is not None:
        # DETAIL holds a violated key's value and HINT the routine's own hint
        message = "\n".join(
            str(getattr(diag, a, None) or "")
            for a in ("message_primary", "message_detail", "message_hint")
        )
    if message is None:
        message = getattr(exc, "msg", None)  # MySQL Connector
    names = ("schema_name", "table_name", "column_name", "datatype_name", "constraint_name")
    parts = [getattr(diag, a, None) for a in names] if diag is not None else []
    constraint = "/".join(str(p or "") for p in parts) if any(parts) else None
    return CallResult(
        error=f"{type(exc).__name__}: {str(exc).strip()}",
        sqlstate=_sqlstate(exc),
        errno=errno if isinstance(errno, int) and errno > 0 else None,
        message=str(message) if message else None,
        constraint=constraint,
    )


def _execute(cur: Any, sql: str, params: Sequence[Any] | None = None) -> None:
    cur.execute(
        sql, params
    )  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- see _SQL_NOTE


def _passwords() -> tuple[str, str]:
    """``(root, runner)``: root's from the environment when set, the runner's always generated."""
    root = os.environ.get(PASSWORD_ENV) or secrets.token_urlsafe(24)
    runner = secrets.token_urlsafe(24)
    while runner == root:  # never the same, whatever the environment holds
        runner = secrets.token_urlsafe(24)
    return root, runner


def _is_root() -> bool:
    return os.geteuid() == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _temp_root() -> str | None:
    # Unix socket paths are limited to about 100 bytes; macOS's default TMPDIR is long.
    return "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - private mkdtemp below


def _short_tempdir() -> Path:
    path = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=_temp_root()))
    path.chmod(0o700)
    # At once, before anything that takes seconds: a parallel run's sweep must see an
    # owner and leave this folder alone while the server is still starting.
    _write_owner(path, [])
    return path


def _major(engine: str, version: str) -> str:
    """The major that has to match: ``16`` or ``9.6`` for PostgreSQL, ``8.0`` for MySQL."""
    nums = re.findall(r"\d+", str(version))
    if not nums:
        return ""
    if engine == "postgresql":
        # One number from PostgreSQL 10 on; two before it, where 9.5 and 9.6 are
        # different majors.
        return str(nums[0]) if int(nums[0]) >= 10 else ".".join(str(n) for n in (nums + ["0"])[:2])
    return ".".join(str(n) for n in (nums + ["0"])[:2])


def _major_sort(major: str) -> list[int]:
    return [int(n) for n in major.split(".")]


def _unavailable(engine: str, version: str, why: str) -> EngineUnavailable:
    return EngineUnavailable(f"no engine for {engine} {version}: {why}")


def _log_tail(path: Path) -> str:
    """The last lines of the engine's own log (in the 0700 folder, never customer text)."""
    try:
        text = path.read_bytes()[-_LOG_TAIL_BYTES:].decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line for line in text.splitlines() if line.strip()][-5:]
    return " | ".join(lines)


def _start_failed(engine: str, major: str, why: str, log: Path | None = None) -> EngineStartError:
    tail = _log_tail(log) if log is not None else ""
    return EngineStartError(
        f"{engine} {major} is installed but did not start: {why}" + (f" [{tail}]" if tail else "")
    )


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int, field_name: str) -> str | None:
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed arguments, no shell
            ["ps", "-ww", "-o", field_name, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


_START_TOLERANCE_S = 2.0
_MARIADB_BANNER = re.compile(r"maria", re.IGNORECASE)  # a binary's own --version text


def _proc_start(pid: int) -> float | None:
    """When the process started, in epoch seconds: tells a pid that was reused later.

    An epoch float, because anything the system prints (``ps -o lstart=``) is local
    time in the caller's language, so two runs of the same user under a different
    ``TZ`` or locale would disagree about whether an owner is alive.
    """
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - gone, denied or psutil missing: unknown
        return None


def _proc_cmdline(pid: int) -> str | None:
    proc_file = Path(f"/proc/{pid}/cmdline")
    try:
        text = proc_file.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        return text or None
    except OSError:
        pass
    return _ps(pid, "command=")


def _ours(pid: int, folder: Path) -> bool:
    """Whether ``pid`` is a process we started from ``folder``.

    The folder is unique to one ``start`` (``mkdtemp``), so its path in the process's
    command line is what names it. A process whose command line cannot be read is
    never ours, so nothing unrelated is signalled on a guess.
    """
    if not _running(pid):
        return False
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return False
    names = {str(folder), str(folder.resolve())}
    return any(name in cmdline for name in names)


def _stop_pid(pid: int, folder: Path, first: signal.Signals = signal.SIGTERM) -> None:
    """Stop one server process that is not our child, only if it is ours.

    The engine's own signal first (a ``SIGKILL``ed postmaster leaks its System V
    shared-memory segment, and macOS allows only 32), then ``SIGKILL`` of that one
    pid if it has not gone in time. Never a process group: the group of a pid that
    is not our child is not ours to signal, and the pid is checked again before the
    kill because it may have exited and been reused meanwhile.
    """
    if not _ours(pid, folder):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, first)
    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _running(pid):
            return
        time.sleep(0.1)
    if not _ours(pid, folder):
        return
    logger.warning("engine pid %s did not stop on %s; killing it", pid, first.name)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _reap(proc: subprocess.Popen[bytes] | None, first: signal.Signals = signal.SIGTERM) -> None:
    """Stop our own engine process the engine's way, escalating only if it does not go.

    ``proc.wait`` rather than a pid check, because an exited child we have not
    waited for is a zombie and still answers ``kill(pid, 0)``. The child is ours and
    unreaped, so its pid cannot have been reused, and ``start_new_session`` made its
    group ours: the ``SIGKILL`` fallback may take the group.
    """
    if proc is None or proc.poll() is not None:
        return
    # Only the server process first: PostgreSQL's children take a signal of their own
    # badly, and a child killed behind the postmaster's back can leave the
    # System V segment behind.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(proc.pid, first)
    try:
        proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        logger.warning("engine pid %s did not stop on %s; killing it", proc.pid, first.name)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)


def _write_owner(workdir: Path, servers: list[int]) -> None:
    # Plain "key=value" lines, not JSON: this is a private note in a throwaway folder,
    # not a versioned artifact, and it must stay readable by a sweep of any version.
    lines = [
        f"owner_pid={os.getpid()}",
        f"owner_start={_proc_start(os.getpid()) or ''}",  # epoch seconds, zone-free
        "servers=" + ",".join(str(p) for p in servers),
    ]
    tmp = workdir / (_OWNER_FILE + ".new")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, workdir / _OWNER_FILE)


def _read_owner(folder: Path) -> dict[str, Any] | None:
    try:
        text = (folder / _OWNER_FILE).read_text()
    except OSError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    try:
        return {
            "owner_pid": int(fields["owner_pid"]),
            "owner_start": float(fields["owner_start"]) if fields.get("owner_start") else None,
            "servers": [int(p) for p in fields.get("servers", "").split(",") if p],
        }
    except (KeyError, ValueError):
        return None


def _record_server(workdir: Path, pid: int | None) -> None:
    """Note a server pid in the folder's owner record, for the sweep after a hard kill."""
    if pid is None:
        return
    record = _read_owner(workdir) or {}
    servers = [p for p in record.get("servers", []) if isinstance(p, int)]
    _write_owner(workdir, sorted({*servers, pid}))


def _stop_workdir_servers(workdir: Path) -> None:
    """Stop every server still running from ``workdir``, whichever way it started.

    Reads the pids the owner record, ``pgdata/postmaster.pid`` and ``m.pid`` name, so a
    server is found even when ``start`` failed before it could note it (``pgserver``
    timing out after its ``pg_ctl start``, Ctrl-C in a wait loop).
    """
    found: dict[int, signal.Signals] = {}
    record = _read_owner(workdir) or {}
    for pid in record.get("servers", []):
        if isinstance(pid, int):
            found[pid] = signal.SIGINT
    for name, sig in (("pgdata/postmaster.pid", signal.SIGINT), ("m.pid", signal.SIGTERM)):
        try:
            first_line = (workdir / name).read_text().splitlines()[0].strip()
            found[int(first_line)] = sig
        except (OSError, ValueError, IndexError):
            continue
    for pid, sig in found.items():
        if pid != os.getpid():
            _stop_pid(pid, workdir, sig)


def _owner_alive(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    pid = record.get("owner_pid")
    if not isinstance(pid, int) or not _running(pid):
        return False
    started = record.get("owner_start")
    now = _proc_start(pid)
    return started is None or now is None or abs(now - started) <= _START_TOLERANCE_S


def _sweepable(entry: Path) -> bool:
    """A real directory of ours, old enough, that no live process owns."""
    try:
        info = entry.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        logger.warning("%s is not a plain directory; leaving it alone", entry)
        return False
    if info.st_uid != os.getuid():
        return False
    if time.time() - info.st_mtime < _SWEEP_MIN_AGE_S:
        return False
    return not _owner_alive(_read_owner(entry))


def sweep_stale() -> list[Path]:
    """Clean up after crashed runs, and return the folders removed.

    Nothing survives ``SIGKILL`` or a hard test timeout, so a crashed run leaves a
    folder, and its server, behind. A folder is swept only when it is a plain
    directory owned by this user, is not younger than ``_SWEEP_MIN_AGE_S`` and its
    owner process (recorded when it was created) is gone, so a parallel run that is
    still starting its engine is never touched. Any server still running from it is
    stopped the engine's own way first. ``start`` calls this.
    """
    root = _temp_root() or tempfile.gettempdir()
    removed: list[Path] = []
    for entry in sorted(Path(root).glob(_TEMP_PREFIX + "*")):
        if any(entry == live.workdir for live in _LIVE) or not _sweepable(entry):
            continue
        _stop_workdir_servers(entry)
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry)
    if removed:
        logger.info("swept %d stale disposable-engine folder(s)", len(removed))
    return removed


# --------------------------------------------------------------------------- backends


class _Backend:
    def load(
        self,
        ddl: Sequence[str],
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
        routines: Sequence[SourceRoutine],
    ) -> None:
        raise NotImplementedError

    def call(
        self,
        routine: SourceRoutine | str,
        args: Sequence[Any],
        modes: Sequence[ArgMode] | None = None,
        casts: Sequence[str | None] | None = None,
    ) -> CallResult:
        raise NotImplementedError

    def execute(self, sql: str, params: Sequence[Any]) -> CallResult:
        raise NotImplementedError

    def snapshot(self, tables: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        raise NotImplementedError

    def counters(self) -> tuple[tuple[str, Any, Any], ...]:
        raise NotImplementedError

    def tables(self) -> list[str]:
        raise NotImplementedError

    def column_types(self, tables: Sequence[str]) -> dict[str, dict[str, str]]:
        raise NotImplementedError

    def reset_session(self) -> None:
        raise NotImplementedError

    def interrupt(self) -> None:
        raise NotImplementedError

    def reconnect(self) -> None:
        raise NotImplementedError

    def execute_aside(self, sql: str, params: Sequence[Any]) -> CallResult:
        raise NotImplementedError

    def restore_counters(self, state: Sequence[tuple[str, Any, Any]]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _routine_kind(
    backend_routines: dict[str, str], routine: SourceRoutine | str
) -> tuple[str, str]:
    if isinstance(routine, SourceRoutine):
        return routine.name, routine.kind
    return routine, backend_routines.get(routine, "function")


class _PgserverServer:
    """PostgreSQL 16 from the ``pgserver`` package, on a Unix socket."""

    def __init__(self, workdir: Path, major: str) -> None:
        import pgserver  # dev dependency; the caller has checked it imports

        self._server: Any = None
        self._workdir = workdir
        self.pid: int | None = None
        try:
            self._server = pgserver.get_server(workdir / "pgdata", cleanup_mode="delete")
            info = self._server.get_postmaster_info()
            self.pid = info.pid
            _record_server(workdir, self.pid)
            self.socket_dir = Path(info.socket_dir or workdir)
            self._trust_the_socket_only(workdir)
        except BaseException as exc:  # a server may already be up: never orphan it
            self.stop()
            if isinstance(exc, (EngineUnavailable, EngineError, KeyboardInterrupt, SystemExit)):
                raise
            raise _start_failed(
                "postgresql", major, f"pgserver failed ({type(exc).__name__}: {exc})"
            ) from exc

    def _trust_the_socket_only(self, workdir: Path) -> None:
        """Drop pgserver's ``127.0.0.1`` and ``::1`` trust lines (dormant, but not needed)."""
        import psycopg2

        (workdir / "pgdata" / "pg_hba.conf").write_text("local all all trust\n")
        conn = psycopg2.connect(self.dsn("postgres", "postgres"))
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_reload_conf()")
        finally:
            conn.close()

    def dsn(self, user: str, dbname: str, password: str | None = None) -> str:
        parts = [f"host={self.socket_dir}", f"dbname={dbname}", f"user={user}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    def stop(self) -> None:
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.cleanup()  # pg_ctl stop, so no shared-memory segment leaks
            self._server = None
        # A failure inside pgserver.get_server leaves the postmaster up with no
        # handle here: find it through pgdata/postmaster.pid. This is the first line
        # of defence; start()'s failure path calls the same cleanup as the backstop,
        # and test_a_failure_inside_pgserver_... only fails when BOTH are removed.
        _stop_workdir_servers(self._workdir)


class _PgBinaryServer:
    """PostgreSQL from a ``bin`` folder (``initdb`` and ``postgres``), on a Unix socket only."""

    def __init__(self, workdir: Path, bin_dir: Path, major: str) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self.pid: int | None = None
        self.socket_dir = workdir
        self._log = workdir / "postgres.log"
        self._pg_ctl = bin_dir / "pg_ctl"
        self._data = workdir / "pgdata"
        self._workdir = workdir
        try:
            self._initdb(workdir, bin_dir, major)
            self._spawn(workdir, bin_dir, major)
            self._wait(major)
        except BaseException:  # includes KeyboardInterrupt: never orphan the server
            self.stop()
            raise

    def _initdb(self, workdir: Path, bin_dir: Path, major: str) -> None:
        # trust on the socket in the 0700 folder, reject every host line.
        args = [
            str(bin_dir / "initdb"),
            "-D",
            str(workdir / "pgdata"),
            "-U",
            "postgres",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-sync",  # a throwaway cluster never has to survive a crash
        ]
        try:
            subprocess.run(  # nosec B603 - fixed arguments, no shell
                args, check=True, capture_output=True, timeout=_INIT_TIMEOUT_S
            )
        except subprocess.CalledProcessError as exc:
            why = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise _start_failed(
                "postgresql", major, f"initdb failed: {why[-1] if why else exc}"
            ) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise _start_failed(
                "postgresql", major, f"initdb failed ({type(exc).__name__})"
            ) from exc

    def _spawn(self, workdir: Path, bin_dir: Path, major: str) -> None:
        settings = [
            "listen_addresses=",  # no TCP listener at all
            f"unix_socket_directories={workdir}",
            "fsync=off",
        ]
        if _major_sort(major)[0] >= 12:  # shared_memory_type arrived in PostgreSQL 12
            settings += ["shared_memory_type=mmap", "dynamic_shared_memory_type=posix"]
        args = [str(bin_dir / "postgres"), "-D", str(workdir / "pgdata")]
        for setting in settings:
            args += ["-c", setting]
        try:
            with self._log.open("wb") as log:
                self._proc = subprocess.Popen(  # nosec B603 - fixed arguments, no shell
                    args, stdout=log, stderr=log, start_new_session=True
                )
        except OSError as exc:
            raise _start_failed("postgresql", major, f"postgres could not be run ({exc})") from exc
        self.pid = self._proc.pid
        _record_server(workdir, self.pid)

    def _wait(self, major: str) -> None:
        import psycopg2

        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise _start_failed(
                    "postgresql", major, "postgres exited while starting", self._log
                )
            try:
                psycopg2.connect(self.dsn("postgres", "postgres")).close()
                return
            except psycopg2.OperationalError:
                time.sleep(0.2)
        raise _start_failed("postgresql", major, "postgres did not accept connections", self._log)

    def dsn(self, user: str, dbname: str, password: str | None = None) -> str:
        parts = [f"host={self.socket_dir}", f"dbname={dbname}", f"user={user}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    def stop(self) -> None:
        # pg_ctl's fast shutdown is PostgreSQL's own way out, and it is the
        # postmaster that removes the System V segment; a killed one leaks it.
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                subprocess.run(  # nosec B603 - fixed arguments, no shell
                    [
                        str(self._pg_ctl),
                        "-D",
                        str(self._data),
                        "-m",
                        "fast",
                        "-w",
                        "-t",
                        str(int(_SHUTDOWN_TIMEOUT_S)),
                        "stop",
                    ],
                    capture_output=True,
                    timeout=_SHUTDOWN_TIMEOUT_S + 5,
                    check=False,
                )
        _reap(self._proc, signal.SIGINT)  # a backstop when pg_ctl is not there
        self._proc = None
        _stop_workdir_servers(self._workdir)


class _PostgresBackend(_Backend):
    """Routines run as a plain login role, never as the ``postgres`` superuser."""

    def __init__(self, server: _PgserverServer | _PgBinaryServer) -> None:
        import psycopg2

        self._psycopg2 = psycopg2
        self._kinds: dict[str, str] = {}
        self._server = server
        self.pid: int | None = server.pid
        password = secrets.token_urlsafe(24)
        admin = psycopg2.connect(server.dsn("postgres", "postgres"))
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                _execute(cur, _PG_CREATE_ROLE, [password])
                _execute(cur, _PG_CREATE_DB)
        finally:
            admin.close()
        self._password = password
        self._type_names: dict[int, str] = {}
        self._cancels = 0
        self._terminated = False
        self._conn = self._connect()

    def _connect(self) -> Any:
        """A runner connection that reads ``interval`` and ``json`` as the engine writes
        them: ``'1 mon'`` is not ``'30 days'``, and ``json`` keeps its key order."""
        ext = self._psycopg2.extensions
        conn = self._psycopg2.connect(self._server.dsn(_RUNNER, _DATABASE, self._password))
        conn.autocommit = True
        for oids, array, name in (((1186,), (1187,), "INTERVAL"), ((114,), (199,), "JSON")):
            as_text = ext.new_type(oids, f"{name}_AS_TEXT", lambda value, cur: value)
            ext.register_type(as_text, conn)
            ext.register_type(ext.new_array_type(array, f"{name}_AS_TEXT_ARRAY", as_text), conn)
        return conn

    def _types(self, description: Any) -> tuple[str, ...]:
        if not self._type_names:
            cur = self._conn.cursor()
            try:
                _execute(cur, "SELECT oid::int, format_type(oid, NULL) FROM pg_type")
                self._type_names = {int(o): str(n) for o, n in cur.fetchall()}
            finally:
                cur.close()
        return tuple(self._type_names.get(int(d.type_code), str(d.type_code)) for d in description)

    def load(self, ddl, rows, routines):  # type: ignore[no-untyped-def]
        cur = self._conn.cursor()
        try:
            for statement in ddl:
                cur.execute(statement)
            for table, table_rows in rows.items():
                for row in table_rows:
                    cols = list(row)
                    _execute(cur, _insert_sql(table, cols, '"'), [row[c] for c in cols])
            for table in rows:
                self._advance_sequences(cur, table)
            for routine in routines:
                self._kinds[routine.name] = routine.kind
                cur.execute(routine.create_sql)
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    @staticmethod
    def _advance_sequences(cur: Any, table: str) -> None:
        """Every identity or serial sequence of ``table`` continues past its highest value."""
        quoted = _quote(table, '"')
        _execute(cur, _PG_TABLE_SEQUENCES, [quoted, quoted])
        for column, sequence in cur.fetchall():
            if not _IDENT.match(column):
                continue
            col = _quote(column, '"')
            # setval(seq, max, true): the next value is max + 1, and a row already at the
            # type's highest value does not overflow the sequence; an empty table keeps it
            highest = f"SELECT max({col}) AS m FROM {quoted}"  # nosec B608 - see _SQL_NOTE
            _execute(
                cur,
                f"SELECT setval(%s, m, true) FROM ({highest}) s WHERE m IS NOT NULL",  # nosec B608 - see _SQL_NOTE
                [sequence],
            )

    def call(self, routine, args, modes=None, casts=None):  # type: ignore[no-untyped-def]
        # PostgreSQL takes OUT arguments of a procedure as values (NULL) and returns them
        # as the call's row; a function's OUT arguments are not passed at all. A cast
        # names the overload: f(%s::integer) is never f(bigint) or f(text).
        name, kind = _routine_kind(self._kinds, routine)
        if kind == "event":
            raise ValueError("an event runs on MySQL only")
        casts = list(casts or [None] * len(args))
        if len(casts) != len(args):
            raise ValueError("one cast (or None) per argument")
        slots = [f"%s::{pg_cast_type(c)}" if c else "%s" for c in casts]
        sql = _call_sql(name, kind, len(args), '"', "postgresql", slots)
        cur = self._conn.cursor()
        try:
            _execute(cur, sql, list(args))
            if not cur.description:
                return CallResult()
            rows = [[_normalize(c) for c in r] for r in cur.fetchall()]
            return CallResult(
                rows=rows,
                column_types=(self._types(cur.description),),
                column_names=(tuple(str(d[0]) for d in cur.description),),
            )
        except self._psycopg2.Error as exc:
            return _error_result(exc)
        finally:
            cur.close()

    def execute(self, sql, params):  # type: ignore[no-untyped-def]
        cur = self._conn.cursor()
        try:
            _execute(cur, sql, list(params))
            rows = [[_normalize(c) for c in r] for r in cur.fetchall()] if cur.description else []
            return CallResult(rows=rows)
        except self._psycopg2.Error as exc:
            return _error_result(exc)
        finally:
            cur.close()

    def column_types(self, tables):  # type: ignore[no-untyped-def]
        out: dict[str, dict[str, str]] = {}
        cur = self._conn.cursor()
        try:
            for table in tables:
                _execute(cur, _PG_COLUMN_TYPES, [_quote_table(table, '"')])
                out[table] = {str(n): str(t) for n, t in cur.fetchall()}
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()
        return out

    def snapshot(self, tables):  # type: ignore[no-untyped-def]
        out: dict[str, list[dict[str, Any]]] = {}
        cur = self._conn.cursor()
        try:
            for table in tables:
                _execute(cur, _select_all_sql(table, '"'))
                names = [d[0] for d in cur.description]
                got = [
                    {n: _normalize(v) for n, v in zip(names, r, strict=True)}
                    for r in cur.fetchall()
                ]
                out[table] = sorted(got, key=_sort_key)
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()
        return out

    def tables(self) -> list[str]:
        cur = self._conn.cursor()
        try:
            _execute(cur, _PG_TABLES)
            return [str(r[0]) for r in cur.fetchall()]
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def reset_session(self) -> None:
        # Every session change a call can make (SET, set_config, a temporary table) is
        # transactional on PostgreSQL, so the rollback after each call undoes it.
        return None

    def interrupt(self) -> None:
        """Cancel the statement (57014 in the call); a routine that catches the cancel is
        ended after three, by terminating its backend from a second connection."""
        self._cancels += 1
        if self._cancels <= 3:
            self._conn.cancel()  # psycopg2: safe from another thread
            return
        self._terminated = True
        self.execute_aside("SELECT pg_terminate_backend(%s)", [int(self._conn.get_backend_pid())])

    def reconnect(self) -> None:
        """After :meth:`interrupt`: a new connection when the old one was terminated."""
        self._cancels = 0
        if self._terminated or self._conn.closed:
            self._terminated = False
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = self._connect()

    def execute_aside(self, sql, params):  # type: ignore[no-untyped-def]
        conn = self._psycopg2.connect(self._server.dsn(_RUNNER, _DATABASE, self._password))
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                _execute(cur, sql, list(params))
                rows = (
                    [[_normalize(c) for c in r] for r in cur.fetchall()] if cur.description else []
                )
                return CallResult(rows=rows)
            except self._psycopg2.Error as exc:
                return _error_result(exc)
        finally:
            conn.close()

    def counters(self) -> tuple[tuple[str, Any, Any], ...]:
        """``(sequence, last value, start value)`` for every sequence of the database."""
        cur = self._conn.cursor()
        try:
            _execute(cur, _PG_SEQUENCE_STATE)
            return tuple((str(n), last, start) for n, last, start in cur.fetchall())
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def restore_counters(self, state):  # type: ignore[no-untyped-def]
        # setval is not undone by a rollback, so it puts each sequence back by hand; a
        # sequence never drawn from goes back to its start, not yet called.
        cur = self._conn.cursor()
        try:
            for name, last, start in state:
                called = last is not None
                _execute(cur, _PG_SET_SEQUENCE, [name, last if called else start, called])
        except self._psycopg2.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
        self._server.stop()


class _MySQLServer:
    """MySQL from a ``mysqld`` binary, on a Unix socket, with no binary log."""

    def __init__(self, workdir: Path, mysqld: str, major: str) -> None:
        import mysql.connector

        self._mc = mysql.connector
        self._proc: subprocess.Popen[bytes] | None = None
        self.pid: int | None = None
        self.password, self.runner_password = _passwords()
        self._mysqld = mysqld
        self.time_zones = False
        self._log = workdir / "mysqld.log"
        self._workdir = workdir
        self.socket_path: str | None = (
            str(workdir / "m.sock") if hasattr(socket, "AF_UNIX") else None
        )
        self.port = 0 if self.socket_path else _free_port()
        try:
            self._initialize(workdir, mysqld, major)
            self._spawn(workdir, mysqld, major)
            self._secure(major)
        except BaseException:  # includes KeyboardInterrupt: never orphan the server
            self.stop()
            raise

    def _base(self, workdir: Path, mysqld: str) -> list[str]:
        return [
            mysqld,
            "--no-defaults",
            f"--datadir={workdir / 'data'}",
            f"--log-error={self._log}",
        ]

    def _initialize(self, workdir: Path, mysqld: str, major: str) -> None:
        try:
            subprocess.run(  # nosec B603 - fixed arguments, no shell
                self._base(workdir, mysqld) + ["--initialize-insecure"],
                check=True,
                capture_output=True,
                timeout=_INIT_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise _start_failed(
                "mysql",
                major,
                f"mysqld could not initialise a data folder ({type(exc).__name__})",
                self._log,
            ) from exc

    def _spawn(self, workdir: Path, mysqld: str, major: str) -> None:
        listen = (
            ["--skip-networking", f"--socket={self.socket_path}"]
            if self.socket_path
            else [
                "--bind-address=127.0.0.1",
                f"--port={self.port}",
                f"--socket={workdir / 'm.sock'}",
            ]
        )
        no_export = workdir / "no-export"
        no_export.mkdir(exist_ok=True)
        no_export.chmod(0o700)
        hardening = [
            f"--secure-file-priv={no_export}",  # an empty folder, so OUTFILE has nowhere to go
            "--local-infile=0",
            "--skip-log-bin",  # also drops the SUPER requirement for creating routines
            "--event-scheduler=OFF",  # an event must not fire between a call and the snapshot
        ]
        if not major.startswith("5."):
            hardening.append("--mysqlx=OFF")
        args = (
            self._base(workdir, mysqld) + listen + hardening + [f"--pid-file={workdir / 'm.pid'}"]
        )
        try:
            self._proc = subprocess.Popen(  # nosec B603 - fixed arguments, no shell
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
        except OSError as exc:
            raise _start_failed(
                "mysql", major, f"mysqld could not be run ({exc})", self._log
            ) from exc
        self.pid = self._proc.pid
        _record_server(workdir, self.pid)

    def connect_args(
        self, user: str, password: str | None, database: str | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"user": user, "autocommit": True, "use_pure": True}
        if password is not None:
            args["password"] = password
        if database:
            args["database"] = database
        if self.socket_path:
            args["unix_socket"] = self.socket_path
        else:
            args.update(host="127.0.0.1", port=self.port)
        return args

    def _secure(self, major: str) -> None:
        """Set the root password, then make the database and the non-admin runner."""
        deadline = time.monotonic() + _START_TIMEOUT_S
        last: Exception | None = None
        conn = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise _start_failed("mysql", major, "mysqld exited while starting", self._log)
            try:
                conn = self._mc.connect(**self.connect_args("root", None))
                break
            except Exception as exc:  # noqa: BLE001 - not up yet
                last = exc
                time.sleep(0.2)
        if conn is None:
            raise _start_failed(
                "mysql", major, f"mysqld did not accept connections ({last})", self._log
            )
        try:
            cur = conn.cursor()
            _execute(cur, _MY_SET_ROOT_PASSWORD, [self.password])
            _execute(cur, _MY_CREATE_DB)
            _execute(cur, _MY_CREATE_USER, [self.runner_password])
            _execute(cur, _MY_GRANT)
            # its own session's user variables, for the comparison (#1944); no other privilege
            with contextlib.suppress(self._mc.Error):
                _execute(cur, _MY_GRANT_USER_VARIABLES)
            cur.close()
            self.time_zones = self._load_time_zones(conn)
        finally:
            conn.close()

    def _load_time_zones(self, conn: Any) -> bool:
        """Load the named time zones (``Europe/Paris``) from the system's zoneinfo through
        the ``mysql_tzinfo_to_sql`` beside ``mysqld``, as Aurora MySQL has them; False when
        either is missing. The statements are the tool's own output, never customer text."""
        tool = Path(self._mysqld).with_name("mysql_tzinfo_to_sql")
        zoneinfo = Path("/usr/share/zoneinfo")
        if not (os.access(tool, os.X_OK) and zoneinfo.is_dir()):
            return False
        try:
            sql = subprocess.run(  # nosec B603 - a fixed tool and folder, no shell
                [str(tool), str(zoneinfo)],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            ).stdout
            cur = conn.cursor()
            _execute(cur, "USE mysql")
            for statement in sql.split(";\n"):
                if statement.strip():
                    with contextlib.suppress(self._mc.Error):  # a zone file the tool skips
                        _execute(cur, statement)
            _execute(cur, "SELECT CONVERT_TZ('2024-01-01 00:00:00', 'UTC', 'Europe/Paris')")
            loaded = cur.fetchall()[0][0] is not None
            cur.close()
            return loaded
        except (subprocess.SubprocessError, OSError, self._mc.Error):
            return False

    def stop(self) -> None:
        _reap(self._proc, signal.SIGTERM)  # mysqld's own clean shutdown signal
        self._proc = None
        _stop_workdir_servers(self._workdir)


class _MySQLBackend(_Backend):
    """Routines run as a user granted one schema, never as ``root``."""

    def __init__(self, server: _MySQLServer) -> None:
        self._mc = server._mc
        self._kinds: dict[str, str] = {}
        #: events with no body to run (their key from ``_event_key``) and why
        self._no_run: dict[str, str] = {}
        self._server = server
        self.pid: int | None = server.pid
        self.socket_path = server.socket_path
        self.port = server.port
        self._conn = self._mc.connect(
            **server.connect_args(_RUNNER, server.runner_password, _DATABASE)
        )

    def load(self, ddl, rows, routines):  # type: ignore[no-untyped-def]
        cur = self._conn.cursor()
        try:
            for statement in ddl:
                cur.execute(statement)
            for table, table_rows in rows.items():
                for row in table_rows:
                    cols = list(row)
                    _execute(cur, _insert_sql(table, cols, "`"), [row[c] for c in cols])
            for routine in routines:
                self._kinds[routine.name] = routine.kind
                cur.execute(routine.create_sql)
                if routine.kind == "event":
                    self._make_event_runner(cur, routine.name)
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def _make_event_runner(self, cur, event: str) -> None:  # type: ignore[no-untyped-def]
        """Make a procedure with the event's own statement as its body, read back from the
        server, so one run of the event is a ``CALL`` while the scheduler stays off. Made
        here, at load, because DDL commits the transaction a call runs in. An event that
        cannot be run (a name no runner can be named after, or one the server dropped when
        it was created because its time had passed) is remembered with why, and its run
        answers with that error: it fails its own calls, never the load (#1971)."""
        key = _event_key(event)
        try:
            name = _parts(event)[-1]
        except ValueError:
            self._no_run[key] = "the event's name is not a plain identifier, so it is not run"
            return
        _execute(
            cur,
            "SELECT EVENT_DEFINITION FROM information_schema.EVENTS "
            "WHERE EVENT_SCHEMA = DATABASE() AND LOWER(EVENT_NAME) = %s",
            [name.casefold()],
        )
        found = cur.fetchall()
        if not found or not found[0][0]:
            self._no_run[key] = (
                "the server dropped the event when it was created (its time had passed), "
                "so there is no body to run"
            )
            return
        runner = _quote(event_runner_name(name), "`")
        _execute(cur, f"DROP PROCEDURE IF EXISTS {runner}")
        # nosec B608 - a hashed, quoted name; the body is the event's own text
        _execute(cur, f"CREATE PROCEDURE {runner}() {found[0][0]}")  # nosec B608

    def call(self, routine, args, modes=None, casts=None):  # type: ignore[no-untyped-def]
        # MySQL has no overloads, so a cast is never needed and is not written.
        name, kind = _routine_kind(self._kinds, routine)
        if kind == "event":
            if args or modes or casts:
                raise ValueError("an event run takes no argument")
            if (why := self._no_run.get(_event_key(name))) is not None:
                return CallResult(error=why, not_run=True)
            name, kind = event_runner_name(name), "procedure"
        modes = list(modes or ["IN"] * len(args))
        if len(modes) != len(args):
            raise ValueError("one mode per argument")
        slots = [_MY_ARG_VAR % i if m != "IN" else "%s" for i, m in enumerate(modes)]
        sql = _call_sql(name, kind, len(args), "`", "mysql", slots)
        cur = self._conn.cursor()
        try:
            if kind == "procedure":
                outs = [i for i, m in enumerate(modes) if m != "IN"]
                for i in outs:  # an OUT or INOUT argument is a session variable
                    value = args[i] if modes[i] == "INOUT" else None
                    _execute(cur, f"SET {_MY_ARG_VAR % i} = %s", [value])
                _execute(cur, sql, [a for a, m in zip(args, modes, strict=True) if m == "IN"])
                rows: list[list[Any]] = []
                sizes: list[int] = []
                with warnings.catch_warnings():  # the connector is moving this to a property
                    warnings.simplefilter("ignore", DeprecationWarning)
                    stored = cur.stored_results
                    results = list(stored() if callable(stored) else stored)
                fetched = [result.fetchall() for result in results]
                kinds = [_my_types(result.description) for result in results]
                names = [_my_names(result.description) for result in results]
                if not results:  # Connector 9.2 on: a CALL's result sets come through nextset
                    while True:
                        if cur.description:
                            kinds.append(_my_types(cur.description))
                            names.append(_my_names(cur.description))
                            fetched.append(cur.fetchall())
                        if not getattr(cur, "nextset", lambda: None)():
                            break
                for result_rows in fetched:
                    got = [[_normalize(c) for c in r] for r in result_rows]
                    sizes.append(len(got))
                    rows.extend(got)
                if outs:
                    read = ", ".join(_MY_ARG_VAR % i for i in outs)
                    _execute(cur, f"SELECT {read}")  # nosec B608 - fixed variable names
                    kinds.append(_my_types(cur.description))
                    names.append(_my_names(cur.description))
                    got = [[_normalize(c) for c in r] for r in cur.fetchall()]
                    sizes.append(len(got))
                    rows.extend(got)
                return CallResult(
                    rows=rows,
                    result_sets=tuple(sizes),
                    column_types=tuple(kinds),
                    column_names=tuple(names),
                )
            _execute(cur, sql, list(args))
            types = (_my_types(cur.description),)
            labels = (_my_names(cur.description),)
            return CallResult(
                rows=[[_normalize(c) for c in r] for r in cur.fetchall()],
                column_types=types,
                column_names=labels,
            )
        except self._mc.Error as exc:
            return _error_result(exc)
        finally:
            cur.close()

    def execute(self, sql, params):  # type: ignore[no-untyped-def]
        cur = self._conn.cursor()
        try:
            _execute(cur, sql, list(params))
            rows = [[_normalize(c) for c in r] for r in cur.fetchall()] if cur.description else []
            return CallResult(rows=rows)
        except self._mc.Error as exc:
            return _error_result(exc)
        finally:
            cur.close()

    def snapshot(self, tables):  # type: ignore[no-untyped-def]
        out: dict[str, list[dict[str, Any]]] = {}
        cur = self._conn.cursor()
        try:
            for table in tables:
                _execute(cur, _select_all_sql(table, "`"))
                names = [d[0] for d in cur.description]
                got = [
                    {n: _normalize(v) for n, v in zip(names, r, strict=True)}
                    for r in cur.fetchall()
                ]
                out[table] = sorted(got, key=_sort_key)
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()
        return out

    def tables(self) -> list[str]:
        cur = self._conn.cursor()
        try:
            _execute(cur, _MY_TABLES)
            return [str(r[0]) for r in cur.fetchall()]
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def column_types(self, tables):  # type: ignore[no-untyped-def]
        out: dict[str, dict[str, str]] = {}
        cur = self._conn.cursor()
        try:
            for table in tables:
                _execute(cur, _MY_COLUMN_TYPES, [table])
                out[table] = {str(n): str(t) for n, t in cur.fetchall()}
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()
        return out

    def reset_session(self) -> None:
        """User variables, temporary tables and session settings back to a new
        connection's (``COM_RESET_CONNECTION``)."""
        try:
            self._conn.cmd_reset_connection()
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc

    def _aside(self) -> Any:
        server = self._server
        return self._mc.connect(**server.connect_args(_RUNNER, server.runner_password, _DATABASE))

    def interrupt(self) -> None:
        """``KILL CONNECTION`` of the runner's connection, from a second runner connection
        (a user may end its own connections), repeated until the connection is gone: a
        ``KILL QUERY`` can be lost, and ends only one ``SLEEP`` of a loop of them."""
        target = int(self._conn.connection_id)
        conn = self._aside()
        try:
            cur = conn.cursor()
            for _ in range(100):
                with contextlib.suppress(self._mc.Error):  # gone already: unknown thread
                    _execute(cur, "KILL CONNECTION %s", [target])
                _execute(cur, _MY_THREAD_ALIVE, [target])
                if not cur.fetchall():
                    return
                time.sleep(0.1)
            cur.close()
        finally:
            conn.close()

    def reconnect(self) -> None:
        """A new runner connection after :meth:`interrupt` ended the old one."""
        with contextlib.suppress(Exception):
            self._conn.close()
        server = self._server
        self._conn = self._mc.connect(
            **server.connect_args(_RUNNER, server.runner_password, _DATABASE)
        )

    def execute_aside(self, sql, params):  # type: ignore[no-untyped-def]
        conn = self._aside()
        try:
            cur = conn.cursor()
            try:
                _execute(cur, sql, list(params))
                rows = (
                    [[_normalize(c) for c in r] for r in cur.fetchall()] if cur.description else []
                )
                return CallResult(rows=rows)
            except self._mc.Error as exc:
                return _error_result(exc)
        finally:
            conn.close()

    def counters(self) -> tuple[tuple[str, Any, Any], ...]:
        """``(table, next AUTO_INCREMENT value, None)`` for every table that has one."""
        cur = self._conn.cursor()
        try:
            with contextlib.suppress(self._mc.Error):  # 5.7 has no such variable
                _execute(cur, _MY_STATS_NOW)
            _execute(cur, _MY_AUTO_INCREMENTS)
            return tuple((str(t), int(n), None) for t, n in cur.fetchall())
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def restore_counters(self, state):  # type: ignore[no-untyped-def]
        # A rollback does not give AUTO_INCREMENT values back. Setting the counter at or
        # below the highest value in use sets it just past that value, which, once the
        # rows are back as seeded, is where it was.
        cur = self._conn.cursor()
        try:
            for table, value, _ in state:
                target = _quote_table(table, "`")  # any spelling, the backtick doubled
                _execute(cur, f"ALTER TABLE {target} AUTO_INCREMENT = {int(value)}")
        except self._mc.Error as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}".strip(), _sqlstate(exc)) from exc
        finally:
            cur.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
        self._server.stop()


# --------------------------------------------------------------------------- binaries


def _run_version(exe: str | Path) -> str | None:
    try:
        return subprocess.run(  # nosec B603 - fixed arguments, no shell
            [str(exe), "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _pg_bin_major(bin_dir: Path) -> str | None:
    """The major of the PostgreSQL in ``bin_dir`` (needs ``postgres`` and ``initdb``)."""
    if not (os.access(bin_dir / "initdb", os.X_OK) and os.access(bin_dir / "postgres", os.X_OK)):
        return None
    out = _run_version(bin_dir / "postgres")
    match = re.search(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?", out or "")
    if not match:
        return None
    first, second = match.group(1), match.group(2)
    return first if int(first) >= 10 else f"{first}.{second or 0}"


def _pgserver_bin_dir() -> Path | None:
    try:
        import pgserver._commands as commands
    except ImportError:
        return None
    return Path(commands.POSTGRES_BIN_PATH)


def _name_major(key: str, prefix: str) -> str:
    """The major a ``MODERNIZER_..._<major>`` name claims (``_8_0`` -> ``8.0``)."""
    return key[len(prefix) :].replace("_", ".")


def _check_claim(key: str, prefix: str, found: str, what: str) -> bool:
    """Whether the binary's own major matches the one the variable name claims."""
    claimed = _name_major(key, prefix)
    if claimed == found:
        return True
    logger.warning("%s names major %s but %s reports %s; ignoring it", key, claimed, what, found)
    return False


def pg_candidates(pg_bin_dir: str | None = None) -> dict[str, tuple[str, Path | None]]:
    """PostgreSQL majors present here: ``{major: (where from, bin dir)}``.

    In order, the first for a major winning: the ``pg_bin_dir`` argument,
    ``MODERNIZER_PG_BIN_<major>`` (whose binary must really be that major),
    ``MODERNIZER_PG_BIN``, a ``postgres`` on ``PATH``, and last the ``pgserver``
    package (bin dir ``None``, because it starts through ``pgserver`` itself).
    """
    found: dict[str, tuple[str, Path | None]] = {}

    def add(origin: str, folder: str | Path | None, claims: bool = False) -> None:
        if not folder:
            return
        major = _pg_bin_major(Path(folder))
        if not major:
            logger.warning("%s (%s) has no usable initdb and postgres", origin, folder)
            return
        if claims and not _check_claim(origin, PG_BIN_PREFIX, major, "postgres --version"):
            return
        found.setdefault(major, (origin, Path(folder)))

    if pg_bin_dir:
        add("pg_bin_dir", pg_bin_dir)
    for key in sorted(os.environ):
        if key.startswith(PG_BIN_PREFIX) and key != PG_BIN_ENV:
            add(key, os.environ[key], claims=True)
    add(PG_BIN_ENV, os.environ.get(PG_BIN_ENV))
    on_path = shutil.which("postgres")
    if on_path:
        add("PATH", Path(on_path).parent)
    pgs = _pgserver_bin_dir()
    if pgs is not None:
        major = _pg_bin_major(pgs)
        if major and major not in found:
            found[major] = ("pgserver", None)
    return found


def _mysqld_major(mysqld: str) -> str | None:
    out = _run_version(mysqld)
    if out is None or _MARIADB_BANNER.search(out):
        return None  # MariaDB's mysqld is not MySQL: it numbers its own versions
    match = re.search(r"Ver\s+(\d+)\.(\d+)", out)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def find_mysqld(mysqld_path: str | None = None) -> str | None:
    """The one ``mysqld`` to use: the argument, ``MODERNIZER_MYSQLD``, then ``PATH``."""
    configured = mysqld_path or os.environ.get(MYSQLD_ENV)
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which("mysqld")


def mysqld_candidates(mysqld_path: str | None = None) -> dict[str, str]:
    """MySQL majors present here: ``{major: mysqld path}``.

    In order, the first for a major winning: the ``mysqld_path`` argument,
    ``MODERNIZER_MYSQLD_<major>`` (``_8_0`` for 8.0, whose binary must really be
    that major), then ``MODERNIZER_MYSQLD`` or a ``mysqld`` on ``PATH``.
    """
    found: dict[str, str] = {}

    def add(origin: str, exe: str | None, claims: bool = False) -> None:
        if not exe or not os.access(exe, os.X_OK):
            return
        major = _mysqld_major(exe)
        if not major:
            logger.warning("%s (%s) is not a usable mysqld (MariaDB is not MySQL)", origin, exe)
            return
        if claims and not _check_claim(origin, MYSQLD_PREFIX, major, "mysqld --version"):
            return
        found.setdefault(major, exe)

    add("mysqld_path", mysqld_path)
    for key in sorted(os.environ):
        if key.startswith(MYSQLD_PREFIX):
            add(key, os.environ[key], claims=True)
    add(MYSQLD_ENV, find_mysqld())
    return found


def available_majors(
    engine: str, *, mysqld_path: str | None = None, pg_bin_dir: str | None = None
) -> list[str]:
    """Exactly the majors ``start`` can run for this engine here, e.g. ``["14", "16"]``."""
    try:
        if engine == "postgresql":
            found: Mapping[str, Any] = pg_candidates(pg_bin_dir)
        elif engine == "mysql":
            found = mysqld_candidates(mysqld_path)
        else:
            return []
    except Exception:  # noqa: BLE001
        return []
    return sorted(found, key=_major_sort)


def available(
    engine: str,
    version: str,
    *,
    mysqld_path: str | None = None,
    pg_bin_dir: str | None = None,
) -> bool:
    """Whether ``start`` would find a binary for this engine and major version.

    The version is required: use ``available_majors`` to ask what is present,
    because "is any engine available" is never the question a proof needs.
    """
    major = _major(engine, version)
    if not major:
        raise ValueError("available() needs a version; use available_majors() for the list")
    return major in available_majors(engine, mysqld_path=mysqld_path, pg_bin_dir=pg_bin_dir)


# --------------------------------------------------------------------------- public interface

_LIVE: list[EngineHandle] = []


def _canonical(engine: str) -> str:
    engine = engine.lower()
    if engine in ("postgres", "aurora-postgresql", "aurora_postgresql"):
        return "postgresql"
    if engine == "aurora-mysql":
        return "mysql"
    return engine


def start(
    engine: str,
    version: str = "",
    *,
    newest: bool = False,
    mysqld_path: str | None = None,
    pg_bin_dir: str | None = None,
) -> EngineHandle:
    """Start a throwaway engine of this kind and major version.

    The major is required, so a proof never runs on a version the source does not
    have; ``newest=True`` asks for the newest present major instead. The picked
    major is logged and is ``handle.version``.

    Raises ``EngineUnavailable`` ("no engine for <engine> <version>") when no
    binary for that major is present, ``EngineStartError`` when the engine is
    present but does not start, and ``ValueError`` when no version is given.
    Anything started is stopped and removed again if this fails.
    """
    engine = _canonical(engine)
    if engine not in SUPPORTED_ENGINES:
        raise _unavailable(engine, version, "engine not supported")
    want = _major(engine, version)
    if not want and not newest:
        raise ValueError(
            f"start({engine!r}) needs the source's major version; "
            "pass newest=True to take the newest major present"
        )
    majors = available_majors(engine, mysqld_path=mysqld_path, pg_bin_dir=pg_bin_dir)
    if not majors:
        raise _unavailable(engine, version, "no binary found (" + _HOW_TO[engine] + ")")
    if want and want not in majors:
        raise _unavailable(engine, version, "only " + ", ".join(majors) + " present")
    want = want or majors[-1]
    # After the version checks, so a major nobody has is "no engine" even as root.
    if _is_root():
        raise EngineStartError(
            f"{engine} {version} was not started: running as root, where the private "
            "socket folder cannot stay 0700 (and initdb and mysqld refuse root); "
            "run as an ordinary user"
        )
    sweep_stale()
    workdir = _short_tempdir()
    server: Any = None
    backend: _Backend | None = None
    try:
        if engine == "postgresql":
            origin, bin_dir = pg_candidates(pg_bin_dir)[want]
            logger.info("disposable postgresql %s from %s", want, origin)
            server = (
                _PgserverServer(workdir, want)
                if bin_dir is None
                else _PgBinaryServer(workdir, bin_dir, want)
            )
            backend = _PostgresBackend(server)
            endpoint = f"unix:{workdir}"
        else:
            exe = mysqld_candidates(mysqld_path)[want]
            logger.info("disposable mysql %s from %s", want, exe)
            server = _MySQLServer(workdir, exe, want)
            backend = _MySQLBackend(server)
            endpoint = (
                f"unix:{backend.socket_path}"
                if backend.socket_path
                else f"tcp:127.0.0.1:{backend.port}"
            )
        handle = EngineHandle(
            engine, want, workdir, endpoint, getattr(backend, "pid", None), backend
        )
    except BaseException:  # includes KeyboardInterrupt: nothing is left running
        with contextlib.suppress(Exception):
            if backend is not None:
                backend.close()
            elif server is not None:
                server.stop()
        # The backstop for any server a constructor could not stop itself (the
        # pgserver path in particular); do not remove it as a duplicate of
        # _PgserverServer.stop, because the injection test relies on the pair.
        _stop_workdir_servers(workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    _LIVE.append(handle)
    return handle


def load(
    handle: EngineHandle,
    ddl: Sequence[str],
    rows: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    routines: Sequence[SourceRoutine] = (),
) -> None:
    """Create the tables (``ddl``: one statement each), insert ``rows``, create the routines.

    Everything here runs as the non-superuser role, because it is customer text.
    """
    _check_live(handle)
    handle._backend.load(ddl, rows or {}, routines)


def call(
    handle: EngineHandle,
    routine: SourceRoutine | str,
    args: Sequence[Any] = (),
    modes: Sequence[ArgMode] | None = None,
    casts: Sequence[str | None] | None = None,
) -> CallResult:
    """Call a loaded routine. A SQL error becomes ``CallResult.error``, so it can be compared.

    ``modes`` gives each argument's mode (all ``IN`` when omitted): on MySQL an ``OUT``
    or ``INOUT`` argument of a procedure goes through a session variable, and the
    variables' values come back as the last row. ``casts`` gives each argument's type on
    PostgreSQL (``integer``, ``numeric(10,2)``, ``app.mood``; None for none), written as
    ``%s::type`` so the call reaches that overload; a type name is checked first
    (``ValueError`` otherwise). MySQL has no overloads and writes none.
    """
    _check_live(handle)
    return handle._backend.call(routine, list(args), modes, casts)


def execute(handle: EngineHandle, sql: str, params: Sequence[Any] = ()) -> CallResult:
    """Run one statement as the non-superuser role, values bound (``%s`` marks): a test
    row change from ``row_change_sql``. A SQL error becomes ``CallResult.error``."""
    _check_live(handle)
    return handle._backend.execute(sql, list(params))


def snapshot(handle: EngineHandle, tables: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Every row of each table, normalised and sorted, for a row-by-row comparison."""
    _check_live(handle)
    return handle._backend.snapshot(tables)


def counters(handle: EngineHandle) -> tuple[tuple[str, Any, Any], ...]:
    """The state of every counter a rollback does not undo: PostgreSQL's sequences
    (``(name, last value, start value)``) and MySQL's ``AUTO_INCREMENT`` values
    (``(table, next value, None)``), for :func:`restore_counters` (#1944)."""
    _check_live(handle)
    return handle._backend.counters()


def restore_counters(handle: EngineHandle, state: Sequence[tuple[str, Any, Any]]) -> None:
    """Put every counter back as :func:`counters` read it, so each call of a side-by-side
    comparison draws the same sequence values on both engines. On MySQL this runs one
    ``ALTER TABLE`` per table, which commits: call it outside a transaction."""
    _check_live(handle)
    handle._backend.restore_counters(state)


def tables(handle: EngineHandle) -> list[str]:
    """The base tables of the routines' own schema (PostgreSQL ``public``, the MySQL
    database), so a table a call creates is seen (#1944)."""
    _check_live(handle)
    return handle._backend.tables()


def column_types(handle: EngineHandle, tables: Sequence[str]) -> dict[str, dict[str, str]]:
    """Each table's columns with the engine's own type (``numeric(10,2)``,
    ``decimal(10,2)``), so two tables of one shape and other types are told apart."""
    _check_live(handle)
    return handle._backend.column_types(tables)


def time_zones_loaded(handle: EngineHandle) -> bool:
    """Whether the engine knows named time zones (``Europe/Paris``): PostgreSQL always;
    MySQL when its zone tables were loaded at start."""
    server = getattr(handle._backend, "_server", None)
    return handle.engine == "postgresql" or bool(getattr(server, "time_zones", False))


def reset_session(handle: EngineHandle) -> None:
    """Put the session back as a new connection has it: on MySQL user variables,
    temporary tables and session settings are dropped (``COM_RESET_CONNECTION``); on
    PostgreSQL nothing is needed, because a rolled-back transaction undoes them."""
    _check_live(handle)
    handle._backend.reset_session()


class Limit:
    """What :func:`time_limit` yields: ``fired`` is True once the call ran out of time."""

    def __init__(self) -> None:
        self.fired = False


@contextlib.contextmanager
def time_limit(handle: EngineHandle, seconds: float) -> Iterator[Limit]:
    """Stop the statement running on ``handle`` when it takes longer than ``seconds``, and
    keep stopping it until it returns: PostgreSQL cancels it (57014); MySQL ends the
    connection (``KILL CONNECTION``, repeated until it is gone), and the handle gets a new
    one once the statement has returned. ``Limit.fired`` says whether it ran out of time;
    nothing is sent after the statement returned."""
    _check_live(handle)
    limit = Limit()
    done = threading.Event()

    def watch() -> None:
        if done.wait(seconds):
            return
        limit.fired = True
        while not done.is_set():
            with contextlib.suppress(Exception):
                handle._backend.interrupt()
            done.wait(0.5)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield limit
    finally:
        done.set()
        watcher.join(timeout=60)
        if limit.fired:
            handle._backend.reconnect()


def execute_aside(handle: EngineHandle, sql: str, params: Sequence[Any] = ()) -> CallResult:
    """Run one statement on a second, new runner connection, outside the handle's own
    transaction: what it reads is only what has been committed (#1944)."""
    _check_live(handle)
    return handle._backend.execute_aside(sql, list(params))


def stop(handle: EngineHandle) -> None:
    """Shut the engine down, then remove its temporary folder. Safe to call twice."""
    if handle.stopped:
        return
    handle.stopped = True
    try:
        handle._backend.close()
    finally:
        _stop_workdir_servers(handle.workdir)
        shutil.rmtree(handle.workdir, ignore_errors=True)
        if handle in _LIVE:
            _LIVE.remove(handle)


@contextlib.contextmanager
def running(engine: str, version: str = "", **kwargs: Any) -> Iterator[EngineHandle]:
    """``start`` then ``stop``, also on an exception or Ctrl-C."""
    handle = start(engine, version, **kwargs)
    try:
        yield handle
    finally:
        stop(handle)


def _check_live(handle: EngineHandle) -> None:
    if handle.stopped:
        raise EngineError("the engine is stopped")


@atexit.register
def _stop_all() -> None:
    for handle in list(_LIVE):
        with contextlib.suppress(Exception):
            stop(handle)
