# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The source server's own counters, as a collection records them (#1991).

The rate window check (``src.shared.rate_window.window_check_facts``) compares the
window the statement counts cover with what the server reports about itself:

- ``uptime_seconds``: how long the server has run (MySQL and MariaDB ``Uptime``,
  PostgreSQL ``pg_postmaster_start_time()``, the SQL Server and Oracle scripts'
  ``os_stats.db_uptime_seconds``, or ``db_uptime_hours`` from an older collection). A
  window far shorter than the uptime means the counters were reset, or the statements
  evicted, well after the server started.
- ``stats_reset_seconds``: the time since PostgreSQL's ``pg_stat_statements`` was reset.
- ``oldest_entry_age_seconds`` (#2389): on PostgreSQL 17, the age of the database's
  oldest ``pg_stat_statements`` entry (``stats_since``). A reset scoped to one database
  does not move ``stats_reset``, and no count covers more time than its entry.
- ``scoped_reset_calls`` (#2389): PostgreSQL's calls of ``pg_stat_statements_reset`` with
  arguments among the server's statements, every database's: a full reset removes every
  entry, so each one ran since the last full reset, and before PostgreSQL 17 it is the
  one trace a reset scoped to a database, user or statement leaves.
- ``read_at``: when the counters were read (an offline script's ``collected_at``, a live
  collector's clock), which dates a window that has no first and last seen of its own.
- ``statements_since_start``: the server's own count of client statements since it
  started, MySQL's and MariaDB's ``Questions``. No digest reset touches it, so over an
  uptime of a week or more it is the source's own long-window rate. PostgreSQL's script
  writes the sum of ``pg_stat_statements`` calls under the same name: the very counters
  the rates come from, so it is not recorded; SQL Server's ``Batch Requests/sec`` counts
  batches, not statements, and is not either.

Every read is read-only (``SHOW GLOBAL STATUS`` and the engines' existing uptime reads);
nothing is reset or changed. A figure that is missing, not a number, zero or negative
is left out.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from src.shared.rate_window import STATS_RESET, UPTIME

logger = logging.getLogger(__name__)

#: The engines whose ``questions`` figure is the server's own statement count.
QUESTIONS_ENGINES = frozenset({"mysql", "mariadb"})


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip()) if isinstance(value, str) else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip()) if isinstance(value, str) else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 else None


def server_counters(
    *,
    uptime_seconds: object = None,
    stats_reset_seconds: object = None,
    read_at: object = None,
    statements_since_start: object = None,
    oldest_entry_age_seconds: object = None,
    scoped_reset_calls: object = None,
) -> dict[str, Any] | None:
    """The ``metadata.server_counters`` record, or None when nothing was read."""
    out = {
        "uptime_seconds": _positive(uptime_seconds),
        "stats_reset_seconds": _positive(stats_reset_seconds),
        "read_at": read_at if isinstance(read_at, (str, datetime)) and read_at else None,
        "statements_since_start": _count(statements_since_start),
    }
    # #2389: PostgreSQL's reset scope figures, recorded only when read, so every other
    # engine's record keeps its four keys.
    for key, value in (
        ("oldest_entry_age_seconds", _positive(oldest_entry_age_seconds)),
        ("scoped_reset_calls", _count(scoped_reset_calls)),
    ):
        if value is not None:
            out[key] = value
    return out if any(v is not None for v in out.values()) else None


def offline_server_counters(data: Mapping[str, Any], source_engine: str) -> dict[str, Any] | None:
    """The server counters an offline collection script wrote (see the module docstring)."""
    stats = data.get("global_stats") or {}
    os_stats = data.get("os_stats") or {}
    stats = stats if isinstance(stats, Mapping) else {}
    os_stats = os_stats if isinstance(os_stats, Mapping) else {}
    uptime = _positive(os_stats.get("db_uptime_seconds")) or _positive(stats.get("uptime"))
    if uptime is None:
        hours = _positive(os_stats.get("db_uptime_hours"))
        uptime = hours * 3600 if hours is not None else None
    return server_counters(
        uptime_seconds=uptime,
        stats_reset_seconds=stats.get("stats_reset_age_seconds"),
        read_at=data.get("collected_at"),
        statements_since_start=(
            stats.get("questions") if source_engine in QUESTIONS_ENGINES else None
        ),
        oldest_entry_age_seconds=(
            stats.get("oldest_entry_age_seconds") if source_engine == "postgresql" else None
        ),
        scoped_reset_calls=(
            stats.get("scoped_reset_calls") if source_engine == "postgresql" else None
        ),
    )


def live_server_counters(db: Any, *, read_at: object = None) -> dict[str, Any] | None:
    """The server counters a live collector reads: its server window (``stats_window``,
    the uptime or PostgreSQL's reset age), PostgreSQL's uptime (``server_uptime``) and
    reset scope (``reset_scope``, #2389) and, on MySQL and MariaDB, ``Questions``.
    ``read_at`` defaults to now (UTC). A read that fails is left out."""
    window = None
    try:
        window = db.stats_window()
    except Exception:  # noqa: BLE001 -- no server window: the check says so
        logger.warning("Failed to read the server's uptime")
    uptime = window.seconds if window is not None and window.basis == UPTIME else None
    if uptime is None and hasattr(db, "server_uptime"):
        # PostgreSQL: the window is the reset age; the uptime is a read of its own.
        try:
            uptime = db.server_uptime()
        except Exception:  # noqa: BLE001 -- no uptime: the check says so
            logger.warning("Failed to read the server's uptime")
    # #2389: PostgreSQL's reset scope figures, each a read of its own.
    scope: dict[str, Any] = {}
    if hasattr(db, "reset_scope"):
        try:
            scope = db.reset_scope()
        except Exception:  # noqa: BLE001 -- no reset scope: the check says so
            logger.warning("Failed to read the statement statistics' reset scope")
    questions = None
    if hasattr(db, "statements_since_start"):
        try:
            questions = db.statements_since_start()
        except Exception:  # noqa: BLE001 -- no statement count: the check says so
            logger.warning("Failed to read the server's statement count")
    return server_counters(
        uptime_seconds=uptime,
        # The reset age as the server records it (the window may be bounded below it).
        stats_reset_seconds=(
            scope["stats_reset_seconds"]
            if scope.get("stats_reset_seconds") is not None
            else window.seconds if window is not None and window.basis == STATS_RESET else None
        ),
        read_at=read_at or datetime.now(UTC).isoformat(),
        statements_since_start=questions,
        oldest_entry_age_seconds=scope.get("oldest_entry_age_seconds"),
        scoped_reset_calls=scope.get("scoped_reset_calls"),
    )


def server_counters_model(record: object) -> Any:
    """``record`` as the contract's ``ServerCounters``, or None when it is not one."""
    from pydantic import ValidationError

    from src.contracts.collector_output import ServerCounters

    if not isinstance(record, Mapping):
        return None
    try:
        return ServerCounters.model_validate(dict(record))
    except ValidationError:
        logger.warning("Dropped unreadable server counters: %r", dict(record))
        return None
