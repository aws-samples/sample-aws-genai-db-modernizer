# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Raw trigger rows, read the same way by every collector path (#1651).

The scripts and live collectors spell a trigger's timing and events differently
('INSTEAD_OF', 'BEFORE EACH ROW', 'INSERT OR UPDATE', 'INSERT,UPDATE', one row per
event); these helpers read them once. No I/O apart from the skip warning; input order
is kept.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

EVENTS = ("INSERT", "UPDATE", "DELETE")


def _fold(value: object) -> str:
    return str(value or "").strip().casefold()


def trigger_events(raw: object) -> list[str]:
    """Every DML event in ``raw``, in INSERT, UPDATE, DELETE order; empty for none."""
    text = " ".join(str(r) for r in raw) if isinstance(raw, list | tuple) else str(raw or "")
    upper = text.upper()
    return [e for e in EVENTS if re.search(rf"\b{e}\b", upper)]


def trigger_timing(raw: object) -> str | None:
    """The contract timing for any source spelling, or None when it is not one."""
    text = re.sub(r"[\s_]+", " ", str(raw or "")).strip().upper()
    if text.startswith("INSTEAD OF"):
        return "INSTEAD OF"
    if text.startswith("BEFORE"):
        return "BEFORE"
    if text.startswith(("AFTER", "COMPOUND")):
        return "AFTER"
    return None


def dml_trigger(row: Mapping[str, Any]) -> tuple[list[str], str] | None:
    """A trigger row's events and contract timing, or None when the contract cannot
    hold it: no DML event (LOGON, DDL, TRUNCATE, ...), no known timing, or no table."""
    events = trigger_events(row.get("events") or row.get("event_type"))
    timing = trigger_timing(row.get("timing"))
    if not events or timing is None or not row.get("table_name"):
        return None
    return events, timing


_NAMED_LIMIT = 20


def trigger_label(row: Mapping[str, Any]) -> str:
    """``schema.table.trigger`` for a log line, leaving out the parts the row lacks."""
    parts = (row.get("schema_name"), row.get("table_name"), row.get("trigger_name"))
    return ".".join(str(p) for p in parts if p) or "?"


def log_skipped_triggers(logger: logging.Logger, skipped: Sequence[Mapping[str, Any]]) -> None:
    """The one warning every builder gives for the trigger rows it skipped, naming them."""
    if not skipped:
        return
    names = [trigger_label(r) for r in skipped[:_NAMED_LIMIT]]
    more = len(skipped) - len(names)
    logger.warning(
        "Skipped %d non-DML or DB-level trigger(s) (no INSERT/UPDATE/DELETE event, as for "
        "LOGON/DDL/STARTUP/DROP or TRUNCATE; unknown timing; or no table_name): %s%s",
        len(skipped),
        ", ".join(names),
        f" and {more} more" if more else "",
    )


def merge_trigger_rows(rows: object) -> list[dict[str, Any]]:
    """One row per trigger (schema, table, name), ``events`` the union of its rows'.

    PostgreSQL's ``information_schema.triggers`` gives one row per event; the first
    row's other fields are kept.
    """
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        key = (
            _fold(row.get("schema_name")),
            _fold(row.get("table_name")),
            _fold(row.get("trigger_name")),
        )
        events = trigger_events(row.get("events") or row.get("event_type"))
        if key in merged:
            have = set(merged[key]["events"]) | set(events)
            merged[key]["events"] = [e for e in EVENTS if e in have]
        else:
            merged[key] = {**row, "events": events}
    return list(merged.values())
