# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement's call rate over the window its execution count covers (#1893).

Every collector reads a cumulative counter: SQL Server's plan cache
(``sys.dm_exec_query_stats``), MySQL's and MariaDB's ``performance_schema`` digests,
PostgreSQL's ``pg_stat_statements`` and Oracle's ``V$SQL``. A count covers the time since
the statement was first cached or the counters were last reset or the server restarted,
which is rarely one day. This module is the one place a count becomes a rate, for the
offline parser and every live collector. The window, in order:

1. ``observed``: the statement's own window. SQL Server ``creation_time`` to
   ``last_execution_time``, MySQL and MariaDB ``FIRST_SEEN`` to ``LAST_SEEN`` and Oracle
   ``FIRST_LOAD_TIME`` to ``LAST_ACTIVE_TIME`` give the time the statement was active;
   PostgreSQL 17's ``pg_stat_statements.stats_since`` gives the time since its entry
   started counting (to the collection, as ``observed_seconds``: the view has no
   last-execution time). Used when it is at least :data:`MIN_WINDOW_SECONDS` long.
2. The server window (:func:`server_window`): ``stats_reset``, the time since
   ``pg_stat_statements`` was last reset (``pg_stat_statements_info.stats_reset``,
   PostgreSQL 14+), or ``uptime``, the server's uptime for every other engine, whose
   statement counters empty on a restart. PostgreSQL never uses its uptime: with
   ``pg_stat_statements.save`` on (the default) the counters survive a clean restart, so
   the uptime can be far shorter than the window the counts cover.
3. ``assumed_24h``: 24 hours, the figure every collector used before #1893.

