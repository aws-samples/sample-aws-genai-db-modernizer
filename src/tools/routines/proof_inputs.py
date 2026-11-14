# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test calls and synthetic rows for testing routines (#1942), no model.

To run a routine (and later, to compare the carried one with the original side by side),
it needs inputs and data, and no collection reads table rows (#1555). This module builds
both from shapes only:

- **Calls.** Each function or procedure gets a baseline call (a typical value per
  argument), then one call per edge value of each argument, the others kept at their
  baseline: NULL, then the type family's own edges (empty, zero, the type's lowest and
  highest values, the maximum length, a quote, bytes, ``money``, an enum by position
  ...; :func:`parameter_cases`), and one all-NULL call when it has several arguments.
  ``OUT`` arguments are passed as NULL to a procedure (MySQL through a session variable,
  see ``disposable_engine.call``) and not at all to a PostgreSQL function. On PostgreSQL
  each argument is cast to its parameter's declared type (``%s::integer``,
  ``%s::"char"``, ``%s::"public"."Mood"``), so a call reaches its own overload. A routine gets at most ``MAX_CALLS_PER_ROUTINE`` calls.
- **Row changes.** A trigger gets the row change that fires it, for each of its events
  (the call list starts with them, so each finds the rows as seeded): an ``INSERT`` of a
  held-back synthetic row, an ``UPDATE`` of the first row (a column of ``UPDATE OF``
  when it names one) and a ``DELETE`` of a row no foreign key points at, each trigger
  on a table with its own rows.
