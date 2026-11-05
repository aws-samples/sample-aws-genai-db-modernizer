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

A rate is never negative or infinite: every window used is at least 60 seconds, and an
unreadable, negative or non-finite execution count counts as 0. A very old statement
(a huge window) gets the small rate its count gives over that window.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

OBSERVED = "observed"
UPTIME = "uptime"
STATS_RESET = "stats_reset"
ASSUMED_24H = "assumed_24h"
RateWindowBasis = Literal["observed", "uptime", "stats_reset", "assumed_24h"]
RATE_WINDOW_BASES: tuple[str, ...] = (OBSERVED, UPTIME, STATS_RESET, ASSUMED_24H)

MIN_WINDOW_SECONDS = 60.0
ASSUMED_WINDOW_SECONDS = 24 * 3600.0

WINDOW_KEY = "rate_window_seconds"
BASIS_KEY = "rate_window_basis"


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


def statement_rate_fields(
    execution_count: object,
    first_seen: object,
    last_seen: object,
    server: ServerArg,
    *,
    observed_seconds: object = None,
) -> dict[str, Any]:
    """A statement's rate fields: :meth:`RateWindow.fields` over :func:`rate_window`."""
    window = rate_window(first_seen, last_seen, server, observed_seconds=observed_seconds)
    return window.fields(execution_count)


_PHRASES = {
    OBSERVED: "each statement's own observed window",
    UPTIME: "the server uptime",
    STATS_RESET: "the time since the statement statistics were reset",
    ASSUMED_24H: "an assumed 24 hours",
}


def _counted(n: int) -> str:
    return f"{n} statement{'' if n == 1 else 's'}"


def rate_window_facts(query_patterns: Iterable[Any]) -> dict[str, Any]:
    """How many collected statements' rates used each window basis (``RateWindowFacts``).

    ``unrecorded_count`` is the statements with no basis recorded (a collector output
    written before #1893, whose rates assumed 24 hours, or a Performance Insights
    statement, which reports its own rate). ``statement`` is the one sentence every
    deliverable prints after "Call rates:" whenever the collection has statements: the
    count of each basis (observed, uptime and the 24-hour assumption always, the stats
    reset when any statement used it), the statements that recorded none, and that the
    costs assume each rate holds continuously. None for a collection with no statements.
    """
    rows = [p for p in query_patterns if isinstance(p, Mapping)]
    counts = {basis: 0 for basis in RATE_WINDOW_BASES}
    for row in rows:
        basis = row.get(BASIS_KEY)
        if basis in counts:
            counts[basis] += 1
    unrecorded = len(rows) - sum(counts.values())
    statement = None
    if rows:
        shown = [b for b in RATE_WINDOW_BASES if b != STATS_RESET or counts[b]]
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
        statement += "; costs assume that rate runs continuously"
    return {
        "statement_count": len(rows),
        "observed_count": counts[OBSERVED],
        "uptime_count": counts[UPTIME],
        "stats_reset_count": counts[STATS_RESET],
        "assumed_24h_count": counts[ASSUMED_24H],
        "unrecorded_count": unrecorded,
        "statement": statement,
    }