Above all three, ``sampled`` (#1908): a collection reads the counters, waits the sampling
interval (``CollectionOptions.rate_sample_seconds``, the scripts' documented variable; 300
seconds by default), and reads them again. A statement's rate is then the change in its
count over the time between the two reads, by the database's own clock: the rate during
the load the collection ran in, with no idle hours in it. See :func:`rate_sample` and
:func:`statement_rate_fields` for the cases, each recorded in the statement's
``rate_sample``:

- ``both_snapshots``: in both reads, the count went up: ``sampled``.
- ``second_snapshot_only``: not in the first read (cached, or re-cached, in between),
  so every call it counts ran in the interval: ``sampled``, the count over the interval.
- ``no_calls_in_interval``: the same count twice: fewer than one call in the interval.
  Its rate is the lower of its single-snapshot rate and one call over the interval
  (maintainer decision on #1980): never 0, so it still counts for sizing and costs, and
  never above a statement that made one call (basis ``sampled`` when the one call over
  the interval is the lower, else the single snapshot's basis).
- ``count_decreased``: the count went down, so its counter was reset in between: the
  single-snapshot rate.
- ``counters_reset``: the server's own counters were reset inside the interval (its
  uptime, or PostgreSQL's time since the statistics reset, is shorter than it): every
  statement keeps its single-snapshot rate. After a restart that rate rests on windows
  that all lie inside the interval; when they are all under 60 seconds it falls back to
  24 hours and understates the load. PostgreSQL 12 and 13 have no
  ``pg_stat_statements_info``, so a ``pg_stat_statements_reset()`` inside the interval is
  not seen there: its entries read as ``count_decreased``, or as too few calls once they
  climb past their first count.
- ``interval_too_short``: the measured interval is under :data:`MIN_SAMPLE_SECONDS` or
  under :data:`MIN_SAMPLE_SHARE` of the interval asked for (a ``statement_timeout`` that
  cancelled ``pg_sleep``, a ``KILL QUERY`` that ended ``DO SLEEP``, or a small setting):
  a count over a few seconds is a burst, not a rate, so the single-snapshot rate.
- ``snapshot_incomplete``: a live snapshot came back without all its entries (the
  remote command's output is capped, and the rows past the cap are lost), so the
  statement's change can't be told. A statement missing from the first snapshot counts
  as new only when its own entry started counting after the first read (its age, from
  ``FIRST_SEEN``, ``stats_since``, ``creation_time`` or ``FIRST_LOAD_TIME``), or, for
  an engine with no such time, when the first snapshot came back whole. Anything else,
  and every statement when the second snapshot was cut, keeps the single-snapshot rate.
- ``unavailable``: the interval was not measured (the server could not wait, a read
  failed, or its clock gave no forward elapsed time): the single-snapshot rate.

An interval of 0 takes one read, as before #1908, and records no ``rate_sample``.

The facts layer counts the statements of each basis and every deliverable states them
once, with the cost model's reading of the rate (:func:`rate_window_facts`): an observed
window is the time the statement was active, and the costs assume its rate holds around
the clock.

Why a 60-second minimum, and why it is not a cliff: the timestamps are whole seconds in
MySQL's and Oracle's output, so one second of rounding is under 2% of a 60-second window
but half of a 2-second one, and a window that short is a burst, not a rate (five
executions one second apart would read as 1.25 calls a second, over the cache overlay's 1
call/s floor). So an observed window under the minimum is not used on its own: the rate
moves linearly from the fallback window's rate (at 0 seconds, or with no usable window)
to the rate over the minimum (at 60 seconds), and the statement is counted under the
fallback's basis. 300 calls over 59 seconds then give about 4.9 calls a second, as 300
over 61 seconds do, where a plain fallback to a 3-day uptime would give 0.001.
``rate_window_seconds`` records the window the rate implies (``count / rate``).

Times: every source writes first/last seen in the server's local time with no zone, so
the subtraction does not depend on the zone, but a window across a daylight-saving
change is off by the hour it moved (a fall-back reads an hour short, a spring-forward an
hour long). A time that carries a zone is converted to UTC; a time without one is taken
as it is, so the two compare only when the zone-less one is UTC too. The PostgreSQL
windows are computed by the server itself (``now() - stats_since``,
``now() - stats_reset``) and have neither problem.

A rate is never negative or infinite: every window used is at least 60 seconds (10 for
the interval between two snapshots, :data:`MIN_SAMPLE_SECONDS`), and an
unreadable, negative or non-finite execution count counts as 0. A very old statement
(a huge window) gets the small rate its count gives over that window.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

SAMPLED = "sampled"
OBSERVED = "observed"
UPTIME = "uptime"
STATS_RESET = "stats_reset"
ASSUMED_24H = "assumed_24h"
RateWindowBasis = Literal["sampled", "observed", "uptime", "stats_reset", "assumed_24h"]
RATE_WINDOW_BASES: tuple[str, ...] = (SAMPLED, OBSERVED, UPTIME, STATS_RESET, ASSUMED_24H)

# What happened to a statement's two snapshots (#1908), in the statement's rate_sample.
BOTH_SNAPSHOTS = "both_snapshots"
SECOND_SNAPSHOT_ONLY = "second_snapshot_only"
NO_CALLS_IN_INTERVAL = "no_calls_in_interval"
COUNT_DECREASED = "count_decreased"
COUNTERS_RESET = "counters_reset"
INTERVAL_TOO_SHORT = "interval_too_short"
SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
UNAVAILABLE = "unavailable"
RateSampleStatus = Literal[
    "both_snapshots",
    "second_snapshot_only",
    "no_calls_in_interval",
    "count_decreased",
    "counters_reset",
    "interval_too_short",
    "snapshot_incomplete",
    "unavailable",
]
# The statuses that keep the single-snapshot rate, in the order the facts name them
# (no_calls_in_interval is rated at most one call over the interval, counted apart).
SAMPLE_FALLBACKS: tuple[str, ...] = (
    COUNT_DECREASED,
    COUNTERS_RESET,
    INTERVAL_TOO_SHORT,
    SNAPSHOT_INCOMPLETE,
    UNAVAILABLE,
)
# The shortest measured interval a rate is taken over (#1908 review): at least this many
# seconds, and at least this share of the interval asked for.
MIN_SAMPLE_SECONDS = 10.0
MIN_SAMPLE_SHARE = 0.5
# Seconds of slack when an entry's age is compared with the interval: MySQL's, SQL
# Server's and Oracle's entry times are whole seconds or milliseconds.
_AGE_SLACK_SECONDS = 2.0

MIN_WINDOW_SECONDS = 60.0
ASSUMED_WINDOW_SECONDS = 24 * 3600.0

WINDOW_KEY = "rate_window_seconds"
BASIS_KEY = "rate_window_basis"
SAMPLE_KEY = "rate_sample"


def parse_seen(value: object) -> datetime | None:
    """A collector's first_seen / last_seen as a naive datetime, or None.

    Reads ISO 8601 text with a space, ``T`` or ``/`` (Oracle's ``FIRST_LOAD_TIME``)
    between date and time, an optional ``Z`` or offset (converted to UTC, then dropped),
    SQL Server ``FOR JSON``'s form with the space missing (``2026-06-1706:58:18.890``) and
    a ``datetime``. A time without a zone is returned as it is (see the module docstring).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if len(text) >= 11 and text[10] == "/":
            text = text[:10] + " " + text[11:]
        elif len(text) >= 12 and text[4] == "-" and text[7] == "-" and text[10].isdigit():
            text = text[:10] + " " + text[10:]
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


@dataclass(frozen=True)
class ServerWindow:
    """The seconds a server's statement counters cover, and what they are."""

    seconds: float
    basis: str


def server_window(
    *,
    uptime_seconds: object = None,
    uptime_hours: object = None,
    stats_reset_seconds: object = None,
) -> ServerWindow | None:
    """The window a server's cumulative statement counters cover, or None.

    ``stats_reset_seconds`` (PostgreSQL's time since ``pg_stat_statements`` was reset)
    wins when given; else the uptime, in seconds (MySQL's and MariaDB's ``Uptime``, the
    SQL Server and Oracle scripts' ``os_stats.db_uptime_seconds`` and live reads) or, from
    an older collection, in hours (``os_stats.db_uptime_hours``). A PostgreSQL caller
    passes only the reset age (see the module docstring). A missing, non-numeric, zero,
    negative or non-finite figure is ignored.
    """
    reset = _positive(stats_reset_seconds)
    if reset is not None:
        return ServerWindow(reset, STATS_RESET)
    seconds = _positive(uptime_seconds)
    if seconds is None:
        hours = _positive(uptime_hours)
        seconds = hours * 3600 if hours is not None else None
    return ServerWindow(seconds, UPTIME) if seconds is not None else None


def first_row_figure(rows: object, key: str) -> object:
    """``key`` (in any case) of the first row a live collector's query returned, or None.

    For the live collectors' uptime reads: a driver may return Oracle's column names in
    upper case, and an empty or unexpected answer is no figure."""
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    for name in (key, key.upper(), key.lower()):
        if name in row:
            return row[name]
    return None


@dataclass(frozen=True)
class RateWindow:
    """The seconds a statement's execution count covers, and how they were chosen."""

    seconds: float
    basis: str

    def fields(self, execution_count: object) -> dict[str, Any]:
        """The collector output's rate fields for ``execution_count`` over this window:
        ``frequency_per_hour``, ``calls_per_second``, ``rate_window_seconds`` and
        ``rate_window_basis``."""
        count = _count(execution_count)
        return {
            "frequency_per_hour": count / (self.seconds / 3600),
            "calls_per_second": count / self.seconds,
            WINDOW_KEY: round(self.seconds, 3),
            BASIS_KEY: self.basis,
        }


def _count(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int):
        return float(max(value, 0))
    number = _number(value)
    return number if number is not None and number > 0 else 0.0


ServerArg = ServerWindow | None | Callable[[], ServerWindow | None]


def _observed_seconds(
    first_seen: object, last_seen: object, observed_seconds: object
) -> float | None:
    given = _number(observed_seconds)
    if given is not None:
        return given
    first, last = parse_seen(first_seen), parse_seen(last_seen)
    if first is None or last is None:
        return None
    return (last - first).total_seconds()


def rate_window(
    first_seen: object,
    last_seen: object,
    server: ServerArg,
    *,
    observed_seconds: object = None,
) -> RateWindow:
    """The window a statement's count covers (see the module docstring for the order).

    ``observed_seconds``, when given, is the statement's own window in seconds as the
    server computed it (PostgreSQL 17's ``now() - stats_since``), in place of
    ``first_seen``/``last_seen``. ``server`` is the server window (:func:`server_window`),
    or a function of no arguments that returns it: a live collector passes its uptime
    read, called only for a statement with no full window of its own (and cached by the
    collector), so a collection whose every statement has one makes no extra query."""
    observed = _observed_seconds(first_seen, last_seen, observed_seconds)
    if observed is not None and observed >= MIN_WINDOW_SECONDS:
        return RateWindow(observed, OBSERVED)
    resolved = server() if callable(server) else server
    if resolved is not None and resolved.seconds >= MIN_WINDOW_SECONDS:
        fallback = RateWindow(resolved.seconds, resolved.basis)
    else:
        fallback = RateWindow(ASSUMED_WINDOW_SECONDS, ASSUMED_24H)
    if observed is None or observed <= 0:
        return fallback
    # Under the minimum: the rate moves linearly from the fallback's (0 s) to the
    # minimum's (60 s), so a window just under 60 s reads as one just over it.
    share = observed / MIN_WINDOW_SECONDS
    per_call = share / MIN_WINDOW_SECONDS + (1 - share) / fallback.seconds
    return RateWindow(1 / per_call, fallback.basis)


@dataclass(frozen=True)
class RateSample:
    """A collection's two snapshots (#1908): the interval asked for, the seconds the
    database's clock measured between the two reads, and what that allows (``off``,
    ``sampled``, ``unavailable``, ``interval_too_short`` or ``counters_reset``; see the
    module docstring)."""

    seconds: int
    elapsed_seconds: float | None
    outcome: str


def rate_sample(seconds: object, elapsed_seconds: object, server: ServerArg) -> RateSample:
    """What a collection's two snapshots allow.

    ``seconds`` is the interval asked for: 0 (or none, or not a positive number) is
    ``off``, a single snapshot. ``elapsed_seconds`` is the time between the two reads by
    the database's clock: missing, zero, negative or not finite is ``unavailable``, and
    under :data:`MIN_SAMPLE_SECONDS` or :data:`MIN_SAMPLE_SHARE` of ``seconds`` is
    ``interval_too_short``. ``server`` is the server window (:func:`server_window`, or a
    function that returns it) read with the second snapshot: one shorter than the
    interval means the counters were reset, or the server restarted, inside it
    (``counters_reset``)."""
    requested = _positive(seconds)
    whole = int(requested) if requested is not None else 0
    if whole <= 0:
        return RateSample(0, None, "off")
    elapsed = _positive(elapsed_seconds)
    if elapsed is None:
        return RateSample(whole, None, UNAVAILABLE)
    if elapsed < max(MIN_SAMPLE_SECONDS, MIN_SAMPLE_SHARE * whole):
        return RateSample(whole, round(elapsed, 3), INTERVAL_TOO_SHORT)
    resolved = server() if callable(server) else server
    if resolved is not None and resolved.seconds < elapsed:
        return RateSample(whole, round(elapsed, 3), COUNTERS_RESET)
    return RateSample(whole, round(elapsed, 3), SAMPLED)


def _whole_count(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def statement_rate_fields(
    execution_count: object,
    first_seen: object,
    last_seen: object,
    server: ServerArg,
    *,
    observed_seconds: object = None,
    sample: RateSample | None = None,
    first_count: object = None,
    second_count: object = None,
    unmeasured: bool = False,
    incomplete: bool = False,
) -> dict[str, Any]:
    """A statement's rate fields: :meth:`RateWindow.fields` over :func:`rate_window`,
    or, when ``sample`` was taken, over the interval between its two snapshots (#1908).

    ``first_count`` is the statement's count in the first snapshot (None: it was not
    there); ``second_count`` its count in the second, which is ``execution_count`` when
    the second snapshot is the statement query itself (the collection scripts). A live
    collector passes ``unmeasured`` for a statement its second snapshot did not hold, and
    ``incomplete`` for one whose change a cut snapshot hides (``snapshot_incomplete``).
    With no sample, or one that is ``off``, the fields are the single snapshot's, bit for
    bit; otherwise they add ``rate_sample``: the status, both counts and the elapsed
    seconds (see the module docstring for each status)."""
    window = rate_window(first_seen, last_seen, server, observed_seconds=observed_seconds)
    single = window.fields(execution_count)
    if sample is None or sample.outcome == "off":
        return single
    if sample.outcome == UNAVAILABLE or unmeasured or sample.elapsed_seconds is None:
        return single | {SAMPLE_KEY: _sample_record(UNAVAILABLE, None, None, None)}
    first = _whole_count(first_count)
    second = _whole_count(second_count if second_count is not None else execution_count)
    second = second if second is not None else 0
    elapsed = sample.elapsed_seconds
    if sample.outcome in (COUNTERS_RESET, INTERVAL_TOO_SHORT):
        return single | {SAMPLE_KEY: _sample_record(sample.outcome, first, second, elapsed)}
    if incomplete:
        return single | {SAMPLE_KEY: _sample_record(SNAPSHOT_INCOMPLETE, first, second, elapsed)}
    if first is None:
        status, calls = SECOND_SNAPSHOT_ONLY, second
    elif second < first:
        status, calls = COUNT_DECREASED, None
    elif second == first:
        # Fewer than one call in the interval: at most one call over it, and never above
        # the single-snapshot rate (maintainer decision on #1980).
        record = {SAMPLE_KEY: _sample_record(NO_CALLS_IN_INTERVAL, first, second, elapsed)}
        bound = RateWindow(elapsed, SAMPLED).fields(1)
        lower = single if single["calls_per_second"] <= bound["calls_per_second"] else bound
        return lower | record
    else:
        status, calls = BOTH_SNAPSHOTS, second - first
    record = {SAMPLE_KEY: _sample_record(status, first, second, elapsed)}
    if calls is None:
        return single | record
    return RateWindow(elapsed, SAMPLED).fields(calls) | record


def _sample_record(
    status: str, first: int | None, second: int | None, elapsed: float | None
) -> dict[str, Any]:
    return {
        "status": status,
        "first_count": first,
        "second_count": second,
        "elapsed_seconds": elapsed,
    }


def _clock_seconds(value: object) -> float | None:
    """A snapshot's database clock as seconds: an epoch number, or an ISO 8601 time
    (:func:`parse_seen`). None when it can't be read."""
    number = _number(value)
    if number is not None:
        return number
    parsed = parse_seen(value)
    if parsed is None:
        return None
    return (parsed - datetime(1970, 1, 1)).total_seconds()


@dataclass(frozen=True)
class _Entry:
    group: str
    count: int
    age_seconds: float | None


@dataclass(frozen=True)
class _Snapshot:
    clock: float | None
    complete: bool
    entries: dict[str, _Entry] = field(default_factory=dict)


def _lowered(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _read_snapshot(read: Callable[[], object]) -> _Snapshot | None:
    """One snapshot query's rows: first a row with ``sampled_at`` (the database's clock)
    and ``entry_count`` (how many entry rows the query returned), then one row per cached
    entry with its ``group_key`` (the statement's id), ``entry_key`` (the entry within
    it, with what identifies a re-created one), ``execution_count`` and
    ``entry_age_seconds`` (how long the entry had been counting, when the engine says).
    ``complete`` is false when fewer entry rows came back than the server counted (the
    remote command's output is capped). None when the read failed."""
    try:
        rows = read()
    except Exception:  # noqa: BLE001 -- a failed read leaves the interval unmeasured
        return None
    if not isinstance(rows, list):
        return None
    clock = None
    expected: int | None = None
    entries: dict[str, _Entry] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = _lowered(raw)
        if row.get("sampled_at") is not None and clock is None:
            clock = _clock_seconds(row["sampled_at"])
            expected = _whole_count(row.get("entry_count"))
        group, entry = row.get("group_key"), row.get("entry_key")
        count = _whole_count(row.get("execution_count"))
        if group is None or entry is None or count is None:
            continue
        age = _number(row.get("entry_age_seconds"))
        entries[str(entry)] = _Entry(str(group), count, age if age is None or age >= 0 else None)
    complete = expected is not None and len(entries) >= expected
    return _Snapshot(clock, complete, entries)


@dataclass(frozen=True)
class LiveSample:
    """A live collector's two snapshots (#1908): the database clock's elapsed seconds
    and, per statement id, its count in each (the first only over the entries both
    snapshots hold, so an entry evicted in between takes nothing away). ``incomplete``
    holds the statements whose change a cut snapshot hides, and ``second_complete`` is
    false when the second snapshot was cut (every statement is then incomplete)."""

    elapsed_seconds: float | None
    seconds: int
    first: Mapping[str, int] = field(default_factory=dict)
    second: Mapping[str, int] = field(default_factory=dict)
    incomplete: frozenset[str] = frozenset()
    second_complete: bool = True

    def sample(self, server: ServerArg) -> RateSample:
        """The :class:`RateSample`, checked against the server window ``server``."""
        return rate_sample(self.seconds, self.elapsed_seconds, server)

    def counts(self, group_key: object) -> dict[str, Any]:
        """The keyword arguments :func:`statement_rate_fields` takes for one statement."""
        key = str(group_key)
        if not self.second_complete:
            return {"incomplete": True}
        if key not in self.second:
            return {"unmeasured": True}
        counts: dict[str, Any] = {
            "first_count": self.first.get(key),
            "second_count": self.second[key],
        }
        if key in self.incomplete:
            counts["incomplete"] = True
        return counts


def rate_sample_setting(value: object) -> int:
    """A sampling interval as a collector uses it: a whole number of seconds, 0 or more.

    Raises:
        ValueError: it is not one (a bool is not one).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"the sampling interval must be a whole number of seconds, 0 or more, got {value!r}"
        )
    return int(value)


def sample_arguments(
    live: LiveSample | None, sample: RateSample | None, group_key: object
) -> dict[str, Any]:
    """The :func:`statement_rate_fields` keyword arguments for one live statement: none
    with no sample (a single snapshot), else the sample and the statement's counts."""
    if live is None or sample is None:
        return {}
    return {"sample": sample, **live.counts(group_key)}


def take_live_sample[T](
    read_snapshot: Callable[[], object],
    seconds: object,
    collect: Callable[[], T],
    *,
    sleep: Callable[[float], object] | None = None,
) -> tuple[T, LiveSample | None]:
    """Run a live collector's statement query between two snapshots (#1908).

    Reads the first snapshot, waits ``seconds``, runs ``collect`` (the statement query)
    and reads the second, so every statement the query returns is in the second (bar
    an eviction in the moment between). Returns ``collect``'s result and the
    :class:`LiveSample`; with ``seconds`` 0 it reads no snapshot, never waits, and the
    sample is None. ``sleep`` waits (``time.sleep`` when None). Both reads are read-only
    ``SELECT`` statements; nothing on the server is changed or reset.

    An entry missing from the first snapshot is new only when its age at the second
    read is within the interval (it started counting after the first read), or, with no
    age, when the first snapshot came back whole; otherwise its statement is
    ``incomplete``. A cut snapshot is logged."""
    requested = _positive(seconds)
    whole = int(requested) if requested is not None else 0
    if whole <= 0:
        return collect(), None
    first = _read_snapshot(read_snapshot)
    (sleep or time.sleep)(whole)
    result = collect()
    second = _read_snapshot(read_snapshot)
    if first is None or second is None or first.clock is None or second.clock is None:
        return result, LiveSample(None, whole)
    elapsed = second.clock - first.clock
    for name, snap in (("first", first), ("second", second)):
        if not snap.complete:
            logger.warning(
                "rate sample: the %s snapshot came back with %d entries, not all of them: "
                "the statements it hides keep their single-snapshot rates",
                name,
                len(snap.entries),
            )
    first_counts: dict[str, int] = {}
    second_counts: dict[str, int] = {}
    incomplete: set[str] = set()
    for entry, now in second.entries.items():
        group = now.group
        second_counts[group] = second_counts.get(group, 0) + now.count
        before = first.entries.get(entry)
        if before is not None and before.group == group:
            first_counts[group] = first_counts.get(group, 0) + before.count
        elif not _new_since_first(now, first.complete, elapsed):
            incomplete.add(group)
    return result, LiveSample(
        elapsed,
        whole,
        first_counts,
        second_counts,
        frozenset(incomplete),
        second.complete,
    )


def _new_since_first(entry: _Entry, first_complete: bool, elapsed: float) -> bool:
    """Whether an entry missing from the first snapshot started counting after it: its
    age at the second read is within the interval, or, with no age, the first snapshot
    came back whole (so its absence means it was not there)."""
    if entry.age_seconds is not None:
        return entry.age_seconds <= elapsed + _AGE_SLACK_SECONDS
    return first_complete


_PHRASES = {
    SAMPLED: "the time between two snapshots of the counters",
    OBSERVED: "each statement's own observed window",
    UPTIME: "the server uptime",
    STATS_RESET: "the time since the statement statistics were reset",
    ASSUMED_24H: "an assumed 24 hours",
}
_FALLBACK_PHRASES = {
    COUNT_DECREASED: "whose count went down",
    COUNTERS_RESET: "after a counter reset between the snapshots",
    INTERVAL_TOO_SHORT: "whose measured interval was too short",
    SNAPSHOT_INCOMPLETE: "missing from a snapshot that came back incomplete",
    UNAVAILABLE: "whose interval was not measured",
}


def _counted(n: int) -> str:
    return f"{n} statement{'' if n == 1 else 's'}"


def rate_window_facts(query_patterns: Iterable[Any]) -> dict[str, Any]:
    """How many collected statements' rates used each window basis (``RateWindowFacts``).

    ``unrecorded_count`` is the statements with no basis recorded (a collector output
    written before #1893, whose rates assumed 24 hours, or a Performance Insights
    statement, which reports its own rate). ``sample_no_calls_count`` is the sampled
    statements with no calls between the snapshots, and ``sample_fallback_count`` the ones
    of a sampled collection (#1908) that kept a single-snapshot rate. ``statement`` is the
    one sentence every deliverable prints after "Call rates:" whenever the collection
    has statements: the count of each basis (observed, uptime and the 24-hour assumption
    always; the stats reset when any statement used it; the two snapshots whenever the
    collection sampled), the statements that recorded none, why sampled statements kept
    a single-snapshot rate, and that the costs assume each rate holds continuously. None
    for a collection with no statements.
    """
    rows = [p for p in query_patterns if isinstance(p, Mapping)]
    counts = {basis: 0 for basis in RATE_WINDOW_BASES}
    fallbacks = {status: 0 for status in SAMPLE_FALLBACKS}
    no_calls = 0
    any_sample = False
    for row in rows:
        basis = row.get(BASIS_KEY)
        if basis in counts:
            counts[basis] += 1
        sample = row.get(SAMPLE_KEY)
        status = sample.get("status") if isinstance(sample, Mapping) else None
        if status is not None:
            any_sample = True
        if status in fallbacks:
            fallbacks[status] += 1
        elif status == NO_CALLS_IN_INTERVAL:
            no_calls += 1
    unrecorded = len(rows) - sum(counts.values())
    statement = None
    if rows:
        shown = [
            b
            for b in RATE_WINDOW_BASES
            if (b != STATS_RESET or counts[b]) and (b != SAMPLED or counts[b] or any_sample)
        ]
        parts = [
            f"{_PHRASES[b]} ({_counted(counts[b]) if i == 0 else counts[b]})"
            for i, b in enumerate(shown)
        ]
        listed = ", ".join(parts[:-1]) + ", or " + parts[-1]
        statement = f"execution counts are divided by {listed}"
        if unrecorded:
            statement += (
                f"; {_counted(unrecorded)} recorded no window (an older collector output, "
                "whose rates assumed 24 hours, or a Performance Insights rate)"
            )
        if no_calls:
            statement += (
                f"; {_counted(no_calls)} made no calls between the snapshots and "
                f"{'is' if no_calls == 1 else 'are'} rated at most one call over the interval"
            )
        fell_back = sum(fallbacks.values())
        if fell_back:
            reasons = ", ".join(
                f"{n} {_FALLBACK_PHRASES[status]}" for status, n in fallbacks.items() if n
            )
            statement += f"; {_counted(fell_back)} kept a single-snapshot rate ({reasons})"
        statement += "; costs assume that rate runs continuously"
    return {
        "statement_count": len(rows),
        "sampled_count": counts[SAMPLED],
        "observed_count": counts[OBSERVED],
        "uptime_count": counts[UPTIME],
        "stats_reset_count": counts[STATS_RESET],
        "assumed_24h_count": counts[ASSUMED_24H],
        "unrecorded_count": unrecorded,
        "sample_fallback_count": sum(fallbacks.values()),
        "sample_no_calls_count": no_calls,
        "statement": statement,
    }