- **Event runs.** A MySQL event (#1971) gets one run of its body, recorded as a call
  (``kind`` ``event``, no arguments) so the comparison reads its table changes as data.
  The disposable engine runs with ``--event-scheduler=OFF`` and never turns it on: the
  engine runs the event's own statement once, on demand (``disposable_engine.call``), so
  a run never waits for a schedule or fires between other calls. A disabled event is run
  too: its body is carried logic, whatever its status. The run happens in the caller's
  session, not in the scheduler's own thread, so the comparison's frozen ``TIMESTAMP``,
  ``RAND`` seed and session variables apply to it, and the event's stored ``TIME_ZONE``
  does not.
- **Rows.** ``generate_related_rows`` (the ADR-033 generator,
  :mod:`src.tools.synthetic_data.generator`) seeds every table the routines touch, plus
  the tables their foreign keys reference: types, primary keys, unique indexes, foreign
  keys (parents first, a cycle broken at a nullable key), row counts capped at
  ``PROOF_ROWS``, and the value ranges and null rates a collection gives.
- **Setup.** The tables' ``CREATE TABLE``, index and foreign-key statements come from
  the Aurora DDL generator (same family, so the source engine reads them). No literal
  of the collection reaches the setup: a default that holds none (``now()``, a
  sequence) is kept and a literal one becomes a plain value of the column's type (0,
  FALSE, an empty string, a fixed date, time or uuid); a MySQL ``ENUM`` or ``SET``
  keeps its number of values, each a placeholder (``e1``, ``e2``, ...); only plain
  indexes are kept (no predicate or expression). A PostgreSQL enum column becomes
  ``TEXT`` (the collection has no enum labels, #1834). A table with a column whose type
  an extension provides (``EXTENSION_TYPES``) is left out, with why, unless the caller
  names that extension, and the rest still load. Rows and call arguments are bound
  values. No SQL text is stored with a call: ``run_call`` builds each statement when it
  runs, with every identifier and cast type checked and quoted
  (``disposable_engine.row_change_sql``, ``disposable_engine.pg_cast_type``).

The same routines, tables and seed always give byte-identical output
(:meth:`ProofInputs.to_json`). Nothing here reads or holds a routine's text: the calls
come from its metadata (:class:`RoutineProofInput`), and its ``CREATE`` reaches the
engine only through ``load_setup``'s ``SourceRoutine`` list.

Input shape. :class:`RoutineProofInput` is the metadata the routine collection (#1652,
#1920) will give with each routine: PostgreSQL ``pg_proc`` argument names, types and
modes (``proargnames``, ``proargtypes`` / ``proallargtypes``, ``proargmodes``) and
``pg_trigger``; MySQL ``information_schema.parameters`` (``PARAMETER_MODE``,
``PARAMETER_NAME``, ``DTD_IDENTIFIER``) and ``information_schema.triggers``
(``ACTION_TIMING``, ``EVENT_MANIPULATION``, ``EVENT_OBJECT_TABLE``) and
``information_schema.events`` (``EVENT_TYPE``, ``INTERVAL_VALUE``, ``INTERVAL_FIELD``,
``STATUS``). Tables are the
collection's own table objects (``database_schema.tables``); a column may also carry
``null_rate``, ``min_value`` and ``max_value``, which the collector contract does not
have yet.

Not generated (each one listed in ``ProofInputs.skipped`` with its reason): a trigger
function (it runs through its trigger), a routine with a ``VARIADIC`` argument, a
PostgreSQL routine with a parameter whose type no cast can name (an uncast call could
reach another overload, so none is sent), a trigger
on a view (``INSTEAD OF``), a ``TRUNCATE`` trigger, and a trigger on a table the
collection lacks, a MySQL event on PostgreSQL and one whose name is not a plain
identifier (#1971). Setup tables are created
without a schema, so a routine body that names another schema (#1970) does not find
them.

Building the inputs takes time linear in the routines and tables: every foreign key is
resolved through one ``TableIndex``, and the columns foreign keys point at are indexed
once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.contracts.schema_design_input import AgentTable
from src.tools.database import disposable_engine as de
from src.tools.schema.aurora_common.constraint_translator import (
    MYSQL_TARGET,
    PG_TARGET,
    translate_default,
)
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    generate_mysql_ddl,
    generate_pg_ddl,
)
from src.tools.synthetic_data.generator import (
    MAX_EDGE_TEXT,
    RelatedRows,
    TableIndex,
    column_kind,
    decimal_precision,
    enum_values,
    generate_related_rows,
    integer_bounds,
    is_bytes,
)

__all__ = [
    "ENUM_FIRST",
    "ENUM_LAST",
    "EXTENSION_TYPES",
    "MAX_CALLS_PER_ROUTINE",
    "PROOF_ROWS",
    "ProofCall",
    "ProofInputs",
    "ProofSetup",
    "RoutineParameter",
    "RoutineProofInput",
    "SkippedRoutine",
    "TriggerSpec",
    "build_proof_inputs",
    "iter_routine_calls",
    "load_setup",
    "parameter_cases",
    "routine_calls",
    "run_call",
]

Dialect = Literal["postgresql", "mysql"]
#: Rows per table at most (plus its edge rows and a trigger's held-back rows).
PROOF_ROWS = 10
#: Calls per routine at most; a routine with more edge values is cut, in order.
MAX_CALLS_PER_ROUTINE = 64
_HIDDEN = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")  # the engine's own plain identifier
_EVENTS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
_NO_CALL_RETURNS = {"trigger", "event_trigger"}


# --------------------------------------------------------------------------- input


class RoutineParameter(BaseModel):
    """One parameter: its name (None when unnamed), type as declared (``numeric(10,2)``,
    ``int unsigned``, ``character varying``; on PostgreSQL ``format_type``'s text, which
    is also the cast each call writes), mode, and the length, precision and scale when
    the catalog reports them apart from the type. ``is_enum``: a PostgreSQL enum type
    (``pg_type.typtype = 'e'``). ``TABLE`` (a PostgreSQL ``RETURNS TABLE`` column) is
    read as ``OUT``."""

    model_config = _HIDDEN

    name: str | None = None
    data_type: str = Field(min_length=1)
    mode: Literal["IN", "OUT", "INOUT", "VARIADIC", "TABLE"] = "IN"
    max_length: int | None = Field(None, ge=0)
    numeric_precision: int | None = Field(None, ge=0)
    numeric_scale: int | None = None
    is_enum: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _upper_mode(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class TriggerSpec(BaseModel):
    """What fires a trigger: its table (an id or a name), timing, events, the columns
    of ``UPDATE OF`` and its level."""

    model_config = _HIDDEN

    table: str = Field(min_length=1)
    timing: Literal["BEFORE", "AFTER", "INSTEAD OF"]
    events: tuple[Literal["INSERT", "UPDATE", "DELETE", "TRUNCATE"], ...] = Field(min_length=1)
    columns: tuple[str, ...] = ()
    level: Literal["ROW", "STATEMENT"] = "ROW"

    @field_validator("timing", "level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return " ".join(value.upper().split()) if isinstance(value, str) else value

    @field_validator("events", mode="before")
    @classmethod
    def _upper_events(cls, value: object) -> object:
        if isinstance(value, str):
            value = re.split(r"[\s,]+|\bOR\b", value.upper())
        if isinstance(value, (list, tuple)):
            words = {str(v).strip().upper() for v in value if str(v).strip()}
            return tuple(e for e in _EVENTS if e in words) + tuple(sorted(words - set(_EVENTS)))
        return value


_INTERVALS = ("YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND",
              "YEAR_MONTH", "DAY_HOUR", "DAY_MINUTE", "DAY_SECOND", "HOUR_MINUTE",
              "HOUR_SECOND", "MINUTE_SECOND", "DAY_MICROSECOND", "HOUR_MICROSECOND",
              "MINUTE_MICROSECOND", "SECOND_MICROSECOND", "MICROSECOND")  # fmt: skip


class EventSpec(BaseModel):
    """A MySQL event's metadata, as ``information_schema.events`` gives it (#1971):
    ``EVENT_TYPE`` (``schedule``: ``ONE TIME`` or ``RECURRING``), ``INTERVAL_VALUE`` and
    ``INTERVAL_FIELD`` of a recurring event, and ``STATUS`` (``ENABLED``, ``DISABLED`` or
    ``REPLICA_SIDE_DISABLED``, which older servers spell ``SLAVESIDE_DISABLED``: read as
    the first). The schedule's dates are not taken: a one-time event whose time has passed
    is dropped by the server as it is created, and the engine says so on that event's run. The tables it touches go in the routine's ``tables``.
    """

    model_config = _HIDDEN

    schedule: Literal["ONE TIME", "RECURRING"]
    interval_value: int | None = Field(None, ge=1)
    interval_field: Literal[_INTERVALS] | None = None  # type: ignore[valid-type]
    status: Literal["ENABLED", "DISABLED", "REPLICA_SIDE_DISABLED"] = "ENABLED"

    @field_validator("schedule", "interval_field", "status", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        text = " ".join(value.upper().split()) if isinstance(value, str) else value
        return "REPLICA_SIDE_DISABLED" if text == "SLAVESIDE_DISABLED" else text

    @model_validator(mode="after")
    def _interval_iff_recurring(self) -> EventSpec:
        has = self.interval_value is not None and self.interval_field is not None
        if (self.schedule == "RECURRING") != has or (
            (self.interval_value is None) != (self.interval_field is None)
        ):
            raise ValueError("a recurring event has an interval, and a one-time event has none")
        return self

    @property
    def enabled(self) -> bool:
        return self.status == "ENABLED"


class RoutineProofInput(BaseModel):
    """One routine's metadata, as the routine collection gives it (see the module
    docstring): no ``CREATE`` text.

    - ``routine_id``: the verdict's id (``public.add_tax(numeric)``, ``shop.restock``).
    - ``kind``: ``function``, ``procedure``, ``trigger`` or ``event`` (MySQL).
    - ``name``: what a call names (``public.add_tax``); a trigger's own name.
    - ``parameters``: in declaration order. ``returns``: a function's return type.
    - ``trigger``: required for a trigger, refused otherwise. ``event``: the same for
      an event (:class:`EventSpec`).
    - ``tables``: the tables the routine touches (ids or names, for instance the
      verdict's table dependencies).
    """

    model_config = _HIDDEN

    routine_id: str = Field(min_length=1)
    kind: Literal["function", "procedure", "trigger", "event"]
    name: str = Field(min_length=1)
    parameters: tuple[RoutineParameter, ...] = ()
    returns: str | None = None
    trigger: TriggerSpec | None = None
    event: EventSpec | None = None
    tables: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _trigger_iff_kind(self) -> RoutineProofInput:
        if (self.kind == "trigger") != (self.trigger is not None):
            raise ValueError("a trigger needs its trigger spec, and only a trigger has one")
        if (self.kind == "event") != (self.event is not None):
            raise ValueError("an event needs its event spec, and only an event has one")
        return self


# --------------------------------------------------------------------------- output


@dataclass(frozen=True)
class ProofCall:
    """One generated call or row change. No SQL text is stored: ``run_call`` builds the
    statement from these fields each time, checking every name, so a saved and reloaded
    call list is never trusted as SQL.

    - ``action`` ``call``: ``name`` is the routine (or the MySQL event, run once, with no
      arguments), ``kind`` its kind, ``args`` the
      arguments, ``modes`` their modes and ``casts`` their PostgreSQL types (None where
      no cast is written; MySQL writes none).
    - ``action`` ``row_change``: ``name`` is the table, ``kind`` the event, ``columns``
      the columns it inserts or sets, ``where`` the columns that match its row, and
      ``args`` the values, in that order.
    """

    routine_id: str
    label: str
    action: Literal["call", "row_change"]
    name: str
    kind: str
    args: tuple[Any, ...] = ()
    modes: tuple[str, ...] = ()
    casts: tuple[str | None, ...] = ()
    columns: tuple[str, ...] = ()
    where: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkippedRoutine:
    routine_id: str
    reason: str


@dataclass(frozen=True)
class ProofSetup:
    """The statements and rows a disposable engine loads before the calls.

    ``ddl``: one statement each (tables, then indexes, then foreign keys). ``rows``:
    ``(table, rows)`` in insert order. ``tables``: the table ids in the setup, in
    insert order. ``unseeded``: tables created with no rows, with why. ``missing``:
    ``(routine id, table)`` for each table a routine names that the collection lacks.
    """

    dialect: str
    ddl: tuple[str, ...]
    rows: tuple[tuple[str, tuple[dict[str, Any], ...]], ...]
    tables: tuple[str, ...] = ()
    cycle_breaks: tuple[tuple[str, str], ...] = ()
    unseeded: tuple[tuple[str, str], ...] = ()
    missing: tuple[tuple[str, str], ...] = ()

    def rows_by_table(self) -> dict[str, list[dict[str, Any]]]:
        """The rows as ``disposable_engine.load`` takes them (insert order kept)."""
        return {table: [dict(r) for r in rows] for table, rows in self.rows}


@dataclass(frozen=True)
class ProofInputs:
    setup: ProofSetup
    calls: tuple[ProofCall, ...]
    skipped: tuple[SkippedRoutine, ...] = ()
    truncated: tuple[str, ...] = ()
    seed: int = 0

    def to_json(self) -> str:
        """Canonical JSON: the same inputs and seed give the same bytes."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=_json)


def _json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON: {type(value).__name__}")


# --------------------------------------------------------------------------- calls


def _as_column(param: RoutineParameter) -> dict[str, Any]:
    return {
        "data_type": param.data_type,
        "max_length": param.max_length,
        "numeric_precision": param.numeric_precision,
        "numeric_scale": param.numeric_scale,
    }


_BYTES = {"bytea", "blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary"}
#: An enum argument's label, looked up on the engine when the call runs (PostgreSQL
#: ``enum_first`` / ``enum_last`` of the parameter's type): no label comes from text.
ENUM_FIRST: dict[str, str] = {"enum_label": "first"}
ENUM_LAST: dict[str, str] = {"enum_label": "last"}


def _family(param: RoutineParameter) -> str:
    t = param.data_type.strip().lower()
    if param.is_enum:
        return "pg_enum"
    if t.endswith("[]") or t == "array" or t.startswith("_"):
        return "array"
    word = t.split("(")[0].split()[0] if t else ""
    if word in ("enum", "set"):
        return word
    if word in _BYTES:
        return "bytes"
    if word == "money":
        return "money"
    if t == '"char"':
        return "pg_char"
    kind = column_kind(_as_column(param))
    return "int" if kind == "bool1" else kind


def _first_word(param: RoutineParameter) -> str:
    t = param.data_type.strip().lower()
    return t.split("(")[0].split()[0] if t else ""


def _decimal_text(digits_left: int, scale: int) -> str:
    whole = "9" * digits_left if digits_left > 0 else "0"
    return whole + ("." + "9" * scale if scale > 0 else "")


def parameter_cases(param: RoutineParameter, dialect: Dialect) -> list[tuple[str, Any]]:
    """The edge values of one parameter's type family, each with its label, NULL first.

    Values are those the engine takes for the type with its default settings: MySQL's
    own date and ``TIMESTAMP`` ranges, PostgreSQL's ``infinity`` and ``NaN``, the
    maximum length of a string, the lowest and highest value of an integer or decimal
    type, bytes, ``money``, PostgreSQL ``"char"``, and an enum or set by its position (a
    PostgreSQL enum by its first and last label, looked up on the engine). A type the
    generator does not model gets NULL only.
    """
    pg = dialect == "postgresql"
    family = _family(param)
    word = _first_word(param)
    cases: list[tuple[str, Any]] = [("null", None)]
    if family in ("int", "uint"):
        low, high = integer_bounds(_as_column(param))
        cases += [("zero", 0), ("max", high), ("min", low)]
        if low < 0:
            cases.append(("minus_one", -1))
    elif family == "decimal":
        p, s = decimal_precision(_as_column(param))
        top = _decimal_text(p - s, s)
        step = ("0." + "0" * (s - 1) + "1") if s > 0 else "1"
        cases += [("zero", "0"), ("max", top), ("min", "-" + top), ("smallest_step", step)]
        if pg:
            cases.append(("nan", "NaN"))
    elif family == "float":
        four_byte = word in ("real", "float4") or (not pg and word == "float")
        cases += [("zero", 0.0), ("negative", -1.5)]
        cases.append(("max", 3.4e38 if four_byte else 1.7976931348623157e308))
        if pg:
            cases += [("nan", "NaN"), ("infinity", "Infinity"), ("-infinity", "-Infinity")]
    elif family == "string":
        n = param.max_length
        if n is None:
            m = re.search(r"\(\s*(\d+)\s*\)", param.data_type)
            n = int(m.group(1)) if m else None
        values = [("empty", ""), ("one_char", "a"), ("quote", "it's")]
        if n is None or n >= 4:
            values.append(("non_ascii", "été"))
        if n:
            values.append(("max_length", "z" * min(n, MAX_EDGE_TEXT)))
        cases += [(label, v) for label, v in values if n is None or len(v) <= n]
    elif family == "date":
        if pg:
            cases += [("min", "0001-01-01"), ("max", "9999-12-31")]
            cases += [("infinity", "infinity"), ("-infinity", "-infinity")]
        else:
            cases += [("min", "1000-01-01"), ("max", "9999-12-31")]
    elif family == "datetime":
        if pg:
            cases += [("min", "0001-01-01 00:00:00"), ("max", "9999-12-31 23:59:59")]
            cases += [("infinity", "infinity"), ("-infinity", "-infinity")]
        elif word == "timestamp":  # a day inside the range in every session time zone
            cases += [("min", "1970-01-02 00:00:00"), ("max", "2038-01-18 00:00:00")]
        else:
            cases += [("min", "1000-01-01 00:00:00"), ("max", "9999-12-31 23:59:59")]
    elif family == "time":
        cases += [("min", "00:00:00"), ("max", "23:59:59")]
        cases += (
            [("end_of_day", "24:00:00")]
            if pg
            else [("lowest", "-838:59:59"), ("highest", "838:59:59")]
        )
    elif family == "bool":
        cases += [("true", True), ("false", False)] if pg else [("zero", 0), ("one", 1)]
    elif family == "uuid":
        cases += [("nil", "00000000-0000-0000-0000-000000000000")]
        cases += [("max", "ffffffff-ffff-ffff-ffff-ffffffffffff")]
    elif family == "json":
        cases += [("empty_object", "{}"), ("empty_array", "[]"), ("json_null", "null")]
    elif family == "year":
        cases += [("min", 1901), ("max", 2155)]
    elif family == "bit":
        cases += [("zero", "0"), ("one", "1")] if pg else [("zero", 0), ("one", 1)]
    elif family == "array":
        cases += [("empty", "{}"), ("null_element", "{NULL}")]
    elif family == "bytes":
        if pg:
            cases += [("empty", "\\x"), ("bytes", "\\x00ff")]
        else:  # a BINARY(n) pads; a value never exceeds the declared length
            n = param.max_length
            cases += [
                (lb, v) for lb, v in [("empty", ""), ("bytes", "ab")] if n is None or len(v) <= n
            ]
    elif family == "money":
        cases += [("zero", "0"), ("max", "92233720368547758.07"), ("min", "-92233720368547758.08")]
    elif family == "pg_char":
        cases += [("empty", ""), ("one_char", "a")]
    elif family == "enum":  # MySQL takes an ENUM by its position: no label is needed
        cases += [("first", 1)]
    elif family == "set":  # and a SET by its bit mask
        cases += [("empty", ""), ("first", 1)]
    elif family == "pg_enum":
        cases += [("first", ENUM_FIRST), ("last", ENUM_LAST)]
    return cases


def _typical(param: RoutineParameter, dialect: Dialect) -> Any:
    """A plain value of the type: 1 for a number (the first synthetic key), ``a`` ..."""
    pg = dialect == "postgresql"
    family = _family(param)
    return {
        "int": 1,
        "uint": 1,
        "decimal": "1",
        "float": 1.0,
        "string": "a",
        "date": "2024-01-01",
        "datetime": "2024-01-01 12:00:00",
        "time": "12:00:00",
        "bool": True if pg else 1,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "json": '{"k": 1}',
        "year": 2024,
        "bit": "1" if pg else 1,
        "array": "{}",
        "bytes": "\\x01" if pg else "a",
        "money": "1",
        "pg_char": "a",
        "enum": 1,
        "set": 1,
        "pg_enum": ENUM_FIRST,
    }.get(family)


def _bound(value: Any, dialect: Dialect) -> Any:
    """PostgreSQL arguments go as text, each cast to its parameter's declared type
    (``%s::smallint``), so the call reaches that overload and no other."""
    if dialect != "postgresql" or value is None or isinstance(value, (str, dict)):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, float) else str(value)


# information_schema's names for a family, not a type a cast can name
_NOT_A_CAST = {"array", "user-defined"}


def _cast(param: RoutineParameter) -> str | None:
    """The type a PostgreSQL call casts the argument to (``disposable_engine.pg_cast_type``:
    a built-in type, or a plain or quoted user type such as ``"char"`` or
    ``public."Mood"``), or None when the declared type is not one a cast may write."""
    if param.data_type.strip().lower() in _NOT_A_CAST:
        return None
    try:
        return de.pg_cast_type(param.data_type)
    except ValueError:
        return None


def _skip_reason(routine: RoutineProofInput, dialect: Dialect) -> str | None:
    if routine.kind == "event" and dialect != "mysql":
        return "a MySQL event is generated for MySQL only"
    if routine.kind == "event" and not all(_IDENT.match(p) for p in routine.name.split(".")):
        return "the event's name is not a plain identifier, so it is not run"
    if routine.kind == "function" and (routine.returns or "").strip().lower() in _NO_CALL_RETURNS:
        return "a trigger function runs through its trigger, not by a call"
    if any(p.mode == "VARIADIC" for p in routine.parameters):
        return "a VARIADIC parameter is not generated"
    if dialect == "postgresql":
        # an uncast call could reach another overload, so none is sent
        for i, p in enumerate(_passed(routine, dialect)):
            if _cast(p) is None:
                label = p.name or f"${i + 1}"
                return f"parameter {label} has a type no call can cast to, so no call is generated"
    return None


def _passed(routine: RoutineProofInput, dialect: Dialect) -> list[RoutineParameter]:
    """The parameters a call passes: a PostgreSQL function's OUT ones are not."""
    return [
        p
        for p in routine.parameters
        if not (
            dialect == "postgresql" and routine.kind == "function" and p.mode in ("OUT", "TABLE")
        )
    ]


def routine_calls(
    routine: RoutineProofInput, dialect: Dialect
) -> tuple[tuple[ProofCall, ...], bool]:
    """The calls of one function or procedure, and whether they were cut at
    ``MAX_CALLS_PER_ROUTINE``. A trigger's row changes need the setup's rows: see
    ``build_proof_inputs``. Raises ``ValueError`` for a routine ``_skip_reason`` refuses."""
    if routine.kind == "trigger":
        raise ValueError("a trigger's row changes come from build_proof_inputs")
    if (why := _skip_reason(routine, dialect)) is not None:
        raise ValueError(why)
    if routine.kind == "event":  # one run of its body: the engine runs it, scheduler off
        return (ProofCall(routine.routine_id, "run", "call", routine.name, "event"),), False
    passed = _passed(routine, dialect)
    modes = tuple("OUT" if p.mode in ("OUT", "TABLE") else p.mode for p in passed)
    casts = tuple(_cast(p) for p in passed) if dialect == "postgresql" else ()
    varying = [i for i, m in enumerate(modes) if m != "OUT"]
    base = [_typical(p, dialect) if modes[i] != "OUT" else None for i, p in enumerate(passed)]
    found: list[tuple[str, list[Any]]] = [("baseline", base)]
    for i in varying:
        label = passed[i].name or f"${i + 1}"
        for case, value in parameter_cases(passed[i], dialect):
            args = list(base)
            args[i] = value
            found.append((f"{label}={case}", args))
    if len(varying) > 1:
        found.append(("all_null", [None] * len(passed)))
    seen: set[str] = set()
    calls: list[ProofCall] = []
    for label, args in found:
        bound = tuple(_bound(a, dialect) for a in args)
        key = repr(bound)
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            ProofCall(
                routine.routine_id, label, "call", routine.name, routine.kind, bound, modes, casts
            )
        )
    return tuple(calls[:MAX_CALLS_PER_ROUTINE]), len(calls) > MAX_CALLS_PER_ROUTINE


def iter_routine_calls(
    routines: Iterable[RoutineProofInput], dialect: Dialect
) -> Iterator[ProofCall | SkippedRoutine]:
    """Every function's and procedure's calls, one routine at a time (constant memory
    per routine, so a library of any size streams); triggers are left to
    ``build_proof_inputs``."""
    for routine in routines:
        if routine.kind == "trigger":
            continue
        if (why := _skip_reason(routine, dialect)) is not None:
            yield SkippedRoutine(routine.routine_id, why)
            continue
        yield from routine_calls(routine, dialect)[0]


# --------------------------------------------------------------------------- setup


def _name(table: Mapping[str, Any]) -> str:
    return str(table.get("table_name") or str(table["table_id"]).rpartition(".")[2])


def _plain(table: Mapping[str, Any]) -> bool:
    names = [_name(table)] + [str(c.get("column_name")) for c in table.get("columns") or []]
    return all(_IDENT.match(n) for n in names)


def _closure(start: Iterable[str], index: TableIndex) -> list[str]:
    """``start`` and every table their foreign keys reference, transitively, sorted."""
    seen: set[str] = set()
    todo = sorted(set(start))
    while todo:
        tid = todo.pop()
        if tid in seen:
            continue
        seen.add(tid)
        for fk in index.by_id[tid].get("foreign_keys") or []:
            parent = index.resolve(str(fk.get("referenced_table") or ""), tid)
            if parent is not None and parent not in seen:
                todo.append(parent)
    return sorted(seen)


# A MySQL ENUM or SET keeps its kind and its number of values, never its values: each
# becomes a placeholder (e1, e2, ...), so no literal of the collection reaches the setup.
_MAX_ENUM_VALUES = 64


def _placeholder_type(column: Mapping[str, Any]) -> str | None:
    t = str(column.get("data_type") or "").strip()
    count = len(enum_values(column))
    if not count:
        return None
    word = "SET" if t.lower().startswith("set") else "ENUM"
    values = ",".join(f"'e{i + 1}'" for i in range(min(count, _MAX_ENUM_VALUES)))
    return f"{word}({values})"


def _shaped(table: Mapping[str, Any], dialect: Dialect) -> Mapping[str, Any]:
    """The table as the setup sees it: a MySQL ENUM or SET column's values replaced by
    placeholders, and every default without a literal of the collection."""
    columns = []
    for c in table.get("columns") or []:
        column = dict(c)
        if dialect == "mysql" and (placeholder := _placeholder_type(c)) is not None:
            column["data_type"] = placeholder
        column["default_value"] = _shape_default(column, dialect)
        columns.append(column)
    return {**table, "columns": columns}


# Defaults that hold no literal: the DDL generator's own grammar names them.
_KEPT_DEFAULTS = {"null", "timestamp", "date", "time", "uuid", "sequence"}
# A literal default becomes a plain value of the column's kind: the same value in both
# dialects, quoted as each one's collector reports a default (PostgreSQL's
# ``pg_get_expr`` quotes a string, MySQL's ``COLUMN_DEFAULT`` does not).
_SHAPE_TEXT = {"string": "", "date": "2024-01-01", "datetime": "2024-01-01 00:00:00",
               "time": "00:00:00", "uuid": "00000000-0000-0000-0000-000000000000"}  # fmt: skip
_SHAPE_NUMBER: dict[str, object] = {"int": 0, "uint": 0, "bool1": 0, "decimal": 0, "float": 0,
                                    "year": 2024}  # fmt: skip


def _shape_default(column: Mapping[str, Any], dialect: Dialect) -> object:
    """The column's default for the setup, with no literal of the collection: one that
    holds none (``now()``, a sequence, ``gen_random_uuid()``) is kept; a literal one is
    replaced by a plain value of the column's kind (0, FALSE, an empty string, a fixed
    date, time or uuid, a placeholder ENUM value), so a routine that leaves the column
    out still inserts; any other is left out."""
    value = column.get("default_value")
    if value is None:
        return None
    kind = translate_default(
        value,
        source_engine=dialect,
        target=PG_TARGET if dialect == "postgresql" else MYSQL_TARGET,
        aurora_type=str(column.get("data_type") or ""),
        nullable=bool(column.get("nullable")),
        identity_ok=True,
    ).kind
    if kind in _KEPT_DEFAULTS:
        return value
    if kind not in ("string", "number", "boolean", "bits"):
        return None
    value_kind = column_kind(column)
    if dialect == "mysql" and (values := enum_values(column)):
        return values[0] if value_kind == "enum" else None  # a placeholder, never text
    if value_kind == "bool":
        return False
    if is_bytes(column):
        return "'\\x'" if dialect == "postgresql" else ""  # no bytes; MySQL pads BINARY(n)
    if value_kind == "year" and dialect == "mysql":
        return "2024"  # MySQL's YEAR takes a quoted year as its default, not a number
    if value_kind in _SHAPE_NUMBER:
        return _SHAPE_NUMBER[value_kind]
    if value_kind in _SHAPE_TEXT and not (dialect == "mysql" and value_kind == "uuid"):
        text = _SHAPE_TEXT[value_kind]
        return f"'{text}'" if dialect == "postgresql" else text
    return None


def _ddl_table(table: Mapping[str, Any], selected: TableIndex, dialect: Dialect) -> AgentTable:
    """The (shaped) table as the DDL generator reads it: plain indexes only, and only
    the foreign keys whose table is in the setup (named by its table name)."""
    fks = []
    for fk in table.get("foreign_keys") or []:
        parent = selected.resolve(str(fk.get("referenced_table") or ""), str(table["table_id"]))
        if parent is not None:
            fks.append({**fk, "referenced_table": _name(selected.by_id[parent])})
    return AgentTable.model_validate(
        {
            "table_id": table["table_id"],
            "table_name": _name(table),
            "schema_name": table.get("schema_name"),
            "row_count": table.get("row_count"),
            "columns": list(table.get("columns") or []),
            "indexes": [
                {k: v for k, v in i.items() if k not in ("index_def", "index_expressions")}
                for i in table.get("indexes") or []
                if not i.get("predicate") and not i.get("has_expressions")
            ]
            or None,
            "primary_key": table.get("primary_key"),
            "foreign_keys": fks or None,
        }
    )


#: PostgreSQL column types that an extension provides, by the extension that does. A
#: table with one is left out of the setup unless its extension is named as present.
EXTENSION_TYPES = {"citext": "citext", "hstore": "hstore", "geometry": "postgis",
                   "geography": "postgis", "vector": "vector", "halfvec": "vector",
                   "sparsevec": "vector"}  # fmt: skip


def _generate(dialect: Dialect, tables: Sequence[AgentTable]) -> DdlResult:
    generate = generate_pg_ddl if dialect == "postgresql" else generate_mysql_ddl
    return generate(list(tables), source_engine=dialect)


def _needs_extension(result: DdlResult, extensions: Collection[str]) -> dict[str, str]:
    """Table name to why it is left out: a column whose type an absent extension gives."""
    out: dict[str, str] = {}
    for t in result.tables:
        for c in t.columns:
            base = c.aurora_type.split("(")[0].split("[")[0].strip().lower()
            ext = EXTENSION_TYPES.get(base)
            if ext is not None and ext not in extensions:
                out[t.table_name] = f"column {c.name} needs the {ext} extension ({base})"
                break
    return out


def _setup_ddl(result: DdlResult) -> tuple[str, ...]:
    statements = [t.create_sql for t in result.tables]
    for t in result.tables:
        statements.extend(t.index_sql)
    for t in result.tables:
        statements.extend(t.fk_sql)
    return tuple(statements)


# --------------------------------------------------------------------------- row changes


def _keys(table: Mapping[str, Any]) -> list[list[str]]:
    keys = [list(table["primary_key"])] if table.get("primary_key") else []
    for idx in table.get("indexes") or []:
        if idx.get("is_unique") and not idx.get("predicate") and not idx.get("has_expressions"):
            keys.append(list(idx.get("columns") or []))
    return [k for k in keys if k]


def _match_columns(table: Mapping[str, Any], row: Mapping[str, Any]) -> list[str]:
    """The columns that pick ``row`` out: a key with no NULL, else every non-NULL
    column whose values compare exactly (no float, JSON or unmodelled type)."""
    for key in _keys(table):
        if all(row.get(c) is not None for c in key):
            return key
    return [
        c["column_name"]
        for c in table.get("columns") or []
        if row.get(c["column_name"]) is not None
        and column_kind(c) not in ("float", "json", "other", "bytes", "enum")
    ]


def _referenced_columns(index: TableIndex) -> dict[str, set[str]]:
    """Each table's columns that some foreign key points at, built once for all tables."""
    out: dict[str, set[str]] = {}
    for tid, child in index.by_id.items():
        for fk in child.get("foreign_keys") or []:
            parent = index.resolve(str(fk.get("referenced_table") or ""), tid)
            if parent is not None:
                out.setdefault(parent, set()).update(fk.get("referenced_columns") or [])
    return out


def _update_column(
    table: Mapping[str, Any], spec: TriggerSpec, fixed: set[str]
) -> tuple[str, bool] | None:
    """The column an ``UPDATE`` sets, and whether it may take a new value (False: it is
    set to its own value, as a key must keep its value)."""
    names = [c["column_name"] for c in table.get("columns") or []]
    for c in spec.columns:
        match = next((n for n in names if n.lower() == c.lower()), None)
        if match is not None:
            return match, match not in fixed
    for c in table.get("columns") or []:
        if c["column_name"] not in fixed and column_kind(c) not in ("other", "bytes", "enum"):
            return c["column_name"], True
    return (names[0], False) if names else None


def _param(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def _trigger_changes(
    routine: RoutineProofInput,
    tid: str,
    nth: int,
    tables: Mapping[str, Mapping[str, Any]],
    referenced: Mapping[str, set[str]],
    related: RelatedRows,
    dialect: Dialect,
) -> tuple[list[ProofCall], str | None]:
    spec = routine.trigger
    assert spec is not None  # nosec B101 - the input model requires it for a trigger
    table = tables[tid]
    name = _name(table)
    rows = related.tables[tid].rows
    held = related.held_back.get(tid) or []
    if not rows:
        why = related.unseeded.get(tid, "it has no rows")
        return [], f"its table {tid} has no synthetic rows: {why}"
    fixed = {c for k in _keys(table) for c in k} | referenced.get(tid, set())
    for fk in table.get("foreign_keys") or []:
        fixed.update(fk.get("columns") or [])
    out: list[ProofCall] = []
    for event in spec.events:
        if event == "TRUNCATE":
            continue
        if event == "INSERT":
            if not held:
                continue
            if nth >= len(held):
                continue  # no held-back row of its own: another trigger's would clash
            row = held[nth]
            cols, where = list(row), []
            de.row_change_sql(dialect, "INSERT", name, cols)  # checks every name now
            values = tuple(_param(row[c]) for c in cols)
        elif event == "UPDATE":
            target = rows[0]
            where = _match_columns(table, target)
            pick = _update_column(table, spec, fixed)
            if pick is None or not where:
                continue
            col, new = pick
            value = held[nth % len(held)].get(col) if new and held else target.get(col)
            cols = [col]
            de.row_change_sql(dialect, "UPDATE", name, cols, where)
            values = (_param(value),) + tuple(_param(target[c]) for c in where)
        else:
            if nth >= len(held):
                continue  # only the reserved rows are pointed at by no foreign key
            target = rows[len(rows) - len(held) + nth]
            where = _match_columns(table, target)
            if not where:
                continue
            cols = []
            de.row_change_sql(dialect, "DELETE", name, cols, where)
            values = tuple(_param(target[c]) for c in where)
        out.append(
            ProofCall(
                routine.routine_id,
                event.lower(),
                "row_change",
                name,
                event,
                values,
                columns=tuple(cols),
                where=tuple(where),
            )
        )
    if not out:
        return [], "no row change fires it (TRUNCATE, or no row to change)"
    return out, None


# --------------------------------------------------------------------------- build


def build_proof_inputs(
    routines: Sequence[RoutineProofInput],
    tables: Sequence[Mapping[str, Any]],
    *,
    dialect: Dialect,
    seed: int = 0,
    max_rows: int = PROOF_ROWS,
    extensions: Collection[str] = (),
) -> ProofInputs:
    """The setup and the calls for ``routines`` over the collection's ``tables``.

    ``extensions`` names the PostgreSQL extensions the engine has; a table with a column
    of another extension's type (``EXTENSION_TYPES``) is left out of the setup, with why,
    and the rest still load. Time and memory grow linearly with the routines and tables.
    """
    index = TableIndex({str(t["table_id"]): t for t in tables})
    ordered = sorted(routines, key=lambda r: r.routine_id)
    missing: list[tuple[str, str]] = []
    touched: set[str] = set()
    trigger_table: dict[str, str] = {}
    skipped: list[SkippedRoutine] = []
    for r in ordered:
        names = list(r.tables) + ([r.trigger.table] if r.trigger else [])
        for name in names:
            tid = index.resolve(name, "")
            if tid is None:
                missing.append((r.routine_id, name))
            else:
                touched.add(tid)
        if r.trigger is not None:
            tid = index.resolve(r.trigger.table, "")
            if r.trigger.timing == "INSTEAD OF":
                skipped.append(SkippedRoutine(r.routine_id, "a trigger on a view is not generated"))
            elif tid is None:
                skipped.append(SkippedRoutine(r.routine_id, "its table is not in the collection"))
            else:
                trigger_table[r.routine_id] = tid

    selected: dict[str, Mapping[str, Any]] = {}
    skip: dict[str, str] = {}
    taken: set[str] = set()
    for tid in _closure(touched, index):
        if _name(index.by_id[tid]).lower() in taken:
            skip[tid] = "another table of the same name is already in the setup"
            continue
        taken.add(_name(index.by_id[tid]).lower())
        selected[tid] = _shaped(index.by_id[tid], dialect)
        if not _plain(index.by_id[tid]):
            skip[tid] = "its name or a column name is not a plain identifier"
    # A table whose type the engine lacks is not created at all; the rest still load.
    first = TableIndex(selected)
    lacking = _needs_extension(
        _generate(dialect, [_ddl_table(t, first, dialect) for t in selected.values()]),
        extensions,
    )
    for tid in sorted(selected):
        if _name(selected[tid]) in lacking:
            skip[tid] = lacking[_name(selected[tid])]
            del selected[tid]
    chosen = TableIndex(selected)
    reserve: dict[str, int] = {}
    for tid in trigger_table.values():
        reserve[tid] = reserve.get(tid, 0) + 1
    related = generate_related_rows(
        list(selected.values()),
        dialect=dialect,
        seed=seed,
        max_rows=max_rows,
        reserve=reserve,
        skip={k: v for k, v in skip.items() if k in selected},
    )
    ddl = _setup_ddl(
        _generate(dialect, [_ddl_table(selected[t], chosen, dialect) for t in related.order])
    )
    rows = tuple(
        (_name(selected[t]), tuple(related.tables[t].rows))
        for t in related.order
        if related.tables[t].rows
    )
    unseeded = dict(related.unseeded)
    unseeded.update({k: v for k, v in skip.items() if k not in selected})
    setup = ProofSetup(
        dialect=dialect,
        ddl=ddl,
        rows=rows,
        tables=related.order,
        cycle_breaks=related.cycle_breaks,
        unseeded=tuple(sorted(unseeded.items())),
        missing=tuple(missing),
    )
    referenced = _referenced_columns(chosen)

    # Row changes first, on the rows as seeded; then the calls, routine by routine.
    changed: list[ProofCall] = []
    calls: list[ProofCall] = []
    truncated: list[str] = []
    nth: dict[str, int] = {}
    for r in ordered:
        if r.kind == "trigger":
            if r.routine_id not in trigger_table:
                continue
            tid = trigger_table[r.routine_id]
            if tid not in selected:
                skipped.append(SkippedRoutine(r.routine_id, f"its table {tid}: {skip[tid]}"))
                continue
            changes, why = _trigger_changes(
                r, tid, nth.get(tid, 0), selected, referenced, related, dialect
            )
            nth[tid] = nth.get(tid, 0) + 1
            if why is not None:
                skipped.append(SkippedRoutine(r.routine_id, why))
            changed.extend(changes)
            continue
        if (why := _skip_reason(r, dialect)) is not None:
            skipped.append(SkippedRoutine(r.routine_id, why))
            continue
        made, cut = routine_calls(r, dialect)
        calls.extend(made)
        if cut:
            truncated.append(r.routine_id)
    return ProofInputs(
        setup=setup,
        calls=tuple(changed + calls),
        skipped=tuple(sorted(skipped, key=lambda s: s.routine_id)),
        truncated=tuple(truncated),
        seed=seed,
    )


# --------------------------------------------------------------------------- run


def load_setup(
    handle: de.EngineHandle, inputs: ProofInputs, routines: Sequence[de.SourceRoutine] = ()
) -> None:
    """Load the setup's tables and rows, then ``routines`` (in dependency order)."""
    de.load(handle, inputs.setup.ddl, inputs.setup.rows_by_table(), routines)


_CALL_KINDS = {"function", "procedure", "event"}


def _checked_casts(handle: de.EngineHandle, call: ProofCall) -> tuple[str, ...]:
    """The call's casts, checked again here: on PostgreSQL one per argument, each a type
    ``pg_cast_type`` writes back unchanged (never None: an uncast call could reach
    another overload); on MySQL none."""
    if handle.engine != "postgresql":
        if call.casts:
            raise ValueError("a MySQL call writes no cast")
        return ()
    if len(call.casts) != len(call.args) or any(c is None for c in call.casts):
        raise ValueError("a PostgreSQL call needs one cast per argument")
    for cast in call.casts:
        if de.pg_cast_type(str(cast)) != cast:
            raise ValueError(f"not a type name a cast may write: {cast!r}")
    return tuple(str(c) for c in call.casts)


def run_call(handle: de.EngineHandle, call: ProofCall) -> de.CallResult:
    """Run one generated call or row change on a loaded engine. The statement is built
    here, from the call's fields, and every field is checked before any SQL runs (the
    action, the routine kind, every name through ``row_change_sql``, every cast through
    ``pg_cast_type``), so a saved and edited call list cannot reach the engine as SQL;
    a PostgreSQL enum argument's label is looked up first."""
    if call.action == "row_change":
        event: Any = call.kind
        sql = de.row_change_sql(handle.engine, event, call.name, call.columns, call.where)
        return de.execute(handle, sql, call.args)
    if call.action != "call":
        raise ValueError(f"not an action: {call.action!r}")
    if call.kind not in _CALL_KINDS:
        raise ValueError(f"a call is to a function, a procedure or an event, not {call.kind!r}")
    if call.kind == "event":
        if handle.engine != "mysql":
            raise ValueError("an event runs on MySQL only")
        if call.args:
            raise ValueError("an event run takes no argument")
        if call.modes:
            raise ValueError("an event run takes no argument mode")
        if call.casts:
            raise ValueError("an event run takes no argument cast")
        de.event_runner_name(call.name)  # the name is checked before the engine sees it
        return de.call(handle, de.SourceRoutine(call.name, "event", ""), [], [], None)
    casts = _checked_casts(handle, call)
    args = list(call.args)
    for i, value in enumerate(args):
        if isinstance(value, dict) and set(value) == {"enum_label"}:
            if not casts:
                return de.CallResult(error="an enum argument needs its type to find a label")
            which = "enum_last" if value["enum_label"] == "last" else "enum_first"
            found = de.execute(
                handle,
                f"SELECT {which}(NULL::{casts[i]})::text",  # nosec B608 - a fixed function, a checked type name
            )
            if found.error is not None:
                return found
            args[i] = found.rows[0][0]
    # The engine reads a routine's kind from its name; the text is never needed for a call.
    kind: Any = call.kind
    routine = de.SourceRoutine(call.name, kind, "")
    modes: Any = call.modes
    return de.call(handle, routine, args, modes, casts or None)
