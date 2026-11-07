# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Side-by-side comparison of an original routine and its carried version (#1944).

ADR-034 Decision 5, step 3: a carried routine should behave as the original does.
:func:`compare_routines` runs the same generated calls
(:mod:`src.tools.routines.proof_inputs`) on two sides
(:class:`~src.tools.routines.proof_targets.ProofTarget`): the original routines, exactly
as collected, on a disposable engine of the source's engine and version, and the
carried routines on the test target, today a second disposable engine of the target's
version (the customer's Aurora and the test stack plug in through the same protocol,
#1945, #1946). Each call starts from the same seeded rows on both sides; its returned
values and every table change are compared row by row.

What a result claims. The best outcome is ``no_differences_found``: no difference was
found on the calls that ran. It is never a claim that the two routines behave alike on
every input, and every result lists its ``coverage_limits`` (:data:`COVERAGE_LIMITS`):
what was not compared, and why. The rule: when in doubt, the outcome is ``different``
or ``could_not_compare``, never ``no_differences_found``.

Outcomes, one :class:`RoutineComparison` per routine:

- ``no_differences_found``: every call was compared and matched, at least one call
  succeeded on both sides, and no more than half the calls failed on both sides: "no
  differences found on N calls (M succeeded on both sides)".
- ``different``: at least one call did not match, a call ended the caller's
  transaction on one side only, or the carried routine does not load. Each
  :class:`Difference` names the call, the table and row, the column and both values; the
  routine stays ``needs_attention``.
- ``could_not_compare``: no comparison was possible, with ``reason_code`` and a
  reason. Before any call: the original does not load on its own engine; a ``CREATE``
  the target refuses (:func:`~src.tools.routines.proof_screen.create_refusal`); no
  engine of the source's version; no generated call; a table the routine touches
  (listed, or named in its text) that the setup does not have; a statement built at run
  time (:func:`~src.tools.routines.proof_screen.dynamic_sql`); a call to a function the
  comparison cannot freeze (:func:`~src.tools.routines.proof_screen.nondeterministic_calls`),
  in its text, a routine it depends on, or a routine of its batch it calls by name; a
  named time zone on a MySQL engine with no zone tables. During the calls: a call ran
  past its time limit on either side; a call read a table the setup lacks (42P01,
  42S02); an error came back without a SQLSTATE or a message (a harness error); it ends
  the transaction itself (class ``2D``), or ends the caller's transaction on both sides;
  every call failed on both sides; more than half failed on both sides ("mostly
  errors"); a side failed.

How a call is compared:

- **Each call starts from the seeded rows.** Each side runs a call inside a transaction
  it rolls back afterwards. Before the call it resets the session (MySQL) and puts back
  every counter a rollback does not undo (sequences, ``AUTO_INCREMENT``), so both sides
  draw the same keys, and writes a sentinel row in the transaction. After the call a
  second connection looks for that row (seen: the call committed, even a ``COMMIT``
  with nothing else pending or a DDL statement's implicit commit), and so does the
  call's own session (gone: it rolled back, ``ROLLBACK`` or ``ROLLBACK AND CHAIN``).
  Each side also snapshots every table, whatever its name, before and after the call
  (a table the call creates included) and once more after the rollback. A call that
  ended the transaction on one side only is a difference; on both, *could not
  compare*. Such a side is started again before the next call.
- **Values keep their types.** Each result column's engine type is compared, and each
  value is compared with its kind (``true`` is not ``1``, ``1`` is not ``1.0``; inside
  JSON too). PostgreSQL ``interval`` and ``json`` are read as the engine writes them
  (``'1 mon'`` is not ``'30 days'``; a ``json`` value keeps its key order, a ``jsonb``
  one is compared by content). Returned rows are compared in order, and a MySQL
  procedure's result sets by their sizes too.
- **Table changes** are each side's rows removed and rows written by the call, compared
  as multisets of typed rows, and each table's column types. A written row is paired
  with the other side's by the table's primary key (else by most equal columns), and
  each column that differs is one difference.
- **Two errors match only on everything**: the full SQLSTATE, MySQL's error number, a
  digest of the message with its detail and hint (whitespace collapsed; a violated key
  is in the detail) and the constraint, table, column, type and schema the engine names.
  None of these is shown; a difference in them reads ``SQLSTATE P0001 (one message)``
  against ``(another message)``. Table changes are still compared after an error, since
  a MySQL routine keeps what it did before it.

Non-deterministic values. Only what the engine really freezes is frozen, and the rest is
not compared:

- **Frozen** (:mod:`src.tools.routines.proof_targets`). Each call gets one fixed
  instant, to the microsecond, and one random seed, the same on both sides
  (:func:`frozen_at`, :func:`call_seed`). MySQL: ``SET TIMESTAMP`` with the fraction,
  so a carry that loses microseconds differs, and ``rand_seed1``/``rand_seed2``.
  PostgreSQL: ``setseed``, so ``random()`` counts as frozen only when both sides run the
  same major.
- **Written exactly.** PostgreSQL's ``now()`` and ``CURRENT_TIMESTAMP`` read the
  transaction's start, which no setting fixes. Each side reads that instant inside the
  call's transaction; a date-time value within seven days of it is written as its exact
  offset and its form, ``<now+0us as dddd-dd-ddTdd:dd:dd+05:47>``: the date and time
  digits replaced and the offset kept, so two writings of one instant at another offset
  stay apart (the fraction of a second is in the exact offset). A date is written ``<today+0d as
  dddd-dd-dd>``. No slack: ``now()`` against ``now() - 900 ms`` differs.
- **Never normalised:** UUIDs, generated values (call arguments and seeded rows), and
  anything outside the seven days. So an unchanged routine that writes a clock value
  further away (``now() + interval '8 days'``), an interval or a time of day from the
  clock (``now() - x``, ``current_time``), an epoch (``extract(epoch from now())``) or a
  clock value inside text is reported as *different*: a false difference, listed in
  ``coverage_limits``.

Session time zone. Both sides run at ``+05:47``, an offset no named zone has (neither
UTC nor a whole hour, nor Kathmandu's ``+05:45``), so a routine that mixes local time,
UTC and a named zone differs instead of matching by chance. A MySQL engine loads the
named zones from the system's zoneinfo when ``mysql_tzinfo_to_sql`` is there; without
them, a routine that names a zone is *could not compare*.

No literal of the collection in a result. A difference shows generated values as they
are. Any other string is withheld (``<withheld #1: holds routine text>``, numbered per
distinct value, so two withheld values that differ read differently) when, case-folded,
it equals a string literal of a compared routine's text (one with a letter or a digit,
the literals redaction withholds, #1625), contains a literal or a literal's word of four
or more characters, or is itself four or more characters of a literal (``upper()``,
``substr()`` of one); the same for the text it decodes to as hex (a ``bytea`` value) or
base64, for an object whose key does, and for a number whose digits are a string
literal. A routine's body is code, not a literal: only the literals in it count. Only a
clock token :func:`normalise` writes is exempt. Some values are withheld that hold no
literal. When a text cannot be tokenized, every value that is not generated is withheld.
Errors are reported by SQLSTATE and error number only, never by the engine's message,
and no reason quotes a name from a routine's text. Nothing here logs routine text.

Batches. Routines are compared in batches (``batch_size``), each on fresh targets
loaded with the batch's routines, the routines they depend on, and the tables they
touch. A batch never holds two routines with the same name and signature (PostgreSQL
overloads are fine) or the same helper object, since the second ``CREATE OR REPLACE``
would replace the first; such a routine goes to another batch. A trigger, and any
routine whose statements create one, is alone in its batch, so no trigger fires on
another routine's calls. Work is linear in the calls times the batch's tables; a routine
or a batch that fails never stops the others.

Known limits, also in :data:`COVERAGE_LIMITS`: notices and warnings are not compared; a
statement built at run time is never read (such a routine is not compared); one session
time zone; temporary tables a routine creates are not snapshot (its text names a table
the setup lacks, so it is not compared); concurrency and locking are not compared; the
namespace screen reads names as written and as PostgreSQL's parser reads them, and
``SECURITY DEFINER`` is not refused (#1945 has to run routines as a role confined to
its scratch schema).

Input. :class:`ComparedRoutine` is one routine: its proof metadata
(:class:`~src.tools.routines.proof_inputs.RoutineProofInput`), its original ``CREATE``
statements and its carried ones (a trigger with its trigger function, in creation
order), and the ids of the routines it depends on. Routines are created in the order
given, so the caller passes them in dependency order
(:mod:`src.tools.routines.dependencies`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, computed_field

from src.tools.database import disposable_engine as de
from src.tools.routines.proof_inputs import (
    ProofCall,
    ProofInputs,
    RoutineProofInput,
    build_proof_inputs,
)
from src.tools.routines.proof_screen import (
    called_names,
    dynamic_sql,
    named_time_zones,
    nondeterministic_calls,
    touched_tables,
)
from src.tools.routines.proof_targets import (
    CreateRefused,
    ProofTarget,
    ProofTargetError,
    ProofTargetUnavailable,
    TargetFactory,
)
from src.tools.sql.facade import RoutineTokenError, routine_tokens
from src.tools.synthetic_data.generator import TableIndex

__all__ = [
    "BATCH_SIZE",
    "MAX_DIFFERENCES",
    "WITHHELD",
    "CallRun",
    "ComparedRoutine",
    "ComparisonReport",
    "Difference",
    "RoutineComparison",
    "ValueGuard",
    "call_seed",
    "compare_call",
    "compare_routines",
    "frozen_at",
    "normalise",
    "plan_batches",
]

#: Routines per batch: each batch gets fresh targets on both sides.
BATCH_SIZE = 100
#: Differences kept per routine; ``differences_total`` counts them all.
MAX_DIFFERENCES = 20
#: What every withheld value's placeholder ends with (``<withheld #1: holds routine
#: text>``, numbered per distinct value).
WITHHELD = "holds routine text>"
#: What a ``no_differences_found`` result never covers, listed with every result.
COVERAGE_LIMITS = (
    "only the generated calls ran: a baseline value and edge values per argument, on "
    "synthetic rows, at most 64 calls per routine; real data and other inputs were not tried",
    "notices, warnings and messages a call sends without failing are not compared",
    "a clock value further than seven days from the call's now(), an interval or time of "
    "day taken from the clock, or a clock value inside text is not normalised: an "
    "unchanged routine that writes one shows as different",
    "each call ran under one session time zone (+05:47); behaviour under the source's own "
    "zone setting was not compared",
    "a statement built at run time (dynamic SQL) is never read: such a routine is not " "compared",
    "triggers ran one per batch, so how two triggers of one table interact was not compared",
    "concurrency, locking and performance are not compared",
    "a routine runs as one role on each side: SECURITY DEFINER and the privileges a "
    "routine needs or grants are not compared",
)
_FROZEN_BASE = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)
_NEAR = datetime.timedelta(days=7)  # a clock value: within a week of the call's now
_MICRO = datetime.timedelta(microseconds=1)
_CLOCK_TOKEN = re.compile(
    r"^<(?:now[+-]\d+us|today[+-]\d+d) as dddd-dd-dd(?:[T ]dd:dd:dd)?(?:Z|[+-]\d{2}(?::?\d{2})?)?>$"
)
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_WORD = re.compile(r"[^\W_]{4,}")
_DIGIT = re.compile(r"\d")
_TRANSACTION_CONTROL = "2D"  # invalid transaction termination: COMMIT inside the call
_UNDEFINED_TABLE = {"42P01", "42S02"}
_TIMEOUTS = {"57014", "70100"}  # query canceled; query execution was interrupted
_HIDDEN = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
_CLASSES = {
    "0A": "feature not supported", "22": "data exception",
    "23": "integrity constraint violation", "25": "invalid transaction state",
    "28": "invalid authorization", "2B": "dependent objects still exist",
    "2F": "SQL routine exception", "38": "external routine exception",
    "39": "external routine invocation exception", "3D": "invalid catalog name",
    "3F": "invalid schema name", "40": "transaction rollback",
    "42": "syntax error or access rule violation", "44": "check option violation",
    "53": "insufficient resources", "54": "program limit exceeded",
    "55": "object not in prerequisite state", "57": "operator intervention",
    "58": "system error", "F0": "configuration file error", "HV": "foreign data wrapper error",
    "P0": "PL/pgSQL error", "XX": "internal error", "HY": "general error",
    "45": "unhandled user-defined exception",
}  # fmt: skip

Outcome = Literal["no_differences_found", "different", "could_not_compare"]
ReasonCode = Literal[
    "original_does_not_load",
    "carried_does_not_load",
    "outside_namespace",
    "dependency_not_compared",
    "source_engine_unavailable",
    "target_unavailable",
    "engines_differ",
    "engine_failed",
    "no_calls",
    "no_successful_call",
    "table_not_observed",
    "harness_error",
    "transaction_control",
    "commits",
    "transaction_ended",
    "dynamic_sql",
    "named_time_zone",
    "table_unseeded",
    "refcursor",
    "timeout",
    "non_deterministic",
    "mostly_errors",
    "call_refused",
]


# --------------------------------------------------------------------------- input


@dataclass(frozen=True)
class ComparedRoutine:
    """One routine to compare: its proof metadata, its original ``CREATE`` statements
    (exactly as collected) and its carried ones, each in creation order (a trigger's
    function, then the trigger), and the ids of the routines it depends on. The texts
    are left out of ``repr``."""

    proof: RoutineProofInput
    original: tuple[de.SourceRoutine, ...] = field(repr=False)
    carried: tuple[de.SourceRoutine, ...] = field(repr=False)
    depends_on: tuple[str, ...] = ()

    @property
    def routine_id(self) -> str:
        return self.proof.routine_id


# --------------------------------------------------------------------------- output


class Difference(BaseModel):
    """One way the carried routine behaved differently.

    - ``aspect``: ``load`` (the carried routine did not load), ``error`` (an error on one
      side only, or another error; the values are ``SQLSTATE xxxxx``, with the MySQL
      error number, or ``no error``), ``result`` (a returned value: ``row`` is
      ``{"row": n}`` from 1, ``column`` ``#n`` from 1; or, with neither, the result
      sets' sizes), ``table`` (a table change) or ``transaction`` (the call committed on
      one side only: ``committed`` or ``rolled back``).
    - ``call``: the call's label (``baseline``, ``n=max``, ``insert``) and ``call_index``
      its position among the routine's calls, from 1; None for ``load``.
    - for ``table``: ``table``, ``change`` (``written`` or ``removed``), ``row`` (the
      row's primary key, or the whole row when the table has none), and ``column`` with
      both values, or no column when the row changed on one side only (its value on that
      side is the row, the other None).
    """

    model_config = _HIDDEN

    aspect: Literal["load", "error", "result", "table", "transaction", "session", "sequence"]
    call: str | None = None
    call_index: int | None = None
    table: str | None = None
    change: Literal["written", "removed"] | None = None
    row: dict[str, Any] | None = None
    column: str | None = None
    source: Any = None
    target: Any = None


class RoutineComparison(BaseModel):
    """The comparison of one routine: ``outcome``, the calls compared and the calls
    that succeeded on both sides, why (``reason``, and ``reason_code`` when it is not
    ``same``), and its differences (at most ``MAX_DIFFERENCES``; ``differences_total``
    counts all). ``source`` and ``target`` name where each side ran."""

    model_config = _HIDDEN

    routine_id: str
    outcome: Outcome
    reason: str
    reason_code: ReasonCode | None = None
    calls_compared: int = 0
    calls_succeeded: int = 0
    differences: tuple[Difference, ...] = ()
    differences_total: int = 0
    source: str | None = None
    target: str | None = None
    coverage_limits: tuple[str, ...] = COVERAGE_LIMITS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> Literal["no_differences_found", "needs_attention", "not_compared"]:
        """``no_differences_found``, ``needs_attention`` (different) or ``not_compared``
        (could not compare). No status says more than that no difference was found on
        the calls that ran, within ``coverage_limits``."""
        return {"no_differences_found": "no_differences_found",
                "different": "needs_attention"}.get(self.outcome, "not_compared")  # type: ignore[return-value]  # fmt: skip


class ComparisonReport(BaseModel):
    model_config = _HIDDEN

    results: tuple[RoutineComparison, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> dict[str, int]:
        found = Counter(r.outcome for r in self.results)
        return {
            k: found.get(k, 0) for k in ("no_differences_found", "different", "could_not_compare")
        }

    def by_id(self) -> dict[str, RoutineComparison]:
        return {r.routine_id: r for r in self.results}


# --------------------------------------------------------------------------- freezing


def frozen_at(call_index: int) -> datetime.datetime:
    """The fixed instant call ``call_index`` runs at on both sides (MySQL ``SET
    TIMESTAMP``), with a fraction of a second, so a carry that loses it differs."""
    return _FROZEN_BASE + datetime.timedelta(seconds=call_index, microseconds=123456)


def call_seed(seed: int, call_index: int) -> float:
    """The random seed of one call, the same on both sides, in PostgreSQL's
    ``setseed`` range."""
    return ((seed * 7919 + call_index * 104729) % 2000) / 1000.0 - 1.0


# --------------------------------------------------------------------------- values


_CLOCK_PARTS = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:([T ])(\d{2}:\d{2}:\d{2})(\.\d+)?)?(.*)$")


def _clock_shape(text: str) -> str:
    match = _CLOCK_PARTS.match(text)
    if match is None:
        return _DIGIT.sub("d", text)
    _, sep, _, fraction, rest = match.groups()
    shape = "dddd-dd-dd"
    if sep is not None:
        # the fraction is in the exact offset already, and Python writes none for a whole
        # second, so only the separator, the time and the offset make the form
        shape += f"{sep}dd:dd:dd"
    return shape + rest


def _clock_token(text: str, now: datetime.datetime) -> str | None:
    if not 10 <= len(text) <= 40 or not text[:4].isdigit() or text[4:5] != "-":
        return None
    # the written form: the date and time digits written d and the offset kept, so two
    # writings of one instant (at UTC, at +05:47) stay apart
    shape = _clock_shape(text)
    if len(text) == 10:
        try:
            day = datetime.date.fromisoformat(text)
        except ValueError:
            return None
        days = (day - now.date()).days
        return f"<today{days:+d}d as {shape}>" if abs(days) <= _NEAR.days else None
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)  # read as written, then offset
    delta = moment - now
    if abs(delta) > _NEAR:
        return None
    return f"<now{delta // _MICRO:+d}us as {shape}>"


def normalise(value: Any, now: datetime.datetime | None, generated: Collection[str] = ()) -> Any:
    """``value`` with each clock value written relative to the call's ``now``: a
    date-time within seven days of it as its exact offset in microseconds and its
    written form (``<now-900000us as dddd-dd-dd dd:dd:dd+dd>``, the fraction left out of the form), a date as its
    offset in days. A value in ``generated`` (its ``str``) is kept as it is, and nothing
    else is changed: no UUID, no value further away. Lists and objects item by item."""
    if isinstance(value, list):
        return [normalise(v, now, generated) for v in value]
    if isinstance(value, dict):
        return {k: normalise(v, now, generated) for k, v in value.items()}
    if not isinstance(value, str) or value in generated or now is None:
        return value
    token = _clock_token(value, now)
    return value if token is None else token


class ValueGuard:
    """Decides how a value appears in a difference: as it is when it is generated, a
    number, a clock token or holds no literal of the compared routines' text, else
    :data:`WITHHELD` (see the module docstring for the matching rules).

    ``literals`` are the string literals of the texts (only those with a letter or a
    digit count); ``strict`` (a text did not tokenize) withholds every string that is
    not generated.
    """

    def __init__(
        self, literals: Iterable[str], generated: Collection[str], strict: bool = False
    ) -> None:
        folded = {lit.casefold() for lit in literals if any(c.isalnum() for c in lit)}
        self.literals = frozenset(folded)
        self._fragments = tuple(
            sorted({lit for lit in folded if len(lit) >= 4}
                   | {w for lit in folded for w in _WORD.findall(lit)})
        )  # fmt: skip
        self._joined = "\x00".join(sorted(folded))
        self.generated = generated
        self.strict = strict
        self._numbers: dict[str, int] = {}

    def _withheld(self, value: Any) -> str:
        """The placeholder for one withheld value: numbered per distinct value, so two
        withheld values that differ read differently."""
        key = json.dumps(_typed(value), sort_keys=True, default=str)
        number = self._numbers.setdefault(key, len(self._numbers) + 1)
        return f"<withheld #{number}: holds routine text>"

    @classmethod
    def for_texts(cls, texts: Iterable[str], engine: str, generated: Collection[str]) -> ValueGuard:
        literals: set[str] = set()
        strict = False
        for text in texts:
            try:
                literals |= _literals(text, engine)
            except RoutineTokenError:
                strict = True
        return cls(literals, generated, strict)

    def _holds(self, folded: str) -> bool:
        if folded in self.literals or any(f in folded for f in self._fragments):
            return True
        return len(folded) >= 4 and folded in self._joined

    def withholds(self, value: str) -> bool:
        if value in self.generated or _CLOCK_TOKEN.match(value):
            return False
        if self.strict:
            return True
        return any(self._holds(form.casefold()) for form in _readings(value))

    def shown(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.shown(v) for v in value]
        if isinstance(value, dict):
            if any(self.withholds(str(k)) for k in value):
                return self._withheld(value)  # a JSON key can be a literal too
            return {k: self.shown(v) for k, v in value.items()}
        if isinstance(value, str) and self.withholds(value):
            return self._withheld(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if str(value) in self.generated:
                return value
            if self.strict or str(value).casefold() in self.literals:
                return self._withheld(value)  # a number written as a string literal
        return value


def _readings(value: str) -> list[str]:
    """``value``, and the text it decodes to when it reads as hex (a ``bytea`` or binary
    value) or base64, so a literal written in either is still found."""
    forms = [value]
    text = value[2:] if value.startswith(("\\x", "0x")) else value
    if len(text) >= 8 and len(text) % 2 == 0 and _HEX.match(text):
        forms.append(bytes.fromhex(text).decode("utf-8", "replace"))
    if len(value) >= 8 and len(value) % 4 == 0 and _BASE64.match(value):
        with contextlib.suppress(ValueError, binascii.Error):
            forms.append(base64.b64decode(value, validate=True).decode("utf-8", "replace"))
    return forms


def _literals(text: str, engine: str, depth: int = 0) -> set[str]:
    """The string literals of ``text`` with a letter or a digit, and those inside each
    string (a PostgreSQL body, a dynamic statement) down to three levels. A routine's
    body (the string after ``AS``) is code, not a literal: only the literals in it count."""
    found: set[str] = set()
    tokens = routine_tokens(text, engine)
    for i, tok in enumerate(tokens):
        if not (tok.command_rest or tok.kind.endswith("STRING")):
            continue
        body = depth == 0 and i > 0 and tokens[i - 1].text.upper() in ("AS", "DO")
        if not body and any(c.isalnum() for c in tok.text):
            found.add(tok.text)
        if depth < 3:
            try:
                found |= _literals(tok.text, engine, depth + 1)
            except RoutineTokenError:
                pass  # an inner text that is not SQL is a literal already taken
    return found


def _generated_values(inputs: ProofInputs) -> frozenset[str]:
    out: set[str] = set()
    for _, rows in inputs.setup.rows:
        for row in rows:
            out.update(str(v) for v in row.values() if v is not None)
    for call in inputs.calls:
        out.update(str(a) for a in call.args if a is not None and not isinstance(a, dict))
    return frozenset(out)


# --------------------------------------------------------------------------- one call


@dataclass(frozen=True)
class CallRun:
    """What one side gave for one call: its result, its table snapshots before and
    after, the instant its clock read (``now``) and whether the call committed (its rows
    after the rollback were not the seeded ones)."""

    result: de.CallResult
    before: Mapping[str, list[dict[str, Any]]]
    after: Mapping[str, list[dict[str, Any]]]
    now: datetime.datetime | None = None
    transaction: str = "open"  # or "committed", "rolled back"
    types: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: The session settings and user variables the call changed (None: not read).
    session: Mapping[str, str | None] | None = None
    #: Each sequence's last value and each MySQL table's next AUTO_INCREMENT after it.
    counters: Mapping[str, str] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        """The call ended the caller's transaction (committed or rolled it back)."""
        return self.transaction != "open"


def _state(result: de.CallResult) -> str:
    if result.error is None:
        return "no error"
    text = f"SQLSTATE {result.sqlstate or '?????'}"
    if result.errno is not None:
        text += f" (error {result.errno})"
    if result.timed_out or result.sqlstate in _TIMEOUTS:
        text += " (timed out)"
    return text


def _digest(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(" ".join(text.split()).encode("utf-8", "replace")).hexdigest()


def _error_key(r: de.CallResult) -> tuple[Any, ...] | None:
    """What two errors must share to be the same: the full SQLSTATE, MySQL's error
    number, a digest of the message (whitespace collapsed) and the constraint. None when
    the error lacks a SQLSTATE or a message: such an error never matches."""
    if not r.sqlstate or r.message is None or r.timed_out:
        return None
    return (r.sqlstate, r.errno, _digest(r.message), r.constraint)


def _same_error(a: de.CallResult, b: de.CallResult) -> bool:
    """No error on both sides, or two errors with the same :func:`_error_key`."""
    if a.error is None or b.error is None:
        return a.error is None and b.error is None
    key = _error_key(a)
    return key is not None and key == _error_key(b)


def _typed(value: Any) -> Any:
    """``value`` with each scalar tagged by its kind, so ``True`` is not ``1`` and ``1``
    is not ``1.0`` when two values are compared (Python's ``==`` mixes them)."""
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", repr(value)]
    if isinstance(value, list):
        return ["list", [_typed(v) for v in value]]
    if isinstance(value, dict):
        return ["object", sorted([str(k), _typed(v)] for k, v in value.items())]
    return value


def _canon(row: Mapping[str, Any]) -> str:
    return json.dumps(_typed(dict(row)), sort_keys=True, default=str)


def _delta(
    run: CallRun, table: str, generated: Collection[str]
) -> tuple[Counter[str], Counter[str], dict[str, dict[str, Any]]]:
    """Rows the call removed and wrote on ``table`` (multisets of normalised rows, keyed
    by their typed form)."""
    rows: dict[str, dict[str, Any]] = {}
    for r in (*run.before.get(table, []), *run.after.get(table, [])):
        rows.setdefault(_canon(r), r)
    before = Counter(_canon(r) for r in run.before.get(table, []))
    after = Counter(_canon(r) for r in run.after.get(table, []))
    shapes: dict[str, dict[str, Any]] = {}
    out: list[Counter[str]] = []
    for part in (before - after, after - before):
        counted: Counter[str] = Counter()
        for key, n in part.items():
            row = normalise(dict(rows[key]), run.now, generated)
            shapes[_canon(row)] = row
            counted[_canon(row)] += n
        out.append(counted)
    return out[0], out[1], shapes


def _pair(
    left: list[dict[str, Any]], right: list[dict[str, Any]], key: Sequence[str]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pairs rows of the two sides: by ``key`` when every key column is set, then by most
    equal columns (at least one). Returns the pairs and each side's rows left over."""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rest_right = list(right)
    rest_left: list[dict[str, Any]] = []
    for row in left:
        match = None
        if key and all(row.get(c) is not None for c in key):
            match = next((r for r in rest_right if all(r.get(c) == row.get(c) for c in key)), None)
        if match is None:
            scored = [
                (sum(1 for c in row if c in r and r[c] == row[c]), i)
                for i, r in enumerate(rest_right)
            ]
            best = max(scored, default=(0, -1))
            if best[0] > 0:
                match = rest_right[best[1]]
        if match is None:
            rest_left.append(row)
        else:
            rest_right.remove(match)
            pairs.append((row, match))
    return pairs, rest_left, rest_right


def _expand(counter: Counter[str], shapes: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [shapes[k] for k in sorted(counter) for _ in range(counter[k])]


def _identity(row: Mapping[str, Any], key: Sequence[str]) -> dict[str, Any]:
    if key and all(c in row for c in key):
        return {c: row[c] for c in key}
    return dict(row)


def _table_differences(
    table: str,
    change: Literal["written", "removed"],
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    key: Sequence[str],
    call: str,
    index: int,
    guard: ValueGuard,
) -> list[Difference]:
    out: list[Difference] = []
    pairs, only_source, only_target = _pair(source_rows, target_rows, key)
    for s, t in pairs:
        for column in sorted(set(s) | set(t)):
            if _typed(s.get(column)) != _typed(t.get(column)):
                out.append(
                    Difference(
                        aspect="table", call=call, call_index=index, table=table,
                        change=change, row=guard.shown(_identity(s, key)), column=column,
                        source=guard.shown(s.get(column)), target=guard.shown(t.get(column)),
                    )
                )  # fmt: skip
    for row in only_source:
        out.append(
            Difference(aspect="table", call=call, call_index=index, table=table, change=change,
                       row=guard.shown(_identity(row, key)), source=guard.shown(row))
        )  # fmt: skip
    for row in only_target:
        out.append(
            Difference(aspect="table", call=call, call_index=index, table=table, change=change,
                       row=guard.shown(_identity(row, key)), target=guard.shown(row))
        )  # fmt: skip
    return out


def compare_call(
    source: CallRun,
    target: CallRun,
    *,
    call: str,
    index: int,
    tables: Sequence[str],
    keys: Mapping[str, Sequence[str]] | None = None,
    guard: ValueGuard | None = None,
) -> list[Difference]:
    """Every difference between the two sides' runs of one call (empty: the same).

    Errors match on the full SQLSTATE and error number; returned values are compared
    in order, after :func:`normalise`, with a MySQL procedure's result-set sizes; table
    changes as multisets of normalised rows (``tables`` and any table only the call
    created), paired by the table's key (``keys``) for column-level differences; a
    commit on one side only is a ``transaction`` difference."""
    guard = guard or ValueGuard((), frozenset())
    generated = guard.generated
    keys = keys or {}
    out: list[Difference] = []
    a, b = source.result, target.result
    if source.transaction != target.transaction:
        out.append(
            Difference(aspect="transaction", call=call, call_index=index,
                       source=source.transaction, target=target.transaction)
        )  # fmt: skip
    if not _same_error(a, b):
        source_state, target_state = _state(a), _state(b)
        if a.error is not None and b.error is not None and source_state == target_state:
            source_state += " (one message)"
            target_state += " (another message)"
        out.append(
            Difference(aspect="error", call=call, call_index=index, source=source_state,
                       target=target_state)
        )  # fmt: skip
    elif a.error is None:
        if a.column_names != b.column_names:
            out.append(
                Difference(aspect="result", call=call, call_index=index, column="names",
                           source=guard.shown([list(n) for n in a.column_names]),
                           target=guard.shown([list(n) for n in b.column_names]))
            )  # fmt: skip
        if a.column_types != b.column_types:
            out.append(
                Difference(aspect="result", call=call, call_index=index, column="types",
                           source=[list(t) for t in a.column_types],
                           target=[list(t) for t in b.column_types])
            )  # fmt: skip
        if a.result_sets != b.result_sets:
            out.append(
                Difference(aspect="result", call=call, call_index=index,
                           source=list(a.result_sets or ()), target=list(b.result_sets or ()))
            )  # fmt: skip
        rows_a = normalise(a.rows, source.now, generated)
        rows_b = normalise(b.rows, target.now, generated)
        for i in range(max(len(rows_a), len(rows_b))):
            ra = rows_a[i] if i < len(rows_a) else None
            rb = rows_b[i] if i < len(rows_b) else None
            if _typed(ra) == _typed(rb):
                continue
            if ra is None or rb is None or len(ra) != len(rb):
                out.append(
                    Difference(aspect="result", call=call, call_index=index, row={"row": i + 1},
                               source=guard.shown(ra), target=guard.shown(rb))
                )  # fmt: skip
                continue
            for j, (va, vb) in enumerate(zip(ra, rb, strict=True)):
                if _typed(va) != _typed(vb):
                    out.append(
                        Difference(aspect="result", call=call, call_index=index,
                                   row={"row": i + 1}, column=f"#{j + 1}",
                                   source=guard.shown(va), target=guard.shown(vb))
                    )  # fmt: skip
    if source.session is not None and target.session is not None:
        for name in sorted(set(source.session) | set(target.session)):
            sa, sb = source.session.get(name), target.session.get(name)
            if sa != sb:
                out.append(
                    Difference(aspect="session", call=call, call_index=index, column=name,
                               source=guard.shown(sa), target=guard.shown(sb))
                )  # fmt: skip
    for name in sorted(set(source.counters) | set(target.counters)):
        va, vb = source.counters.get(name), target.counters.get(name)
        if va != vb:
            out.append(
                Difference(aspect="sequence", call=call, call_index=index, column=name,
                           source=va, target=vb)
            )  # fmt: skip
    every = list(tables) + sorted((set(source.after) | set(target.after)) - set(tables))
    for table in every:
        types_a, types_b = source.types.get(table, {}), target.types.get(table, {})
        if types_a != types_b:
            out.append(
                Difference(aspect="table", call=call, call_index=index, table=table,
                           column="types", source=dict(types_a), target=dict(types_b))
            )  # fmt: skip
        removed_a, written_a, shapes_a = _delta(source, table, generated)
        removed_b, written_b, shapes_b = _delta(target, table, generated)
        key = keys.get(table, ())
        for change, ca, cb in (
            ("removed", removed_a, removed_b),
            ("written", written_a, written_b),
        ):
            if ca == cb:
                continue
            out.extend(
                _table_differences(
                    table, change, _expand(ca - cb, shapes_a), _expand(cb - ca, shapes_b),  # type: ignore[arg-type]
                    key, call, index, guard,
                )
            )  # fmt: skip
    return out


# --------------------------------------------------------------------------- batches


def _short(name: str) -> str:
    return name.rpartition(".")[2].strip('"`').casefold()


def _signature(proof: RoutineProofInput, dialect: str) -> tuple[str, ...]:
    if dialect != "postgresql":
        return ()  # MySQL has no overloads
    skipped = ("OUT", "TABLE") if proof.kind == "function" else ("TABLE",)
    return tuple(" ".join(p.data_type.casefold().split())
                 for p in proof.parameters if p.mode not in skipped)  # fmt: skip


def _keys(routine: ComparedRoutine, dialect: str) -> set[tuple[Any, ...]]:
    """What a routine's statements create, as keys two routines of a batch must not share:
    its own name and signature (a trigger's name and table on PostgreSQL), and the name
    of every helper object (a trigger function) it creates."""
    proof = routine.proof
    if proof.kind == "trigger":
        table = _short(proof.trigger.table) if proof.trigger and dialect == "postgresql" else ""
        own: tuple[Any, ...] = ("trigger", _short(proof.name), table)
    else:
        space = "routine" if dialect == "postgresql" else proof.kind
        own = (space, _short(proof.name), _signature(proof, dialect))
    keys = {own}
    for statement in (*routine.original, *routine.carried):
        if _short(statement.name) == _short(proof.name) and statement.kind == proof.kind:
            continue
        keys.add(("helper", statement.kind, _short(statement.name)))
    return keys


@dataclass
class _Batch:
    kind: str
    members: list[ComparedRoutine] = field(default_factory=list)
    owners: dict[tuple[Any, ...], str] = field(default_factory=dict)
    triggers: list[RoutineProofInput] = field(default_factory=list)


_OPEN_BATCHES = 8  # batches still taking members: a bound, so planning stays linear


def plan_batches(
    routines: Sequence[ComparedRoutine], *, batch_size: int, dialect: str
) -> list[list[ComparedRoutine]]:
    """The routines in batches of at most ``batch_size``, in order, such that no two
    routines of a batch (with the routines they depend on) create the same object. A
    trigger, and any routine that loads one, is alone in its batch, so no trigger fires
    on another routine's calls, whatever its helpers write."""
    by_id = {r.routine_id: r for r in routines}
    order = {r.routine_id: i for i, r in enumerate(routines)}
    batches: list[_Batch] = []
    for routine in routines:
        closure = _closure([routine], by_id, order)
        owners = {k: m.routine_id for m in closure for k in _keys(m, dialect)}
        triggers = [
            m.proof
            for m in closure
            if m.proof.kind == "trigger"
            or any(st.kind == "trigger" for st in (*m.original, *m.carried))
        ]
        kind = "trigger" if triggers else "routine"
        home = None
        for batch in [] if triggers else reversed(batches[-_OPEN_BATCHES:]):
            if (
                len(batch.members) < batch_size
                and batch.kind == kind
                and all(batch.owners.get(k, rid) == rid for k, rid in owners.items())
            ):
                home = batch
                break
        if home is None:
            home = _Batch(kind)
            batches.append(home)
        home.members.append(routine)
        home.owners.update(owners)
        home.triggers.extend(t for t in triggers if t not in home.triggers)
    return [b.members for b in batches]


def _reached(
    routine: ComparedRoutine,
    members: Sequence[ComparedRoutine],
    by_id: Mapping[str, ComparedRoutine],
    order: Mapping[str, int],
    dialect: str,
) -> list[ComparedRoutine]:
    """The routine, what it depends on, and every routine loaded in its batch that its
    texts name (a call it did not list), transitively."""
    by_name: dict[str, list[ComparedRoutine]] = {}
    for m in members:
        for statement in (*m.original, *m.carried):
            by_name.setdefault(_short(statement.name), []).append(m)
    seen: dict[str, ComparedRoutine] = {}
    todo = [routine]
    while todo:
        current = todo.pop()
        if current.routine_id in seen:
            continue
        seen[current.routine_id] = current
        todo.extend(_closure([current], by_id, order))
        for statement in (*current.original, *current.carried):
            for name in called_names(statement.create_sql, dialect):
                todo.extend(by_name.get(name, []))
    return sorted(seen.values(), key=lambda r: order.get(r.routine_id, 0))


def _closure(
    batch: Sequence[ComparedRoutine],
    by_id: Mapping[str, ComparedRoutine],
    order: Mapping[str, int],
) -> list[ComparedRoutine]:
    seen: set[str] = set()
    todo = [r.routine_id for r in batch]
    while todo:
        rid = todo.pop()
        if rid in seen or rid not in by_id:
            continue
        seen.add(rid)
        todo.extend(by_id[rid].depends_on)
    return sorted((by_id[r] for r in seen), key=lambda r: order[r.routine_id])


# --------------------------------------------------------------------------- orchestration


def _class_name(sqlstate: str) -> str:
    return _CLASSES.get(sqlstate[:2].upper(), f"SQLSTATE class {sqlstate[:2]}")


def _calls(n: int) -> str:
    return f"{n} call" + ("" if n == 1 else "s")


class _Leaked(RuntimeError):
    """A side's rows before a call are not its seeded rows."""


@dataclass
class _Side:
    """One side of a batch: its target, how to open it again, and its seeded rows."""

    which: Literal["source", "target"]
    factory: TargetFactory
    inputs: ProofInputs
    statements: list[tuple[str, de.SourceRoutine]]
    tables: tuple[str, ...]
    target: ProofTarget | None = None
    failed: dict[str, str] = field(default_factory=dict)  # routine id -> SQLSTATE
    refused: dict[str, str] = field(default_factory=dict)  # routine id -> why
    baseline: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    baseline_names: frozenset[str] = frozenset()

    def open(self) -> None:
        self.close()
        target = self.factory()
        self.target = target
        try:
            target.load_setup(self.inputs.setup)
            self.failed, self.refused = {}, {}
            for routine_id, statement in self.statements:
                if routine_id in self.failed or routine_id in self.refused:
                    continue
                try:
                    sqlstate = target.create(statement)
                except CreateRefused as exc:
                    self.refused[routine_id] = str(exc)
                    continue
                if sqlstate is not None:
                    self.failed[routine_id] = sqlstate
            target.prepare_calls()
            self.baseline_names = frozenset(target.tables())
            self.baseline = target.snapshot(self.tables)
        except BaseException:  # a half-loaded side is never used
            self.close()
            raise

    def run(self, call: ProofCall, index: int, seed: int) -> CallRun:
        target = self.target
        assert target is not None  # nosec B101 - opened before any call
        target.begin_call(frozen_at(index), call_seed(seed, index))
        try:
            now = target.now()
            session_before = target.session_state()
            before = target.snapshot(self.tables)
            if before != self.baseline:
                raise _Leaked(self.which)
            result = target.run(call)
            if result.timed_out or result.sqlstate in _TIMEOUTS:
                # nothing after an interrupted call is read: the side is opened again
                return CallRun(result, before, before, now)
            created = sorted(t for t in target.tables() if t not in self.baseline_names)
            after = target.snapshot([*self.tables, *created])
            types = target.column_types([*self.tables, *created])
            state = target.transaction_state()  # committed, or rolled back in its session
            session_after = target.session_state()
            counters = target.counter_state()
        finally:
            try:
                target.end_call()
            except ProofTargetError:
                pass  # the check below then fails, and the side is opened again
        if state == "open" and (
            frozenset(target.tables()) != self.baseline_names
            or target.snapshot(self.tables) != self.baseline
        ):
            state = "committed"  # rows the rollback did not take back
        session: dict[str, str | None] | None = None
        if session_before is not None and session_after is not None:
            session = {
                k: session_after.get(k)
                for k in set(session_before) | set(session_after)
                if session_before.get(k) != session_after.get(k)
            }
        return CallRun(result, before, after, now, state, types, session, counters)

    def close(self) -> None:
        if self.target is not None:
            target, self.target = self.target, None
            target.close()


def _table_names(
    inputs: ProofInputs, tables: Sequence[Mapping[str, Any]]
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], frozenset[str]]:
    """The setup's tables (every one is snapshot, whatever its name), each one's primary
    key, and their ids."""
    by_id = {str(t["table_id"]): t for t in tables}
    names: list[str] = []
    keys: dict[str, tuple[str, ...]] = {}
    observed: set[str] = set()
    for tid in inputs.setup.tables:
        table = by_id.get(tid, {})
        name = str(table.get("table_name") or tid.rpartition(".")[2])
        names.append(name)  # any spelling: the engine quotes it
        keys[name] = tuple(table.get("primary_key") or ())
        observed.add(tid)
    return tuple(names), keys, frozenset(observed)


def _with_text_tables(
    routine: ComparedRoutine, index: TableIndex, dialect: str
) -> RoutineProofInput:
    """The routine's proof input with every collection table its texts name added to its
    tables, so the setup creates (and seeds, when it can) a table the routine touches
    without listing it, and a table it creates but cannot seed is recorded as such."""
    named = [
        name
        for statement in (*routine.original, *routine.carried)
        for name in touched_tables(statement.create_sql, dialect, statement.kind)
        if index.resolve(name, "") is not None
    ]
    extra = tuple(n for n in dict.fromkeys(named) if n not in routine.proof.tables)
    if not extra:
        return routine.proof
    return routine.proof.model_copy(update={"tables": (*routine.proof.tables, *extra)})


def _unseeded(
    routine: ComparedRoutine,
    index: TableIndex,
    unseeded: Mapping[str, str],
    observed: frozenset[str],
    dialect: str,
) -> str | None:
    """Why the routine touches a table the setup created without rows: an ``UPDATE`` or
    ``DELETE`` of it changes nothing on either side, so nothing about it is compared."""
    proof = routine.proof
    names = list(proof.tables) + ([proof.trigger.table] if proof.trigger else [])
    from_text = [
        name
        for statement in (*routine.original, *routine.carried)
        for name in touched_tables(statement.create_sql, dialect, statement.kind)
    ]
    for name in dict.fromkeys([*names, *from_text]):
        tid = index.resolve(name, "")
        if tid is not None and tid in unseeded and tid in observed:  # created, but empty
            return f"it touches {tid}, which the setup created without rows ({unseeded[tid]})"
    return None


def _unobserved(
    routine: ComparedRoutine, index: TableIndex, observed: frozenset[str], dialect: str
) -> str | None:
    """Why a table the routine touches cannot be observed: its listed tables, its
    trigger's table and every table its texts name (:func:`touched_tables`)."""
    proof = routine.proof
    listed = list(proof.tables) + ([proof.trigger.table] if proof.trigger else [])
    for name in dict.fromkeys(listed):  # the collection's own names
        tid = index.resolve(name, "")
        if tid is None:
            return f"it touches {name}, which the collection does not have"
        if tid not in observed:
            return f"it touches {tid}, which the setup could not create"
    for statement in (*routine.original, *routine.carried):
        for name in touched_tables(statement.create_sql, dialect, statement.kind):
            tid = index.resolve(name, "")
            if tid is None or tid not in observed:  # a name from its text: never quoted
                return "its text names a table the setup does not have"
    return None


def _all(
    batch: Iterable[ComparedRoutine], outcome: Outcome, code: ReasonCode, reason: str, **kw: Any
) -> dict[str, RoutineComparison]:
    return {
        r.routine_id: RoutineComparison(
            routine_id=r.routine_id, outcome=outcome, reason=reason, reason_code=code, **kw
        )
        for r in batch
    }


def _open(side: _Side) -> tuple[ReasonCode, str] | None:
    """Open one side; on failure, the reason every routine of the batch gets."""
    try:
        side.open()
    except ProofTargetUnavailable as exc:
        code: ReasonCode = (
            "source_engine_unavailable" if side.which == "source" else "target_unavailable"
        )
        return code, f"no {side.which} to run on: {exc}"
    except ProofTargetError as exc:
        state = f" (SQLSTATE {exc.sqlstate})" if exc.sqlstate else ""
        return "engine_failed", f"the {side.which} side failed: {exc}{state}"
    return None


@dataclass
class _Batchwide:
    """What every routine of a batch shares."""

    sides: Mapping[str, _Side]
    calls: Mapping[str, list[ProofCall]]
    skipped: Mapping[str, str]
    keys: Mapping[str, Sequence[str]]
    guard: ValueGuard
    index: TableIndex
    observed: frozenset[str]
    seed: int
    max_differences: int
    names: Mapping[str, str]
    dialect: str = "postgresql"
    random_seeded: bool = False
    named_time_zones: bool = True
    unseeded: Mapping[str, str] = field(default_factory=dict)
    reached: Callable[[ComparedRoutine], list[ComparedRoutine]] = lambda r: [r]


def _compare_batch(
    batch: Sequence[ComparedRoutine],
    by_id: Mapping[str, ComparedRoutine],
    order: Mapping[str, int],
    tables: Sequence[Mapping[str, Any]],
    source: TargetFactory,
    target: TargetFactory,
    *,
    dialect: str,
    seed: int,
    extensions: Collection[str],
    max_differences: int,
) -> dict[str, RoutineComparison]:
    members = _closure(batch, by_id, order)
    index = TableIndex({str(t["table_id"]): t for t in tables})
    inputs = build_proof_inputs(
        [_with_text_tables(m, index, dialect) for m in members], tables, dialect=dialect,  # type: ignore[arg-type]
        seed=seed, extensions=extensions,
    )  # fmt: skip
    names, keys, observed = _table_names(inputs, tables)
    sides = {
        which: _Side(
            which,  # type: ignore[arg-type]
            factory,
            inputs,
            [(m.routine_id, s) for m in members
             for s in (m.original if which == "source" else m.carried)],
            names,
        )
        for which, factory in (("source", source), ("target", target))
    }  # fmt: skip
    try:
        for side in sides.values():
            why = _open(side)
            if why is not None:
                return _all(batch, "could_not_compare", why[0], why[1])
        src, tgt = sides["source"].target, sides["target"].target
        assert src is not None and tgt is not None  # nosec B101 - opened above
        if src.engine != tgt.engine:
            return _all(batch, "could_not_compare", "engines_differ",
                        f"the source runs {src.engine} and the target {tgt.engine}")  # fmt: skip
        texts = [s.create_sql for side in sides.values() for _, s in side.statements]
        calls: dict[str, list[ProofCall]] = {}
        for call in inputs.calls:
            calls.setdefault(call.routine_id, []).append(call)
        shared = _Batchwide(
            sides=sides, calls=calls, skipped={s.routine_id: s.reason for s in inputs.skipped},
            keys=keys, guard=ValueGuard.for_texts(texts, dialect, _generated_values(inputs)),
            index=TableIndex({str(t["table_id"]): t for t in tables}), observed=observed,
            unseeded={str(t): str(w) for t, w in inputs.setup.unseeded},
            seed=seed, max_differences=max_differences,
            names={"source": src.description, "target": tgt.description}, dialect=dialect,
            random_seeded=src.random_seeded and tgt.random_seeded and src.version == tgt.version,
            named_time_zones=src.named_time_zones and tgt.named_time_zones,
            reached=lambda r: _reached(r, members, by_id, order, dialect),
        )  # fmt: skip
        out: dict[str, RoutineComparison] = {}
        for routine in batch:
            try:
                out[routine.routine_id] = _with_notes(_compare_one(routine, shared), sides)
            except Exception as exc:  # noqa: BLE001 - one routine never stops the others
                out[routine.routine_id] = RoutineComparison(
                    routine_id=routine.routine_id, outcome="could_not_compare",
                    reason_code="engine_failed", reason=f"the comparison failed ({type(exc).__name__})",
                    source=shared.names["source"], target=shared.names["target"],
                )  # fmt: skip
        return out
    finally:
        for side in sides.values():
            side.close()


def _with_notes(result: RoutineComparison, sides: Mapping[str, _Side]) -> RoutineComparison:
    """A routine with a difference, or not compared, says what a side could not freeze."""
    if result.outcome == "no_differences_found":
        return result
    notes = sorted({n for s in sides.values() if s.target for n in s.target.notes})
    if not notes:
        return result
    return result.model_copy(update={"reason": result.reason + "; note: " + "; ".join(notes)})


def _load_problem(routine: ComparedRoutine, shared: _Batchwide) -> RoutineComparison | None:
    rid = routine.routine_id
    src, tgt = shared.sides["source"], shared.sides["target"]
    names = shared.names
    base: dict[str, Any] = {"routine_id": rid, "source": names["source"], "target": names["target"]}
    for side, what in ((src, "original"), (tgt, "carried")):
        if rid in side.refused:
            return RoutineComparison(
                **base, outcome="could_not_compare", reason_code="outside_namespace",
                reason=f"the {what} routine was not created on {names[side.which]}: "
                f"{side.refused[rid]}",
            )  # fmt: skip
    if rid in src.failed:
        state = src.failed[rid]
        return RoutineComparison(
            **base, outcome="could_not_compare", reason_code="original_does_not_load",
            reason=f"the original does not load on {names['source']}: SQLSTATE {state} "
            f"({_class_name(state)})",
        )  # fmt: skip
    for dep in routine.depends_on:
        for side in (src, tgt):
            if dep in side.failed or dep in side.refused:
                return RoutineComparison(
                    **base, outcome="could_not_compare", reason_code="dependency_not_compared",
                    reason=f"it depends on {dep}, which does not load on the {side.which} side",
                )  # fmt: skip
    if rid in tgt.failed:
        state = tgt.failed[rid]
        diff = Difference(aspect="load", source="loads", target=f"SQLSTATE {state}")
        return RoutineComparison(
            **base, outcome="different", reason_code="carried_does_not_load",
            reason=f"the carried routine does not load on {names['target']}: SQLSTATE "
            f"{state} ({_class_name(state)})", differences=(diff,), differences_total=1,
        )  # fmt: skip
    return None


def _compare_one(routine: ComparedRoutine, shared: _Batchwide) -> RoutineComparison:
    rid = routine.routine_id
    sides = shared.sides
    base: dict[str, Any] = {"routine_id": rid, "source": shared.names["source"],
                            "target": shared.names["target"]}  # fmt: skip

    def not_compared(
        code: ReasonCode, reason: str, compared: int = 0, ok: int = 0
    ) -> RoutineComparison:
        return RoutineComparison(**base, outcome="could_not_compare", reason_code=code,
                                 reason=reason, calls_compared=compared, calls_succeeded=ok)  # fmt: skip

    for side in sides.values():
        if side.target is None:
            side.open()  # failed earlier in its batch: a ProofTargetError fails this routine
    problem = _load_problem(routine, shared)
    if problem is not None:
        return problem
    # a table the setup created without rows first, listed or only named in the text
    why = _unseeded(routine, shared.index, shared.unseeded, shared.observed, shared.dialect)
    if why is not None:
        return not_compared("table_unseeded", why)
    why = _unobserved(routine, shared.index, shared.observed, shared.dialect)
    if why is not None:
        return not_compared("table_not_observed", why)
    texts = [s.create_sql for m in shared.reached(routine) for s in (*m.original, *m.carried)]
    if dynamic_sql(texts, shared.dialect):
        return not_compared("dynamic_sql", "it runs a statement built at run time, which the "
                            "screen cannot read")  # fmt: skip
    unfrozen = nondeterministic_calls(texts, shared.dialect, random_seeded=shared.random_seeded)
    if unfrozen:
        return not_compared("non_deterministic", "it calls what the comparison cannot freeze "
                            f"alike on both sides ({', '.join(unfrozen)})")  # fmt: skip
    if not shared.named_time_zones and named_time_zones(texts, shared.dialect):
        return not_compared("named_time_zone", "it names a time zone, and an engine here has "
                            "no time zone tables, so the zone reads as NULL on both sides")  # fmt: skip
    calls = shared.calls.get(rid, [])
    if not calls:
        skipped = shared.skipped.get(rid, "no call or row change was generated for it")
        return not_compared("no_calls", f"no call to compare: {skipped}")
    differences: list[Difference] = []
    differing = succeeded = errored = 0
    unread_session = False
    for index, call in enumerate(calls, start=1):
        done = index - 1
        try:
            runs = {
                which: _run_side(side, call, index, shared.seed) for which, side in sides.items()
            }
        except ValueError:
            return not_compared("call_refused", f"call {index} ({call.label}) was refused "
                                "before it ran", done, succeeded)  # fmt: skip
        except ProofTargetError as exc:
            state = f" (SQLSTATE {exc.sqlstate})" if exc.sqlstate else ""
            return not_compared("engine_failed", f"call {index} ({call.label}) failed: "
                                f"{exc}{state}", done, succeeded)  # fmt: skip
        results = [r.result for r in runs.values()]
        if any("refcursor" in t for r in results for t in r.column_types):
            return not_compared("refcursor", f"call {index} ({call.label}) returned a cursor, "
                                "whose rows are not fetched or compared", done, succeeded)  # fmt: skip
        if any(r.session is None for r in runs.values()):
            unread_session = True
        if any(r.timed_out or r.sqlstate in _TIMEOUTS for r in results):
            for side in sides.values():
                side.open()  # an interrupted call may leave its side anywhere
            return not_compared("timeout", f"call {index} ({call.label}) ran past its time limit "
                                "on at least one side, which says nothing about its behaviour",
                                done, succeeded)  # fmt: skip
        if any(r.sqlstate in _UNDEFINED_TABLE for r in results):
            return not_compared("table_not_observed", f"call {index} ({call.label}) read a table "
                                "the setup does not have", done, succeeded)  # fmt: skip
        if any(r.error is not None and not r.sqlstate for r in results):
            return not_compared("harness_error", f"call {index} ({call.label}) failed with no "
                                "SQLSTATE, so the failure is not the routine's", done, succeeded)  # fmt: skip
        if any((r.sqlstate or "").startswith(_TRANSACTION_CONTROL) for r in results):
            return not_compared("transaction_control", "it ends the transaction itself "
                                "(SQLSTATE class 2D), so a call cannot be rolled back to the "
                                "seeded rows", done, succeeded)  # fmt: skip
        ended = [which for which, r in runs.items() if r.committed]
        for which in ended:
            sides[which].open()  # its transaction, and maybe its rows, are not as seeded
        if len(ended) == 2:
            code: ReasonCode = (
                "commits" if all(r.transaction == "committed" for r in runs.values())
                else "transaction_ended"
            )  # fmt: skip
            return not_compared(code, f"call {index} ({call.label}) ended the caller's "
                                "transaction on both sides, so it cannot be rolled back to "
                                "the seeded rows", done, succeeded)  # fmt: skip
        if all(r.error is None for r in results):
            succeeded += 1
        elif all(r.error is not None for r in results):
            errored += 1
        found = compare_call(
            runs["source"], runs["target"], call=call.label, index=index,
            tables=sides["source"].tables, keys=shared.keys, guard=shared.guard,
        )  # fmt: skip
        if found:
            differing += 1
            differences.extend(found)
    n = len(calls)
    if differences:
        return RoutineComparison(
            **base, outcome="different", calls_compared=n, calls_succeeded=succeeded,
            reason=f"behaves differently on {differing} of {_calls(n)}",
            differences=tuple(differences[: shared.max_differences]),
            differences_total=len(differences),
        )  # fmt: skip
    if succeeded == 0:
        return not_compared("no_successful_call", f"every one of its {_calls(n)} failed on both "
                            "sides, so no behaviour was compared", n, 0)  # fmt: skip
    if errored * 2 > n:
        return not_compared("mostly_errors", f"{errored} of its {_calls(n)} failed on both "
                            "sides, so the match rests mostly on errors", n, succeeded)  # fmt: skip
    limits = COVERAGE_LIMITS + _routine_limits(texts, shared.dialect, unread_session)
    return RoutineComparison(**base, outcome="no_differences_found", calls_compared=n,
                             calls_succeeded=succeeded, coverage_limits=limits,
                             reason=f"no differences found on {_calls(n)} ({succeeded} "
                             "succeeded on both sides)")  # fmt: skip


_PG_CLOCK_WORDS = {"now", "current_date", "current_timestamp", "localtimestamp",
                   "current_time", "localtime", "transaction_timestamp"}  # fmt: skip


def _routine_limits(texts: Sequence[str], dialect: str, unread_session: bool) -> tuple[str, ...]:
    """What else this routine's result does not cover."""
    out: list[str] = []
    if dialect == "postgresql":
        words: set[str] = set()
        for text in texts:
            with contextlib.suppress(RoutineTokenError):
                words |= {t.text.casefold() for t in routine_tokens(text, dialect)}
                for tok in routine_tokens(text, dialect):
                    if tok.kind.endswith("STRING"):
                        with contextlib.suppress(RoutineTokenError):
                            words |= {t.text.casefold() for t in routine_tokens(tok.text, dialect)}
        if words & _PG_CLOCK_WORDS:
            out.append(
                "PostgreSQL clock-based dates and times (now(), current_date, localtimestamp) "
                "were compared at the real time of each run, not a frozen one: a date that "
                "turns over at another offset or zone shows only when a run crosses that "
                "boundary by chance"
            )
    if unread_session:
        out.append("the session settings and user variables a call leaves were not read on "
                   "this engine, so they were not compared")  # fmt: skip
    return tuple(out)


def _run_side(side: _Side, call: ProofCall, index: int, seed: int) -> CallRun:
    """One side's run of a call. When the side's rows before the call are not its seeded
    rows, or the side failed, it is started again once."""
    try:
        return side.run(call, index, seed)
    except (_Leaked, ProofTargetError):
        try:
            side.open()  # a ProofTargetError here fails this routine only
        except ProofTargetUnavailable as exc:
            raise ProofTargetError(f"the {side.which} side could not be started again") from exc
        try:
            return side.run(call, index, seed)
        except _Leaked as exc:
            raise ProofTargetError(f"the {exc} side could not be reset to its seeded rows") from exc


def compare_routines(
    routines: Sequence[ComparedRoutine],
    tables: Sequence[Mapping[str, Any]],
    *,
    source: TargetFactory,
    target: TargetFactory,
    dialect: Literal["postgresql", "mysql"],
    seed: int = 0,
    batch_size: int = BATCH_SIZE,
    extensions: Collection[str] = (),
    max_differences: int = MAX_DIFFERENCES,
    on_batch: Callable[[int, int], None] | None = None,
) -> ComparisonReport:
    """Compare every routine's original (on ``source``) with its carried version (on
    ``target``), in batches (:func:`plan_batches`), each on targets the factories open
    fresh.

    ``tables`` are the collection's table objects (``database_schema.tables``), which the
    synthetic rows are shaped from; ``extensions`` names the PostgreSQL extensions both
    engines have. ``on_batch(done, total)`` is told after each batch. Routine ids must be
    unique; routines are created in the order given.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    order = {r.routine_id: i for i, r in enumerate(routines)}
    if len(order) != len(routines):
        raise ValueError("routine ids must be unique")
    by_id = {r.routine_id: r for r in routines}
    results: dict[str, RoutineComparison] = {}
    batches = plan_batches(routines, batch_size=batch_size, dialect=dialect)
    for done, batch in enumerate(batches, start=1):
        try:
            results.update(
                _compare_batch(
                    batch, by_id, order, tables, source, target, dialect=dialect, seed=seed,
                    extensions=extensions, max_differences=max_differences,
                )
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - one batch never stops the others
            results.update(
                _all(batch, "could_not_compare", "engine_failed",
                     f"the batch failed ({type(exc).__name__})")  # fmt: skip
            )
        if on_batch is not None:
            on_batch(done, len(batches))
    return ComparisonReport(results=tuple(results[r.routine_id] for r in routines))
