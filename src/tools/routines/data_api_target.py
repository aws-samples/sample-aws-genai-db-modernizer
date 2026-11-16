# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The carried side of the routine proof on the throwaway Aurora test cluster (#1945).

The side-by-side comparison (:mod:`src.tools.routines.comparison`) talks to each side
through :class:`~src.tools.routines.proof_targets.ProofTarget`. :class:`DataApiTarget` is
that side on the #1946 test cluster, reached only through the RDS Data API: HTTPS calls to
the RDS service signed with the caller's IAM credentials, with the master secret RDS keeps
in Secrets Manager named by its ARN. This module opens no network path, never reads the
secret's value, and never logs a statement, a row value or a routine's text.

Scratch database. Each target creates one database with a job-scoped name
(:func:`scratch_name`: ``mdz_proof_<job hash>_<random>``, lower-case letters, digits and
underscores), loads the synthetic rows and the carried routines into it, and drops it in
:meth:`DataApiTarget.close`, which the comparison calls in a ``finally`` block, and again in
the constructor when anything after the create fails. :func:`drop_scratch` drops every
scratch database of a job (what a killed run left), and the proof calls it in its own
``finally``. On Aurora MySQL the scratch database is the routines' namespace, as the
disposable engine's ``src`` database is. On Aurora PostgreSQL it is a scratch database too,
not a scratch schema: the collected ``CREATE`` text (``pg_get_functiondef``,
``pg_get_triggerdef``) names every object with its schema, ``public.posts``, and the
comparison's screen refuses a ``CREATE`` that names any schema but the target's, so the
carried text runs as written in the scratch database's own ``public`` schema, as it does on
the disposable engine of the source.

Role (PostgreSQL). The master user creates a ``NOLOGIN NOSUPERUSER`` role per scratch
database that owns its ``public`` schema and the schemas the setup made for the source tables (#1970), and every load, ``CREATE`` and call runs under
``SET LOCAL ROLE`` to it, so a routine runs without the master user's privileges, as it runs
as a plain role on the disposable engine; the screen refuses a text that changes its role.
The role is dropped with the database.

User (MySQL). The run makes one restricted user (:func:`create_runner`): a generated
password kept only in a Secrets Manager secret of its own, tagged with the job id; the user is
created from the password's ``caching_sha2_password`` hash, computed here, since the Data API
takes no bound password in ``CREATE USER`` (#2298), and the Data API signs in as it once
before it is used. Each scratch database grants it that database and nothing else, and
the setup, the routines (which name it as their ``DEFINER``) and the calls run as it,
through the Data API with its secret's ARN; the master user only creates, grants and drops
the scratch database. :func:`drop_runner` drops the user and deletes its secret with no
recovery window.

Statements. Every statement is a module constant or built from checked, quoted identifiers
(``disposable_engine``'s helpers); every value is bound as a Data API parameter. The Data API
types a string parameter as ``varchar`` on PostgreSQL, where the disposable engine's driver
sends an untyped literal, so a PostgreSQL row is inserted, changed or matched through
``json_populate_record`` of the table's own row type: each value is read by its column's type,
as an untyped literal is. Each call runs in one Data API transaction (the connection is held
for it), opened with the session settings the disposable engine applies (time zone
``+05:47``, lock and statement timeouts; on MySQL also ``SET TIMESTAMP`` and the random seed),
and rolled back.

Errors come back as the Data API's ``DatabaseErrorException`` text, from which the SQLSTATE,
MySQL's error number and the primary message are read; the detail, hint and the constraint
an error names are not reported through the Data API, so the comparison matches errors on
SQLSTATE, error number and primary message when this side is involved (``error_fields``
False), and says so in the coverage limits.

Strict check (PostgreSQL). When the cluster offers the ``plpgsql_check`` extension, it is
created in the scratch database and every PL/pgSQL routine (a trigger function with its
trigger's table) is checked with ``plpgsql_check_function_tb``. Aurora PostgreSQL does not
offer it (16.15 lists ``plpgsql`` and ``amcheck`` only), so there each PL/pgSQL routine's
``CREATE`` runs once more with ``plpgsql.extra_errors = 'all'`` (PL/pgSQL's own extra checks,
a shadowed variable among them, as errors) in a transaction that is rolled back. Either
check's errors are recorded per routine id (``strict_findings``) by SQLSTATE, never by
message.

Values. The Data API returns no ``interval``, no ``NaN`` or infinite number, no infinite
timestamp and no unsigned ``BIGINT`` above 2**63 - 1, so a table is read column by column as
text (``HEX`` for MySQL bytes) and converted by its type here, as the disposable engine's
driver converts it. On Aurora MySQL the setup and the routines load statement by statement,
outside a transaction: a DDL statement's implicit commit ends the Data API's own savepoints.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from src.tools.database import disposable_engine as de
from src.tools.database.mysql_grants import grant_database
from src.tools.routines.proof_inputs import ProofCall, ProofSetup, run_call
from src.tools.routines.proof_targets import (
    CALL_TIMEOUT_S,
    SESSION_TIME_ZONE,
    DisposableEngineTarget,
    ProofTarget,
    ProofTargetError,
    RoleChanged,
    TargetFactory,
)

__all__ = [
    "SCRATCH_PREFIX",
    "DataApiClient",
    "DataApiTarget",
    "StrictFindings",
    "data_api_target",
    "drop_scratch",
    "parse_error",
    "scratch_name",
    "scratch_prefix",
]

SCRATCH_PREFIX = "mdz_proof_"
_SCRATCH = re.compile(r"^mdz_proof_[0-9a-f]{10}_[0-9a-f]{8}$")
_PREFIX = re.compile(r"^mdz_proof_[0-9a-f]{10}_$")
_SQLSTATE = re.compile(r"SQLState:\s*([0-9A-Z]{5})")
_ERRNO = re.compile(r"(?:Error code|error code|ErrorCode|errno)\s*[:=]?\s*(\d+)")
_PG_PREFIX = re.compile(r"^\s*(?:ERROR|FATAL):\s*")
_PG_PARTS = re.compile(r"\n\s*(Detail|Hint|Where|Position|Call getNextException)[: ]", re.I)
_PLACEHOLDER = re.compile(r"%s(?:::([A-Za-z_][A-Za-z0-9_]*))?")
_TIMEOUT_CODES = {"StatementTimeoutException"}
#: A cluster at 0 ACUs answers this while it resumes (about 15 seconds): the call is retried.
_RESUMING = "DatabaseResumingException"
_RESUME_TRIES, _RESUME_WAIT_S = 24, 5.0


def _resumed(call: Any, sleep: Any = None, /, **kwargs: Any) -> Any:
    """``call(**kwargs)``, retried while the paused cluster resumes."""
    for attempt in range(_RESUME_TRIES):
        try:
            return call(**kwargs)
        except Exception as exc:  # noqa: BLE001 - only the resume is retried
            if _code(exc) != _RESUMING or attempt == _RESUME_TRIES - 1:
                raise
            (sleep or time.sleep)(_RESUME_WAIT_S)
    raise AssertionError("unreachable")  # pragma: no cover


_PLPGSQL_CHECK = "plpgsql_check"
#: The strict check where the cluster does not offer plpgsql_check (Aurora PostgreSQL does
#: not, checked on 16.15): each PL/pgSQL routine is compiled again with every extra check of
#: PL/pgSQL itself turned into an error, in a transaction that is rolled back.
EXTRA_ERRORS = "plpgsql_extra_errors"
_PLPGSQL = re.compile(r"\bLANGUAGE\s+'?plpgsql'?", re.I)
#: A CREATE that is not OR REPLACE meets itself: the compile check cannot run on it.
_EXISTS = {"42723", "42P07", "42710"}
#: The local zone of every call (``SESSION_TIME_ZONE``), as Python writes it.
_ZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=47))
_MARK = {"postgresql": '"', "mysql": "`"}
# Every PL/pgSQL routine of the scratch database's public schema, by oid, its routine id
# (schema.name(argument types)) and, for a trigger function, its first trigger's table.
# MySQL: columns and counters read as text, since the Data API returns no unsigned BIGINT
# above 2**63 - 1 (a synthetic edge row takes AUTO_INCREMENT there).
_MY_COLUMNS = (
    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
)
_MY_AUTO_INCREMENTS = (
    "SELECT TABLE_NAME, CAST(AUTO_INCREMENT AS CHAR) FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND AUTO_INCREMENT IS NOT NULL ORDER BY TABLE_NAME"
)
_MY_BYTES = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob", "bit", "geometry"}
_MY_INTEGERS = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"}
_PG_COLUMNS = (
    "SELECT a.attname::text, t.typname::text, t.typcategory::text FROM pg_attribute a "
    "JOIN pg_type t ON t.oid = a.atttypid WHERE a.attrelid = CAST(%s AS regclass) "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
)
_PG_PLPGSQL_ROUTINES = (
    "SELECT p.oid::bigint, n.nspname || '.' || p.proname || '(' || "
    "oidvectortypes(p.proargtypes) || ')', (SELECT t.tgrelid::bigint FROM pg_trigger t "
    "WHERE t.tgfoid = p.oid ORDER BY t.oid LIMIT 1) FROM pg_proc p JOIN pg_namespace n ON "
    "n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang WHERE n.nspname = 'public' "
    "AND l.lanname = 'plpgsql' AND (p.prorettype <> 'trigger'::regtype OR EXISTS (SELECT 1 "
    "FROM pg_trigger t WHERE t.tgfoid = p.oid)) ORDER BY p.oid"
)


class DataApiClient(Protocol):
    """What this module calls on a boto3 ``rds-data`` client."""

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]: ...

    def batch_execute_statement(self, **kwargs: Any) -> dict[str, Any]: ...

    def begin_transaction(self, **kwargs: Any) -> dict[str, Any]: ...

    def rollback_transaction(self, **kwargs: Any) -> dict[str, Any]: ...

    def commit_transaction(self, **kwargs: Any) -> dict[str, Any]: ...


#: Per routine id: each strict-check finding of level error, as ``(level, sqlstate)``.
StrictFindings = dict[str, list[tuple[str, str]]]


def _text_key(text: str) -> str:
    """How a routine's carried text is matched to its routine id, in memory only."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_key(text: str) -> str:
    """:func:`_text_key`, for the caller that builds ``routine_ids``."""
    return _text_key(text)


def scratch_prefix(job_id: str) -> str:
    """The prefix every scratch database of ``job_id`` starts with."""
    return SCRATCH_PREFIX + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:10] + "_"


def scratch_name(job_id: str) -> str:
    """A new scratch database name for ``job_id``: its prefix and 8 random hex digits."""
    name = scratch_prefix(job_id) + secrets.token_hex(4)
    if not _SCRATCH.fullmatch(name):  # pragma: no cover - built above
        raise ValueError("not a scratch database name")
    return name


def _code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str((response.get("Error") or {}).get("Code") or "") or None
    return None


def _message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str((response.get("Error") or {}).get("Message") or "")
    return str(exc)


def parse_error(exc: BaseException) -> de.CallResult:
    """The :class:`~src.tools.database.disposable_engine.CallResult` of a Data API
    ``DatabaseErrorException``: the SQLSTATE, MySQL's error number and the primary
    message (``ERROR:`` and the ``; SQLState: ...`` tail taken off, the detail and hint
    lines dropped), which can quote routine text and is never reported."""
    text = _message(exc)
    state = _SQLSTATE.search(text)
    errno = _ERRNO.search(text)
    primary = text[: state.start()] if state else text
    primary = _PG_PREFIX.sub("", primary)
    primary = _PG_PARTS.split(primary, 1)[0]
    primary = re.sub(r"^\s*Database error code:\s*\d+\.\s*Message:\s*", "", primary)
    primary = primary.strip().rstrip(";").strip()
    return de.CallResult(
        error=f"DatabaseErrorException: {text.strip()}",
        sqlstate=state.group(1) if state else None,
        errno=int(errno.group(1)) if errno and int(errno.group(1)) > 0 else None,
        message=primary or None,
    )


# --------------------------------------------------------------------------- values


def _param(name: str, value: Any) -> dict[str, Any]:
    """One bound Data API parameter."""
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return {"name": name, "value": {"doubleValue": value}}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"name": name, "value": {"blobValue": bytes(value)}}
    if isinstance(value, Decimal):
        return {"name": name, "value": {"stringValue": str(value)}, "typeHint": "DECIMAL"}
    if isinstance(value, (dict, list)):
        return {"name": name, "value": {"stringValue": json.dumps(value)}, "typeHint": "JSON"}
    return {"name": name, "value": {"stringValue": str(value)}}


def _bind(sql: str, params: Sequence[Any]) -> tuple[str, list[dict[str, Any]]]:
    """``sql`` with each ``%s`` mark (``%s::type`` with a plain type name) as a named Data
    API parameter, and the parameters; refused when the counts differ."""
    out: list[dict[str, Any]] = []
    values = list(params)

    def mark(match: re.Match[str]) -> str:
        i = len(out)
        if i >= len(values):
            raise ValueError("more marks than values")
        out.append(_param(f"p{i}", values[i]))
        return f"CAST(:p{i} AS {match.group(1)})" if match.group(1) else f":p{i}"

    if "%%" in sql:
        raise ValueError("a statement here never holds an escaped percent sign")
    text = _PLACEHOLDER.sub(mark, sql)
    if len(out) != len(values):
        raise ValueError("fewer marks than values")
    return text, out


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    return str(value)


def _row_json(row: Mapping[str, Any]) -> str:
    return json.dumps(dict(row), default=_json_default)


def _field(field: Mapping[str, Any]) -> Any:
    if field.get("isNull"):
        return None
    for key in ("booleanValue", "longValue", "doubleValue", "stringValue"):
        if key in field:
            return field[key]
    if "blobValue" in field:
        return bytes(field["blobValue"])
    if "arrayValue" in field:
        return _array(field["arrayValue"])
    return None


def _array(value: Mapping[str, Any]) -> list[Any]:
    for key in ("booleanValues", "longValues", "doubleValues", "stringValues"):
        if key in value:
            return list(value[key])
    return [_array(v) for v in value.get("arrayValues") or []]


def _as_is(raw: Any, _type_name: str) -> Any:
    return raw


_PG_INTEGERS = {"int2", "int4", "int8", "oid"}
_PG_FLOATS = {"float4", "float8"}


def _pg_text(text: Any, type_name: str, category: str) -> Any:
    """A PostgreSQL value read as its text output, as psycopg2 converts that type: integers
    and floats (``NaN``, ``Infinity`` included) as numbers, ``bool``, dates and times in ISO
    form (an infinite one as Python's highest or lowest), ``bytea`` in hex, ``jsonb`` and arrays
    (read through ``to_jsonb``) parsed; every other type, ``interval`` and ``json`` included,
    as its text."""
    if text is None:
        return None
    if not isinstance(text, str):
        return de._normalize(text)
    t = type_name.lower()
    try:
        if category == "A":
            return de._normalize(json.loads(text))
        if t in _PG_INTEGERS:
            return int(text)
        if t in _PG_FLOATS:
            return float(text)
        if t == "bool":
            return text in ("t", "true")
        if t == "jsonb":
            return de._normalize(json.loads(text))
        if t == "bytea":
            return text[2:] if text.startswith("\\x") else text
        if t in ("date", "timestamp", "timestamptz") and text in ("infinity", "-infinity"):
            high = text == "infinity"
            kind = datetime.date if t == "date" else datetime.datetime
            return (kind.max if high else kind.min).isoformat()
        if t == "date":
            return datetime.date.fromisoformat(text).isoformat()
        if t in ("timestamp", "timestamptz"):
            return _pg_value(text, t)
        if t in ("time", "timetz"):
            return datetime.time.fromisoformat(text).isoformat()
    except ValueError:
        return text
    return text


def _my_text(text: Any, data_type: str) -> Any:
    """A MySQL value read as text (``HEX`` for bytes), as MySQL Connector converts that
    type: integers, floats, ``TIME`` as seconds, ``BIT`` as an integer, bytes in hex, dates
    and times in ISO form; ``DECIMAL`` and every other type as its text."""
    if text is None or not isinstance(text, str):
        return de._normalize(text)
    t = data_type.lower()
    try:
        if t in _MY_INTEGERS:
            return int(text)
        if t in ("float", "double", "real"):
            return float(text)
        if t == "bit":
            return int(text or "0", 16)
        if t in _MY_BYTES:
            return text.lower()
        if t in ("datetime", "timestamp", "time"):
            return _my_value(text, t)
    except ValueError:
        return text
    return text


def _pg_value(raw: Any, type_name: str) -> Any:
    """A PostgreSQL value as the disposable engine's driver gives it, normalised."""
    t = type_name.lower().lstrip("_")
    if raw is None or not isinstance(raw, str):
        return de._normalize(raw)
    try:
        if t == "timestamp":
            return datetime.datetime.fromisoformat(raw).isoformat()
        if t == "timestamptz":
            moment = datetime.datetime.fromisoformat(raw)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.UTC)
            return moment.astimezone(_ZONE).isoformat()
        if t in ("time", "timetz"):
            return datetime.time.fromisoformat(raw).isoformat()
        if t == "jsonb":
            return de._normalize(json.loads(raw))
        if t == "bytea" and raw.startswith("\\x"):
            return raw[2:]
    except ValueError:
        return raw
    return raw


def _my_value(raw: Any, type_name: str) -> Any:
    """A MySQL value as the disposable engine's driver gives it, normalised."""
    t = type_name.upper().split()[0] if type_name else ""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            if t in ("DATETIME", "TIMESTAMP"):
                return datetime.datetime.fromisoformat(raw).isoformat()
            if t == "TIME":
                sign = -1 if raw.startswith("-") else 1
                h, m, s = raw.lstrip("-").split(":")
                return sign * (int(h) * 3600 + int(m) * 60 + float(s))
            if t in ("YEAR",):
                return int(raw)
        except ValueError:
            return raw
        return raw
    return de._normalize(raw)


#: MySQL's type names in the Data API's result metadata, as the driver's field types.
_MY_FIELD_TYPES = {
    "TINYINT": "TINY", "SMALLINT": "SHORT", "MEDIUMINT": "INT24", "INT": "LONG",
    "INTEGER": "LONG", "BIGINT": "LONGLONG", "FLOAT": "FLOAT", "DOUBLE": "DOUBLE",
    "DECIMAL": "NEWDECIMAL", "CHAR": "STRING", "VARCHAR": "VAR_STRING", "BINARY": "STRING",
    "VARBINARY": "VAR_STRING", "TINYTEXT": "BLOB", "TEXT": "BLOB", "MEDIUMTEXT": "BLOB",
    "LONGTEXT": "BLOB", "TINYBLOB": "BLOB", "BLOB": "BLOB", "MEDIUMBLOB": "BLOB",
    "LONGBLOB": "BLOB", "DATE": "DATE", "DATETIME": "DATETIME", "TIMESTAMP": "TIMESTAMP",
    "TIME": "TIME", "YEAR": "YEAR", "BIT": "BIT", "JSON": "JSON", "ENUM": "STRING",
    "SET": "STRING", "GEOMETRY": "GEOMETRY", "BOOLEAN": "TINY", "BOOL": "TINY",
}  # fmt: skip


# --------------------------------------------------------------------------- backend


class _DataApiBackend(de._Backend):
    """``disposable_engine``'s backend calls, through the Data API, in one scratch
    database. A statement runs in the open transaction when there is one."""

    def __init__(self, client: DataApiClient, cluster_arn: str, secret_arn: str, engine: str,
                 database: str, role: str | None, timeout_s: float) -> None:  # fmt: skip
        self._client = client
        self._arn = {"resourceArn": cluster_arn, "secretArn": secret_arn}
        self.engine = engine
        self.database = database
        self.role = role
        self._mark = _MARK[engine]
        self._timeout = timeout_s
        self._tx: str | None = None
        self._kinds: dict[str, str] = {}
        self._no_run: dict[str, str] = {}
        self._pg_types: dict[str, str] = {}

    # ---- the Data API
    def _call(self, sql: str, params: Sequence[Any] = (), *, aside: bool = False,
              meta: bool = False) -> dict[str, Any]:  # fmt: skip
        text, bound = _bind(sql, params)
        kwargs: dict[str, Any] = {**self._arn, "sql": text}
        if self.database:  # none for the runner's sign-in check: it has no grant yet
            kwargs["database"] = self.database
        if bound:
            kwargs["parameters"] = bound
        if meta:
            kwargs["includeResultMetadata"] = True
        if self._tx is not None and not aside:
            kwargs["transactionId"] = self._tx
        got: dict[str, Any] = _resumed(self._client.execute_statement, **kwargs)
        return got

    def _rows(
        self, response: Mapping[str, Any], *, as_text: bool = False
    ) -> tuple[list[list[Any]], list[str], list[str]]:
        names = [
            str(c.get("label") or c.get("name") or "") for c in response.get("columnMetadata") or []
        ]
        types = [str(c.get("typeName") or "") for c in response.get("columnMetadata") or []]
        convert: Callable[[Any, str], Any] = _pg_value if self.engine == "postgresql" else _my_value
        if as_text:
            convert = _as_is
        rows = []
        for record in response.get("records") or []:
            values = [_field(f) for f in record]
            rows.append(
                [convert(v, types[i] if i < len(types) else "") for i, v in enumerate(values)]
            )
        return rows, names, types

    def begin(self) -> None:
        if self._tx is not None:
            self.rollback()
        got = _resumed(self._client.begin_transaction, **self._arn, database=self.database)
        self._tx = str(got["transactionId"])
        if self.engine == "postgresql":
            if self.role:
                self._call(f"SET LOCAL ROLE {de._quote(self.role, self._mark)}")
            if self._namespaces:  # public first, then the source schemas (#1970)
                paths = ", ".join(de._quote(n, '"') for n in ["public", *self._namespaces])
                self._call(f"SET LOCAL search_path = {paths}")
            for statement in (
                f"SET LOCAL TIME ZONE INTERVAL '{SESSION_TIME_ZONE}' HOUR TO MINUTE",
                "SET LOCAL lock_timeout = '5s'",
                f"SET LOCAL statement_timeout = '{int(self._timeout * 1000)}ms'",
            ):
                self._call(statement)

    def rollback(self) -> None:
        tx, self._tx = self._tx, None
        if tx is not None:
            try:
                self._client.rollback_transaction(**self._arn, transactionId=tx)
            except Exception as exc:  # noqa: BLE001 - a transaction the call ended is gone
                if _code(exc) not in ("NotFoundException", "BadRequestException"):
                    raise ProofTargetError("the test cluster refused a rollback") from None

    def commit(self) -> None:
        tx, self._tx = self._tx, None
        if tx is not None:
            self._client.commit_transaction(**self._arn, transactionId=tx)

    def _guarded(self, sql: str, params: Sequence[Any] = (), *, aside: bool = False,
                 meta: bool = False) -> dict[str, Any]:  # fmt: skip
        """A harness statement: a SQL error is an :class:`EngineError` with its SQLSTATE and
        a fixed message (the server's can quote routine text)."""
        try:
            return self._call(sql, params, aside=aside, meta=meta)
        except Exception as exc:  # noqa: BLE001 - every failure is reported the same way
            if _code(exc) == "DatabaseErrorException":
                raise de.EngineError("the test cluster refused a statement",
                                     parse_error(exc).sqlstate) from None  # fmt: skip
            raise de.EngineError(
                f"the Data API failed ({_code(exc) or type(exc).__name__})"
            ) from None

    # ---- disposable_engine's backend calls
    def execute(self, sql, params):  # type: ignore[no-untyped-def]
        word = " ".join(sql.split()).upper()
        if word in ("BEGIN", "START TRANSACTION"):
            self.begin()
            return de.CallResult()
        if word == "ROLLBACK":
            self.rollback()
            return de.CallResult()
        return self._result(sql, params)

    def _result(self, sql: str, params: Sequence[Any], *, aside: bool = False) -> de.CallResult:
        try:
            response = self._call(sql, params, aside=aside, meta=True)
        except Exception as exc:  # noqa: BLE001 - a SQL error is a result
            code = _code(exc)
            if code == "DatabaseErrorException":
                return parse_error(exc)
            if code in _TIMEOUT_CODES:
                self._tx = None  # the Data API ends a transaction whose statement timed out
                return de.CallResult(error="the call ran past its time limit", timed_out=True)
            raise ProofTargetError(f"the Data API failed ({code or type(exc).__name__})") from None
        rows, names, types = self._rows(response)
        if not response.get("columnMetadata"):
            return de.CallResult()
        return de.CallResult(rows=rows, column_types=(self._types(types),),
                             column_names=(tuple(names),))  # fmt: skip

    def _types(self, names: Sequence[str]) -> tuple[str, ...]:
        if self.engine == "mysql":
            return tuple(
                _MY_FIELD_TYPES.get(n.upper().split()[0] if n else "", n.upper()) for n in names
            )
        if not self._pg_types:
            response = self._guarded(
                "SELECT typname::text, pg_catalog.format_type(oid, NULL) FROM pg_type "
                "WHERE typnamespace IN ('pg_catalog'::regnamespace, 'public'::regnamespace)", meta=True
            )  # fmt: skip
            rows, _, _ = self._rows(response)
            self._pg_types = {str(a): str(b) for a, b in rows}
        return tuple(self._pg_types.get(n, n) for n in names)

    def execute_aside(self, sql, params):  # type: ignore[no-untyped-def]
        return self._result(sql, params, aside=True)

    def load(self, ddl, rows, routines):  # type: ignore[no-untyped-def]
        """Tables, rows and routines: on PostgreSQL in one transaction under the role; on
        MySQL each statement on its own, since a DDL statement's implicit commit ends the
        Data API's own savepoints inside a transaction."""
        if self.engine == "postgresql":
            self.begin()
        try:
            for statement in ddl:
                self._guarded(statement)
            for table, table_rows in rows.items():
                self._insert(table, list(table_rows))
            if self.engine == "postgresql":
                for table in rows:
                    self._advance_sequences(table)
            for routine in routines:
                self._kinds[routine.name] = routine.kind
                self._guarded(routine.create_sql)
                if routine.kind == "event":
                    self._make_event_runner(routine.name)
            self.commit()
        finally:
            self.rollback()

    def _insert(self, table: str, table_rows: list[Mapping[str, Any]]) -> None:
        """Batched: one ``BatchExecuteStatement`` per table and column set."""
        groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
        for row in table_rows:
            groups.setdefault(tuple(row), []).append(row)
        for cols, group in groups.items():
            if self.engine == "postgresql":
                names = ", ".join(de._quote(c, '"') for c in cols)
                target = de._quote(table, '"')
                sql = (f"INSERT INTO {target} ({names}) SELECT {names} FROM "  # nosec B608 - quoted identifiers, a bound row
                       f"json_populate_record(NULL::{target}, CAST(:row AS json))")  # fmt: skip
                sets = [[{"name": "row", "value": {"stringValue": _row_json(r)}}] for r in group]
            else:
                text, _ = _bind(de._insert_sql(table, cols, "`"), [None] * len(cols))
                sql = text
                sets = [
                    _bind(de._insert_sql(table, cols, "`"), [r[c] for c in cols])[1] for r in group
                ]
            extra = {"transactionId": self._tx} if self._tx is not None else {}
            for start in range(0, len(sets), 100):
                try:
                    _resumed(
                        self._client.batch_execute_statement,
                        **self._arn, sql=sql, database=self.database,
                        parameterSets=sets[start : start + 100], **extra,
                    )  # fmt: skip
                except Exception as exc:  # noqa: BLE001
                    state = (
                        parse_error(exc).sqlstate
                        if _code(exc) == "DatabaseErrorException"
                        else None
                    )
                    raise de.EngineError("the synthetic rows did not load", state) from None

    def _advance_sequences(self, table: str) -> None:
        quoted = de._quote(table, '"')
        got = self._guarded(de._PG_TABLE_SEQUENCES.replace("%s::regclass", "CAST(%s AS regclass)"),
                            [quoted, quoted], meta=True)  # fmt: skip
        for column, sequence in self._rows(got)[0]:
            if not de._IDENT.match(str(column)):
                continue
            col = de._quote(str(column), '"')
            highest = f"SELECT max({col}) AS m FROM {quoted}"  # nosec B608 - quoted identifiers
            self._guarded(f"SELECT pg_catalog.setval(CAST(%s AS regclass), m, true) FROM ({highest}) s "  # nosec B608 - quoted identifiers, the row a bound parameter
                          "WHERE m IS NOT NULL", [sequence])  # fmt: skip

    def _make_event_runner(self, event: str) -> None:
        key = de._event_key(event)
        try:
            name = de._parts(event)[-1]
        except ValueError:
            self._no_run[key] = "the event's name is not a plain identifier, so it is not run"
            return
        found = self._rows(self._guarded(
            "SELECT EVENT_DEFINITION FROM information_schema.EVENTS "
            "WHERE EVENT_SCHEMA = DATABASE() AND LOWER(EVENT_NAME) = %s", [name.casefold()], meta=True,
        ))[0]  # fmt: skip
        if not found or not found[0][0]:
            self._no_run[key] = ("the server dropped the event when it was created (its time had "
                                 "passed), so there is no body to run")  # fmt: skip
            return
        runner = de._quote(de.event_runner_name(name), "`")
        self._guarded(f"DROP PROCEDURE IF EXISTS {runner}")
        self._guarded(
            f"CREATE PROCEDURE {runner}() {found[0][0]}"
        )  # nosec B608 - the event's own body

    def call(self, routine, args, modes=None, casts=None):  # type: ignore[no-untyped-def]
        name, kind = de._routine_kind(self._kinds, routine)
        if self.engine == "postgresql":
            if kind == "event":
                raise ValueError("an event runs on MySQL only")
            casts = list(casts or [None] * len(args))
            if len(casts) != len(args):
                raise ValueError("one cast (or None) per argument")
            slots = [f"CAST(%s AS {de.pg_cast_type(c)})" if c else "%s" for c in casts]
            return self._result(de._call_sql(name, kind, len(args), '"', "postgresql", slots), args)
        if kind == "event":
            if args or modes or casts:
                raise ValueError("an event run takes no argument")
            if (why := self._no_run.get(de._event_key(name))) is not None:
                return de.CallResult(error=why, not_run=True)
            name, kind = de.event_runner_name(name), "procedure"
        modes = list(modes or ["IN"] * len(args))
        if len(modes) != len(args):
            raise ValueError("one mode per argument")
        slots = [de._MY_ARG_VAR % i if m != "IN" else "%s" for i, m in enumerate(modes)]
        sql = de._call_sql(name, kind, len(args), "`", "mysql", slots)
        if kind != "procedure":
            return self._result(sql, args)
        outs = [i for i, m in enumerate(modes) if m != "IN"]
        for i in outs:
            got = self._result(
                f"SET {de._MY_ARG_VAR % i} = %s", [args[i] if modes[i] == "INOUT" else None]
            )
            if got.error is not None:
                return got
        done = self._result(sql, [a for a, m in zip(args, modes, strict=True) if m == "IN"])
        if done.error is not None:
            return done
        rows, sizes = list(done.rows), [len(done.rows)] if done.column_types else []
        kinds, labels = list(done.column_types), list(done.column_names)
        if outs:
            read = self._result(
                "SELECT " + ", ".join(de._MY_ARG_VAR % i for i in outs), []
            )  # nosec B608 - fixed names
            if read.error is not None:
                return read
            rows += read.rows
            sizes.append(len(read.rows))
            kinds += list(read.column_types)
            labels += list(read.column_names)
        return de.CallResult(rows=rows, result_sets=tuple(sizes), column_types=tuple(kinds),
                             column_names=tuple(labels))  # fmt: skip

    def snapshot(self, tables):  # type: ignore[no-untyped-def]
        """Every row of each table, as the disposable engine's driver reads it. On PostgreSQL
        each column is read as its text and converted by its type here: the Data API returns
        no ``interval``, no ``NaN`` or infinite float and no infinite timestamp."""
        out: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            if self.engine == "postgresql":
                columns = self._pg_columns(table)
                if not columns:
                    out[table] = []
                    continue
                select = ", ".join(
                    (f"pg_catalog.to_jsonb(q.{de._quote_table(n, '"')})::text" if cat == "A"
                     else f"q.{de._quote_table(n, '"')}::text")
                    for n, _, cat in columns
                )  # fmt: skip
                target = de._select_all_sql(table, '"')[len("SELECT * FROM ") :]
                sql = f"SELECT {select} FROM {target} q"  # nosec B608 - quoted identifiers
                rows, _, _ = self._rows(self._guarded(sql, meta=True), as_text=True)
                got = [
                    {n: _pg_text(v, typ, cat) for (n, typ, cat), v in zip(columns, r, strict=True)}
                    for r in rows
                ]
            else:
                columns = self._my_columns(table)
                if not columns:
                    out[table] = []
                    continue
                select = ", ".join(
                    (f"HEX(q.{de._quote_table(n, '`')})" if t in _MY_BYTES
                     else f"CAST(q.{de._quote_table(n, '`')} AS CHAR)")
                    for n, t in columns
                )  # fmt: skip
                target = de._select_all_sql(table, "`")[len("SELECT * FROM ") :]
                sql = f"SELECT {select} FROM {target} q"  # nosec B608 - quoted identifiers
                rows, _, _ = self._rows(self._guarded(sql, meta=True), as_text=True)
                got = [
                    {n: _my_text(v, t) for (n, t), v in zip(columns, r, strict=True)} for r in rows
                ]
            out[table] = sorted(got, key=de._sort_key)
        return out

    def _my_columns(self, table: str) -> list[tuple[str, str]]:
        """Each column's name and data type, in order."""
        got = self._guarded(_MY_COLUMNS, [table], meta=True)
        return [(str(n), str(t).lower()) for n, t in self._rows(got, as_text=True)[0]]

    def _pg_columns(self, table: str) -> list[tuple[str, str, str]]:
        """Each column's name, type name and type category, in order."""
        got = self._guarded(_PG_COLUMNS, [de.qualified_table(table, self._namespaces, '"')],
                            meta=True)  # fmt: skip
        return [(str(n), str(t), str(c)) for n, t, c in self._rows(got, as_text=True)[0]]

    def tables(self) -> list[str]:
        sql = de.pg_tables_sql(self._namespaces) if self.engine == "postgresql" else de._MY_TABLES
        return [str(r[0]) for r in self._rows(self._guarded(sql, meta=True))[0]]

    def column_types(self, tables):  # type: ignore[no-untyped-def]
        out: dict[str, dict[str, str]] = {}
        for table in tables:
            if self.engine == "postgresql":
                sql = de._PG_COLUMN_TYPES.replace("%s::regclass", "CAST(%s AS regclass)")
                got = self._guarded(sql, [de.qualified_table(table, self._namespaces, '"')],
                                    meta=True)  # fmt: skip
            else:
                got = self._guarded(de._MY_COLUMN_TYPES, [table], meta=True)
            out[table] = {str(n): str(t) for n, t in self._rows(got)[0]}
        return out

    def counters(self) -> tuple[tuple[str, Any, Any], ...]:
        if self.engine == "postgresql":
            rows = self._rows(self._guarded(de._PG_SEQUENCE_STATE, meta=True))[0]
            return tuple((str(n), last, start) for n, last, start in rows)
        try:
            self._guarded(de._MY_STATS_NOW)
        except de.EngineError:
            pass
        rows = self._rows(self._guarded(_MY_AUTO_INCREMENTS, meta=True), as_text=True)[0]
        return tuple((str(t), int(n), None) for t, n in rows)

    def restore_counters(self, state):  # type: ignore[no-untyped-def]
        for name, value, start in state:
            if self.engine == "postgresql":
                called = value is not None
                self._guarded("SELECT pg_catalog.setval(CAST(%s AS regclass), %s, %s)",
                              [name, value if called else start, called])  # fmt: skip
            else:
                target = de._quote_table(name, "`")
                self._guarded(f"ALTER TABLE {target} AUTO_INCREMENT = {int(value)}")

    def reset_session(self) -> None:
        return None  # each call opens its own transaction and sets its session itself

    def interrupt(self) -> None:
        return None  # the statement timeout (PostgreSQL) and the Data API's own limit end it

    def reconnect(self) -> None:
        self._tx = None

    def close(self) -> None:
        self.rollback()


# --------------------------------------------------------------------------- target


class DataApiTarget(DisposableEngineTarget):
    """The carried side on the Aurora test cluster (module docstring)."""

    error_fields = False

    def __init__(self, client: DataApiClient, *, cluster_arn: str, secret_arn: str, engine: str,
                 version: str, job_id: str, admin_database: str | None,
                 call_timeout_s: float = CALL_TIMEOUT_S, strict: StrictFindings | None = None,
                 strict_check: list[str] | None = None,
                 routine_ids: Mapping[str, str] | None = None,
                 runner: RunnerAccount | None = None) -> None:  # fmt: skip
        if engine not in _MARK:
            raise ValueError(f"not an engine the proof runs on: {engine!r}")
        self.engine = engine
        self.version = version
        self.database = scratch_name(job_id)
        self.description = f"an Aurora {'PostgreSQL' if engine == 'postgresql' else 'MySQL'} " \
                           f"{version} test cluster"  # fmt: skip
        self.namespace = "public" if engine == "postgresql" else self.database
        self.random_seeded = engine == "postgresql"
        self.named_time_zones = True
        self.notes = ()
        self._timeout = call_timeout_s
        self._counters = ()
        self._open = False
        self._strict = strict if strict is not None else {}
        self._strict_check = strict_check if strict_check is not None else []
        self._routine_ids = dict(routine_ids or {})
        self._admin = _DataApiBackend(client, cluster_arn, secret_arn, engine, admin_database or "",
                                      None, call_timeout_s)  # fmt: skip
        role = self.database + "_runner" if engine == "postgresql" else None
        if engine == "mysql" and runner is None:
            raise ValueError("a MySQL target runs its routines as a restricted runner user")
        self._runner = runner
        # MySQL: the setup, the routines and the calls run as the run's restricted user,
        # through its own secret; only the scratch database's create, grant and drop run as
        # the master user.
        self._backend = _DataApiBackend(client, cluster_arn,
                                        runner.secret_arn if runner else secret_arn, engine,
                                        self.database, role, call_timeout_s)  # fmt: skip
        self._handle = de.EngineHandle(engine, version, Path("/nonexistent"), "rds-data", None,
                                       self._backend)  # fmt: skip
        self._created = False
        self.drop_failed = False
        self._session_user: str | None = None  # read in the first call's transaction
        self._role_sql = de._quote(self.database + "_runner", '"')
        self._strict_ready = False
        try:
            self._create()
        except BaseException:
            self.close()
            raise

    # ---- the scratch database
    def _create(self) -> None:
        db = de._quote(self.database, _MARK[self.engine])
        try:
            self._admin._guarded(f"CREATE DATABASE {db}")
            self._created = True
            if self.engine == "postgresql":
                role = self._role_sql
                self._admin._guarded(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB "
                                     "NOCREATEROLE NOREPLICATION NOBYPASSRLS")  # fmt: skip
                self._admin._guarded(f"GRANT {role} TO CURRENT_USER")
                self._backend._guarded(f"ALTER SCHEMA public OWNER TO {role}")
                self._backend._guarded("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            elif self._runner is not None:
                self._admin._guarded(
                    f"GRANT ALL PRIVILEGES ON {grant_database(self.database)}.* TO "
                    f"{self._runner.account_sql}"
                )
        except de.EngineError as exc:
            raise ProofTargetError(f"the scratch database was not created on {self.description}",
                                   exc.sqlstate) from None  # fmt: skip

    def close(self) -> None:
        """Roll back, drop the scratch database (and its role), whatever happened."""
        with contextlib.suppress(Exception):  # the drop below still runs
            self._backend.close()
        if not getattr(self, "_created", False):
            return
        db = de._quote(self.database, _MARK[self.engine])
        drop = f"DROP DATABASE IF EXISTS {db}"
        if self.engine == "postgresql":
            drop += " WITH (FORCE)"
        try:
            self._admin._guarded(drop)
            if self.engine == "postgresql":
                self._admin._guarded(f"DROP ROLE IF EXISTS {self._role_sql}")
            self._created = False
        except de.EngineError:
            self.drop_failed = True  # the proof's final drop_scratch sweep tries again

    # ---- the protocol, where the Data API differs
    def _create_schemas(self, names: Sequence[str]) -> None:
        """Each source schema of the setup (#1970), in the scratch database, owned by the
        run's role as ``public`` is, and closed to everyone else. Made by the master user in
        the scratch database (the role cannot create a schema there); the names are checked."""
        if self.engine != "postgresql":
            raise ProofTargetError(
                f"{self.description} cannot hold a source database other than the scratch "
                "database yet: the setup's tables name one"
            )
        made = list(self._backend._namespaces)
        try:
            for name in names:
                if name.casefold() == "public" or name in made:
                    continue
                schema = de._quote(name, '"')
                self._backend._guarded(f"CREATE SCHEMA {schema} AUTHORIZATION {self._role_sql}")
                self._backend._guarded(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
                made.append(name)
        except (de.EngineError, ValueError) as exc:
            raise ProofTargetError(f"the source schemas were not created on {self.description}",
                                   getattr(exc, "sqlstate", None)) from None  # fmt: skip
        self._backend._namespaces = tuple(made)
        self._schemas = tuple(made)

    def load_setup(self, setup: ProofSetup) -> None:
        if setup.schemas:
            self._create_schemas(setup.schemas)
        try:
            self._backend.load(setup.ddl, setup.rows_by_table(), [])
        except de.EngineError as exc:
            raise ProofTargetError(f"the synthetic rows did not load on {self.description}",
                                   exc.sqlstate) from exc  # fmt: skip

    def _strict_all(self) -> None:
        """``plpgsql_check`` on every PL/pgSQL routine of the scratch database (a trigger
        function with its trigger's table), when the cluster offers it: its errors by
        level and SQLSTATE, per routine id, never a message."""
        if not self._strict_check:
            self._strict_check.append(self._offer_strict())
        if self._strict_check[0] != _PLPGSQL_CHECK:
            return
        try:
            if not self._strict_ready:
                self._backend._guarded(f"CREATE EXTENSION IF NOT EXISTS {_PLPGSQL_CHECK}")
                self._strict_ready = True
        except de.EngineError:
            self._strict_check[0] = "not_available"  # offered, but it could not be created
            return
        try:
            routines = self._backend._rows(self._backend._guarded(_PG_PLPGSQL_ROUTINES, meta=True))[
                0
            ]
        except de.EngineError:
            return
        for oid, rid, relid in routines:
            key = self._routine_ids.get(str(rid), str(rid))
            try:
                got = self._backend._rows(self._backend._guarded(
                    "SELECT level::text, coalesce(sqlstate, '') FROM plpgsql_check_function_tb("
                    "CAST(%s AS oid)::regprocedure, CAST(%s AS oid)::regclass)",
                    [int(oid), int(relid or 0)], meta=True,
                ))[0]  # fmt: skip
            except de.EngineError as exc:
                self._strict[key] = [("error", exc.sqlstate or "?????")]
                continue
            self._strict[key] = [("error", str(state)) for level, state in got
                                 if str(level).lower() == "error"]  # fmt: skip

    def create(self, routine: de.SourceRoutine) -> str | None:
        found = super().create(routine)
        if (
            found is None
            and self.engine == "postgresql"
            and routine.kind in ("function", "procedure")
        ):
            self._strict_compile(routine)
        return found

    def _strict_compile(self, routine: de.SourceRoutine) -> None:
        """Where plpgsql_check is not offered: the routine's ``CREATE`` once more with
        ``plpgsql.extra_errors = 'all'`` (shadowed variables and PL/pgSQL's other extra
        checks as errors), then rolled back. Its SQLSTATE is recorded, never its message."""
        if not self._strict_check:
            self._strict_check.append(self._offer_strict())
        if self._strict_check[0] != EXTRA_ERRORS or not _PLPGSQL.search(routine.create_sql):
            return
        key = self._routine_ids.get(_text_key(routine.create_sql), routine.name)
        self._backend.begin()
        try:
            self._backend._guarded("SET LOCAL plpgsql.extra_errors = 'all'")
            self._backend._guarded(routine.create_sql)
            self._strict[key] = []
        except de.EngineError as exc:
            if exc.sqlstate in _EXISTS:
                self._strict[key] = [("unchecked", "")]  # the compile check could not run
            else:
                self._strict[key] = [("error", exc.sqlstate or "?????")]
        finally:
            self._backend.rollback()

    def _offer_strict(self) -> str:
        try:
            got = self._backend._rows(self._backend._guarded(
                "SELECT count(*) FROM pg_available_extensions WHERE name = %s", [_PLPGSQL_CHECK],
                meta=True,
            ))[0]  # fmt: skip
        except de.EngineError:
            return "not_available"
        return _PLPGSQL_CHECK if got and got[0][0] else EXTRA_ERRORS

    def prepare_calls(self) -> None:
        # Session settings are applied per call, in its transaction (begin_call).
        if self.engine == "postgresql":
            self._backend.begin()  # under the role, so every call may write it
        try:
            self._execute("CREATE TABLE modernizer_sentinel (id INTEGER PRIMARY KEY)")
            self._backend.commit()
        finally:
            self._backend.rollback()
        try:
            self._counters = self._backend.counters()
        except de.EngineError as exc:
            raise ProofTargetError(f"{self.description} could not read its counters",
                                   exc.sqlstate) from exc  # fmt: skip
        if self.engine == "postgresql":
            self._strict_all()
        if self.engine == "mysql":
            self._backend.begin()
            try:
                seeded = self._backend.execute("SET rand_seed1 = %s, rand_seed2 = %s", [1, 2])
                self.random_seeded = seeded.error is None
            finally:
                self._backend.rollback()
            if not self.random_seeded:
                self.notes = ("the server refused the random seed, so RAND() was not frozen",)

    def begin_call(self, frozen_at: datetime.datetime, seed: float) -> None:
        try:
            self._backend.restore_counters(self._counters)  # MySQL: ALTER TABLE commits
            self._backend.begin()
        except (de.EngineError, Exception) as exc:  # noqa: BLE001
            raise ProofTargetError(f"{self.description} could not reset its session",
                                   getattr(exc, "sqlstate", None)) from None  # fmt: skip
        self._open = True
        if self.engine == "postgresql":
            if self._session_user is None:
                current, session = self._who()
                if current != self._backend.role:
                    raise ProofTargetError(f"{self.description} did not take the runner role")
                self._session_user = session
            self._execute("SELECT setseed(%s)", [seed])
        else:
            self._execute("SET time_zone = '+05:47'")
            self._execute("SET SESSION innodb_lock_wait_timeout = 5")
            self._execute("SET TIMESTAMP = %s", [round(frozen_at.timestamp(), 6)])
            if self.random_seeded:
                whole = int(abs(seed) * 1_000_000) + 1
                self._execute("SET rand_seed1 = %s, rand_seed2 = %s", [whole, whole + 1])
        self._execute("INSERT INTO modernizer_sentinel (id) VALUES (1)")

    def run(self, call: ProofCall) -> de.CallResult:
        postgres = self.engine == "postgresql"
        if postgres:
            self._execute("SAVEPOINT modernizer_call")
        if postgres and call.action == "row_change":
            result = self._pg_row_change(call)
        else:
            result = run_call(self._handle, call)
        if result.timed_out:
            self._open = False
            return result
        if postgres and result.error is not None:
            self._execute("ROLLBACK TO SAVEPOINT modernizer_call")
        if postgres:
            self._check_role()
        return result

    def _who(self) -> tuple[str, str]:
        """``current_user`` and ``session_user``, in the call's transaction."""
        got = self._backend._rows(
            self._backend._guarded("SELECT current_user::text, session_user::text", meta=True),
            as_text=True,
        )[0]
        return (str(got[0][0]), str(got[0][1])) if got else ("", "")

    def _check_role(self) -> None:
        """After the call and before its rollback: a call that left the runner role, by any
        spelling the screen missed (dynamic SQL included), or changed ``session_user``, is
        refused. The screen is a first line only; this read is what holds."""
        try:
            current, session = self._who()
        except de.EngineError as exc:
            raise ProofTargetError(f"{self.description} could not read the call's role",
                                   exc.sqlstate) from None  # fmt: skip
        if current != self._backend.role or session != self._session_user:
            raise RoleChanged("the call changed the role it runs as")

    def _pg_row_change(self, call: ProofCall) -> de.CallResult:
        """A row change whose values each column reads by its own type
        (``json_populate_record`` of the table's row type), as an untyped literal is read:
        the Data API would send each as ``varchar``."""
        de.row_change_sql("postgresql", call.kind, call.name, call.columns, call.where)  # type: ignore[arg-type]
        target = de._quote(call.name, '"')
        values = dict(zip((*call.columns, *call.where), call.args, strict=True)) \
            if call.kind != "UPDATE" else None  # fmt: skip
        record = f"json_populate_record(NULL::{target}, CAST(%s AS json))"
        if call.kind == "INSERT":
            cols = ", ".join(de._quote(c, '"') for c in call.columns)
            sql = f"INSERT INTO {target} ({cols}) SELECT {cols} FROM {record}"  # nosec B608 - quoted names, bound row
            return self._backend._result(sql, [_row_json(values or {})])
        keys = ", ".join(de._quote(c, '"') for c in call.where)
        match = (
            f"({keys}) = (SELECT {keys} FROM {record} r)"  # nosec B608 - quoted names, bound row
        )
        n = len(call.columns)
        if call.kind == "UPDATE":
            sets = ", ".join(de._quote(c, '"') for c in call.columns)
            new = dict(zip(call.columns, call.args[:n], strict=True))
            old = dict(zip(call.where, call.args[n:], strict=True))
            sql = f"UPDATE {target} SET ({sets}) = (SELECT {sets} FROM {record} s) WHERE {match}"  # nosec B608 - quoted identifiers, the row a bound parameter
            if n == 1:
                sql = f"UPDATE {target} SET {sets} = (SELECT {sets} FROM {record} s) WHERE {match}"  # nosec B608 - quoted identifiers, the row a bound parameter
            return self._backend._result(sql, [_row_json(new), _row_json(old)])
        sql = f"DELETE FROM {target} WHERE {match}"  # nosec B608 - quoted names, bound row
        return self._backend._result(sql, [_row_json(values or {})])

    def end_call(self) -> None:
        if self._open:
            self._open = False
        self._backend.rollback()


def data_api_target(client: DataApiClient, *, cluster_arn: str, secret_arn: str, engine: str,
                    version: str, job_id: str, admin_database: str | None,
                    call_timeout_s: float = CALL_TIMEOUT_S, strict: StrictFindings | None = None,
                    strict_check: list[str] | None = None,
                    routine_ids: Mapping[str, str] | None = None,
                    runner: RunnerAccount | None = None) -> TargetFactory:  # fmt: skip
    """A factory for :class:`DataApiTarget`: each target is a new scratch database.
    ``strict`` collects the strict check's findings and ``strict_check`` what ran
    (``plpgsql_check`` or ``not_available``) across every target the factory opens."""

    def open_target() -> ProofTarget:
        return DataApiTarget(client, cluster_arn=cluster_arn, secret_arn=secret_arn,
                             engine=engine, version=version, job_id=job_id,
                             admin_database=admin_database, call_timeout_s=call_timeout_s,
                             strict=strict, strict_check=strict_check, routine_ids=routine_ids,
                             runner=runner)  # fmt: skip

    return open_target


def drop_scratch(client: DataApiClient, *, cluster_arn: str, secret_arn: str, engine: str,
                 job_id: str, admin_database: str | None) -> tuple[list[str], list[str]]:  # fmt: skip
    """Drop every scratch database of ``job_id`` the cluster still holds (what a run that
    was killed left) and, on PostgreSQL, every ``<scratch>_runner`` role, also one whose
    database is gone. Each drop is tried on its own: one that fails never stops the others.
    Returns ``(dropped, left)``, ``left`` naming what could not be dropped or listed."""
    prefix = scratch_prefix(job_id)
    if not _PREFIX.fullmatch(prefix):  # pragma: no cover - built above
        raise ValueError("not a scratch prefix")
    admin = _DataApiBackend(
        client, cluster_arn, secret_arn, engine, admin_database or "", None, CALL_TIMEOUT_S
    )
    mark = _MARK[engine]
    lists: tuple[tuple[str, str, list[Any]], ...]
    if engine == "postgresql":
        lists = (
            ("database", "SELECT datname::text FROM pg_database WHERE starts_with(datname::text, %s) ORDER BY 1", [prefix]),
            ("role", "SELECT rolname::text FROM pg_roles WHERE starts_with(rolname::text, %s) ORDER BY 1", [prefix]),
        )  # fmt: skip
    else:
        lists = (
            ("database", "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
             "WHERE LEFT(SCHEMA_NAME, %s) = %s ORDER BY 1", [len(prefix), prefix]),
        )  # fmt: skip
    dropped: list[str] = []
    left: list[str] = []
    for what, sql, params in lists:
        try:
            names = [str(r[0]) for r in admin._rows(admin._guarded(sql, params, meta=True))[0]]
        except Exception:  # noqa: BLE001 - reported, never raised
            left.append(f"the {what}s of prefix {prefix} (could not be listed)")
            continue
        for name in names:
            base = name[: -len("_runner")] if what == "role" and name.endswith("_runner") else name
            if not _SCRATCH.fullmatch(base):
                continue
            if what == "database":
                drop = f"DROP DATABASE IF EXISTS {de._quote(name, mark)}" + (
                    " WITH (FORCE)" if engine == "postgresql" else ""
                )
            else:
                drop = f"DROP ROLE IF EXISTS {de._quote(name, mark)}"
            try:
                admin._guarded(drop)
                dropped.append(name)
            except Exception:  # noqa: BLE001 - the next one is still dropped
                left.append(name)
    return dropped, left


# --------------------------------------------------------------------------- MySQL runner

_RUNNER_PREFIX = "mdzr_"
_RUNNER_NAME = re.compile(r"^mdzr_[0-9a-f]{10}_[0-9a-f]{8}$")
#: Where a runner's secret is kept, under the job's id.
RUNNER_SECRET_PREFIX = (
    "modernizer/routine-proof/"  # nosec B105 - a secret name prefix, not a password
)


@dataclass(frozen=True)
class RunnerAccount:
    """The run's restricted MySQL user: granted each scratch database it runs in and
    nothing else, reached through the Data API with its own Secrets Manager secret."""

    username: str
    secret_arn: str

    @property
    def account_sql(self) -> str:
        if not _RUNNER_NAME.fullmatch(self.username):
            raise ValueError("not a runner user name")
        return f"'{self.username}'@'%'"

    @property
    def definer(self) -> str:
        """The DEFINER the carried routines name, so each runs with this user's privileges."""
        return f"{self.username}@%"


def runner_prefix(job_id: str) -> str:
    return _RUNNER_PREFIX + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:10] + "_"


def runner_secret_name(job_id: str, username: str) -> str:
    return f"{RUNNER_SECRET_PREFIX}{job_id}/{username}"


def create_runner(client: DataApiClient, secrets_client: Any, *, cluster_arn: str,
                  secret_arn: str, job_id: str, admin_database: str | None,
                  tags: Sequence[Mapping[str, str]]) -> RunnerAccount:  # fmt: skip
    """Create the run's restricted MySQL user (no privilege yet: each scratch database
    grants its own) and its secret, tagged with the job id, then sign in as it through the
    Data API. The password is generated here and kept only in the secret: the Data API takes
    no bound parameter in ``CREATE USER ... IDENTIFIED BY ?``, so the user is created from its
    ``caching_sha2_password`` hash, computed here (:mod:`~src.tools.routines.mysql_auth`), and
    the plain password is never in a statement (#2298)."""
    from src.tools.routines.mysql_auth import AUTH_STRING, caching_sha2_auth_string

    username = runner_prefix(job_id) + secrets.token_hex(4)
    if not _RUNNER_NAME.fullmatch(username):  # pragma: no cover - built above
        raise ValueError("not a runner user name")
    password = secrets.token_urlsafe(24)
    auth = caching_sha2_auth_string(password)
    if not AUTH_STRING.fullmatch(auth):  # pragma: no cover - checked where it is built
        raise ValueError("not a caching_sha2_password authentication string")
    created = secrets_client.create_secret(
        Name=runner_secret_name(job_id, username),
        Description="The routine proof's restricted MySQL user on the throwaway test cluster",
        SecretString=json.dumps({"username": username, "password": password}),
        Tags=list(tags),
    )  # nosec B106 - a generated password, stored only in the secret
    account = RunnerAccount(username, str(created["ARN"]))
    admin = _DataApiBackend(client, cluster_arn, secret_arn, "mysql", admin_database or "", None,
                            CALL_TIMEOUT_S)  # fmt: skip
    # Signed in through the runner's own secret, with no database: it has no grant yet.
    own = _DataApiBackend(client, cluster_arn, account.secret_arn, "mysql", "", None,
                          CALL_TIMEOUT_S)  # fmt: skip
    try:
        # The hash, not the password, is in the text: crypt's alphabet only, checked above.
        admin._guarded(f"CREATE USER {account.account_sql} "
                       f"IDENTIFIED WITH caching_sha2_password AS '{auth}'")  # fmt: skip
        try:
            who = own._rows(own._guarded("SELECT CURRENT_USER()", meta=True), as_text=True)[0]
        except de.EngineError as exc:
            raise de.EngineError("the Data API could not sign in as the runner user",
                                 exc.sqlstate) from None  # fmt: skip
        if not who or str(who[0][0]) != f"{username}@%":
            raise de.EngineError("the Data API signed in as another user than the runner")
    except de.EngineError as exc:
        left = drop_runner(client, secrets_client, cluster_arn=cluster_arn, secret_arn=secret_arn,
                           admin_database=admin_database, account=account)  # fmt: skip
        raise RunnerNotCreated(str(exc), exc.sqlstate, left) from None
    except BaseException:
        drop_runner(client, secrets_client, cluster_arn=cluster_arn, secret_arn=secret_arn,
                    admin_database=admin_database, account=account)  # fmt: skip
        raise
    return account


class RunnerNotCreated(de.EngineError):
    """``CREATE USER`` for the runner failed; ``left`` names what the cleanup that followed
    could not remove (empty when the user and its secret are both gone)."""

    def __init__(self, message: str, sqlstate: str | None, left: Sequence[str]) -> None:
        super().__init__(message, sqlstate)
        self.left = list(left)


def sweep_runners(client: DataApiClient, secrets_client: Any, *, cluster_arn: str,
                  secret_arn: str, job_id: str,
                  admin_database: str | None) -> tuple[list[str], list[str]]:  # fmt: skip
    """Drop every runner user of ``job_id`` the cluster still holds and delete every runner
    secret under the job's name prefix: what a run killed between the runner's creation and
    its drop left. Nothing of another job is touched (each name is checked against the
    job's prefix and the runner pattern). Each removal is tried on its own; returns
    ``(removed, left)``."""
    prefix = runner_prefix(job_id)
    removed: list[str] = []
    left: list[str] = []
    admin = _DataApiBackend(client, cluster_arn, secret_arn, "mysql", admin_database or "", None,
                            CALL_TIMEOUT_S)  # fmt: skip
    try:
        users = [str(r[0]) for r in admin._rows(admin._guarded(
            "SELECT User FROM mysql.user WHERE LEFT(User, %s) = %s ORDER BY 1",
            [len(prefix), prefix], meta=True))[0]]  # fmt: skip
    except Exception:  # noqa: BLE001 - reported, never raised
        users, left = [], [f"the runner users of prefix {prefix} (could not be listed)"]
    for user in users:
        if not (user.startswith(prefix) and _RUNNER_NAME.fullmatch(user)):
            continue
        try:
            admin._guarded(f"DROP USER IF EXISTS '{user}'@'%'")
            removed.append(f"user {user}")
        except Exception:  # noqa: BLE001 - the next one is still dropped
            left.append(f"user {user}")
    name_prefix = f"{RUNNER_SECRET_PREFIX}{job_id}/"
    try:
        found, token = [], None
        while True:
            page = secrets_client.list_secrets(
                Filters=[{"Key": "name", "Values": [name_prefix]}],
                **({"NextToken": token} if token else {}),
            )
            found += list(page.get("SecretList") or [])
            token = page.get("NextToken")
            if not token:
                break
    except Exception:  # noqa: BLE001 - reported, never raised
        return removed, [*left, f"the runner secrets under {name_prefix} (could not be listed)"]
    for secret in found:
        name = str(secret.get("Name") or "")
        user = name[len(name_prefix) :]
        if not (name.startswith(name_prefix) and _RUNNER_NAME.fullmatch(user)):
            continue
        try:
            secrets_client.delete_secret(SecretId=str(secret["ARN"]),
                                         ForceDeleteWithoutRecovery=True)  # fmt: skip
            removed.append(f"secret {name}")
        except Exception:  # noqa: BLE001 - the next one is still deleted
            left.append(f"secret {name}")
    return removed, left


def delete_runner_secrets(secrets_client: Any, *, job_id: str) -> tuple[int, list[str]]:
    """Force-delete every runner secret of ``job_id`` (scheduled deletions included), each
    only once its name is this job's runner pattern and its tags name this job and the
    routine proof: what a run killed before its cleanup left. The teardown calls it, so no
    runner secret outlives the stack (#2351). Returns ``(deleted, left)``; never raises."""
    from src.tools.data_movement.proof_target import JOB_TAG, PURPOSE, PURPOSE_TAG

    name_prefix, user_prefix = f"{RUNNER_SECRET_PREFIX}{job_id}/", runner_prefix(job_id)
    try:
        found, token = [], None
        while True:
            page = secrets_client.list_secrets(
                Filters=[{"Key": "name", "Values": [name_prefix]}], IncludePlannedDeletion=True,
                **({"NextToken": token} if token else {}),
            )  # fmt: skip
            found += list(page.get("SecretList") or [])
            token = page.get("NextToken")
            if not token:
                break
    except Exception:  # noqa: BLE001 - reported, never raised
        return 0, [f"the runner secrets under {name_prefix} (could not be listed)"]
    deleted, left = 0, []
    for secret in found:
        name = str(secret.get("Name") or "")
        user = name[len(name_prefix) :]
        if not (name.startswith(name_prefix) and user.startswith(user_prefix)
                and _RUNNER_NAME.fullmatch(user)):  # fmt: skip
            continue
        tags = {str(t.get("Key")): t.get("Value") for t in secret.get("Tags") or []}
        if tags.get(JOB_TAG) != job_id or tags.get(PURPOSE_TAG) != PURPOSE:
            left.append(f"{name} (its tags are not this job's routine proof)")
            continue
        try:
            secrets_client.delete_secret(SecretId=str(secret["ARN"]),
                                         ForceDeleteWithoutRecovery=True)  # fmt: skip
            deleted += 1
        except Exception:  # noqa: BLE001 - the next one is still deleted
            left.append(f"{name} (could not be deleted)")
    return deleted, left


def drop_runner(client: DataApiClient, secrets_client: Any, *, cluster_arn: str, secret_arn: str,
                admin_database: str | None, account: RunnerAccount) -> list[str]:  # fmt: skip
    """Drop the runner user and delete its secret with no recovery window, each tried on
    its own; returns what could not be removed."""
    left: list[str] = []
    admin = _DataApiBackend(client, cluster_arn, secret_arn, "mysql", admin_database or "", None,
                            CALL_TIMEOUT_S)  # fmt: skip
    try:
        admin._guarded(f"DROP USER IF EXISTS {account.account_sql}")
    except Exception:  # noqa: BLE001 - the secret is still deleted
        left.append(f"user {account.username}")
    try:
        secrets_client.delete_secret(SecretId=account.secret_arn, ForceDeleteWithoutRecovery=True)
    except Exception:  # noqa: BLE001 - reported, never raised
        left.append(f"secret of user {account.username}")
    return left


def mysql_event_scheduler(client: DataApiClient, *, cluster_arn: str, secret_arn: str,
                          admin_database: str | None) -> str:  # fmt: skip
    """``@@GLOBAL.event_scheduler`` (``ON``, ``OFF``, ``DISABLED``), upper-cased."""
    admin = _DataApiBackend(client, cluster_arn, secret_arn, "mysql", admin_database or "", None,
                            CALL_TIMEOUT_S)  # fmt: skip
    rows = admin._rows(admin._guarded("SELECT @@GLOBAL.event_scheduler", meta=True), as_text=True)[
        0
    ]
    return str(rows[0][0]).upper() if rows else ""


#: What the cluster holds outside the scratch databases, read before and after a run.
_BASELINE = {
    "postgresql": (
        ("roles", "SELECT rolname::text FROM pg_roles ORDER BY 1"),
        ("databases", "SELECT datname::text FROM pg_database ORDER BY 1"),
    ),
    "mysql": (
        ("users", "SELECT CONCAT(User, '@', Host) FROM mysql.user ORDER BY 1"),
        ("databases", "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY 1"),
        ("events", "SELECT CONCAT(EVENT_SCHEMA, '.', EVENT_NAME) FROM information_schema.EVENTS ORDER BY 1"),
    ),
}  # fmt: skip


def cluster_baseline(client: DataApiClient, *, cluster_arn: str, secret_arn: str, engine: str,
                     admin_database: str | None) -> dict[str, list[str]]:  # fmt: skip
    """The users or roles, databases and (MySQL) events the cluster holds, by name."""
    admin = _DataApiBackend(client, cluster_arn, secret_arn, engine, admin_database or "", None,
                            CALL_TIMEOUT_S)  # fmt: skip
    return {
        what: [str(r[0]) for r in admin._rows(admin._guarded(sql, meta=True), as_text=True)[0]]
        for what, sql in _BASELINE[engine]
    }


def baseline_changes(before: Mapping[str, Sequence[str]],
                     after: Mapping[str, Sequence[str]]) -> dict[str, dict[str, list[str]]]:  # fmt: skip
    """What a run added or removed outside the cluster's baseline, by kind; empty when
    nothing changed."""
    out: dict[str, dict[str, list[str]]] = {}
    for what in sorted(set(before) | set(after)):
        b, a = set(before.get(what, ())), set(after.get(what, ()))
        if a - b or b - a:
            out[what] = {"added": sorted(a - b), "removed": sorted(b - a)}
    return out
