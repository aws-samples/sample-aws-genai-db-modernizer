# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Raw procedure and trigger rows, read the same way by every collector path (#1651,
#1652).

The scripts and live collectors spell a trigger's timing and events differently
('INSTEAD_OF', 'BEFORE EACH ROW', 'INSERT OR UPDATE', 'INSERT,UPDATE', one row per
event); these helpers read them once. They also read the routine sections the scripts
write since #1652: ``routine_stats`` (the execution counts the engine keeps, one row per
routine with ``routine_name``, ``schema_name``, ``object_type``, ``execution_count`` and
``total_time_ms``), ``routine_sources`` (Oracle bodies, one ``ALL_SOURCE`` line per row)
and ``routine_stats_status``. No I/O apart from the skip warning; input order is kept.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from src.contracts.collector_output import TriggerEnabledMode

EVENTS = ("INSERT", "UPDATE", "DELETE")
_STATS_STATUSES = ("tracked", "not_tracked", "offline")
_REASON_CAP = 500
# EXECUTE FUNCTION f() / EXECUTE PROCEDURE f(): the function a PostgreSQL trigger runs.
_FUNCTION_CALL = re.compile(
    r"EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+((?:\"(?:[^\"]|\"\")+\"|[\w$]+)"
    r"(?:\s*\.\s*(?:\"(?:[^\"]|\"\")+\"|[\w$]+))*)\s*\(",
    re.I,
)


def _fold(value: object) -> str:
    return str(value or "").strip().casefold()


def _schema_key(source_engine: str) -> Callable[[object], str]:
    """How the engine compares schema names: a PostgreSQL schema ``"Reporting"`` is not
    ``reporting``, so it matches exactly; MySQL, MariaDB and SQL Server may spell one
    schema two ways (``lower_case_table_names=2``), so they fold the case."""
    return (lambda v: str(v or "").strip()) if source_engine == "postgresql" else _fold


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


# An event a trigger outside the DML list fires on, as the row names it (#2023); Oracle's
# can be two words (ASSOCIATE STATISTICS).
_OTHER_EVENT = re.compile(r"(?=.{1,64}$)[A-Z][A-Z_]*(?: [A-Z_]+)*")


def other_trigger_events(row: Mapping[str, Any]) -> list[str]:
    """The events of a trigger the DML list cannot hold (#2023), upper case, in the
    row's order: TRUNCATE (PostgreSQL), LOGON, STARTUP, DDL, CREATE, ... (Oracle). Empty
    for a trigger on any INSERT, UPDATE or DELETE, which stays in the DML list (or is
    skipped there, for an unknown timing or no table), and for a row that names none."""
    raw = row.get("events")
    text = " ".join(
        str(v)
        for v in (row.get("event_type"), raw if isinstance(raw, str) else None)
        if v is not None
    )
    if dml_trigger(row) is not None or trigger_events(text):
        return []
    out: list[str] = []
    for token in re.split(r"\s+OR\s+|,", text.upper()):
        event = " ".join(token.split())
        if _OTHER_EVENT.fullmatch(event) and event != "OTHER" and event not in out:
            out.append(event)
    return out


_NAMED_LIMIT = 20


def trigger_label(row: Mapping[str, Any]) -> str:
    """``schema.table.trigger`` for a log line, leaving out the parts the row lacks."""
    parts = (row.get("schema_name"), row.get("table_name"), row.get("trigger_name"))
    return ".".join(str(p) for p in parts if p) or "?"


def log_skipped_triggers(logger: logging.Logger, skipped: Sequence[Mapping[str, Any]]) -> None:
    """The one warning every builder gives for the trigger rows it skipped, naming them.
    A row the collection keeps among its triggers outside the DML list (#2023,
    :func:`other_trigger_events`) is not skipped, and not named."""
    skipped = [r for r in skipped if not other_trigger_events(r)]
    if not skipped:
        return
    names = [trigger_label(r) for r in skipped[:_NAMED_LIMIT]]
    more = len(skipped) - len(names)
    logger.warning(
        "Skipped %d trigger(s) the collection cannot hold (an INSERT/UPDATE/DELETE trigger "
        "with an unknown timing or no table_name, or no event named): %s%s",
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


def trigger_for_each(raw: object) -> str | None:
    """``ROW`` or ``STATEMENT`` from any spelling ('BEFORE EACH ROW', 'STATEMENT')."""
    text = str(raw or "").upper()
    if re.search(r"\bROW\b", text):
        return "ROW"
    if re.search(r"\bSTATEMENT\b", text):
        return "STATEMENT"
    return None


def trigger_function_name(action: object) -> str | None:
    """The function a PostgreSQL trigger executes, unqualified and unquoted, from its
    CREATE TRIGGER or its ``EXECUTE FUNCTION ...`` clause; None when there is none."""
    match = _FUNCTION_CALL.search(str(action or ""))
    if not match:
        return None
    last = re.split(r"\s*\.\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", match.group(1))[-1].strip()
    if last.startswith('"') and last.endswith('"'):
        last = last[1:-1].replace('""', '"')
    return last or None


def trigger_enabled(value: object) -> bool | None:
    """A trigger's enabled flag in any source spelling: True, 1, 'YES', 'ENABLED', and
    PostgreSQL's ``tgenabled`` 'O' (origin) and 'A' (always) fire; 'D' (disabled), 'R'
    (replica only), 0, 'NO' and 'DISABLED' do not. None when not reported."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("1", "TRUE", "YES", "ENABLED", "O", "A")


_ENABLED_MODES = frozenset("ODRA")


def trigger_enabled_mode(value: object) -> TriggerEnabledMode | None:
    """A PostgreSQL trigger's exact enable mode (#2071): the ``tgenabled`` (or
    ``evtenabled``) letter, O (origin), D (disabled), R (replica only) or A (always), as
    the collection reads it into ``is_enabled`` (or a DDL upload into ``enabled_mode``).
    None for any other value.

    The check reads the value, not the engine: it relies on no other engine's script or
    live query giving a lone O, D, R or A (MySQL and MariaDB give no state, SQL Server a 0
    or 1, Oracle ENABLED or DISABLED). A collector that does must leave this out."""
    if not isinstance(value, str):
        return None
    letter = value.strip()
    if len(letter) != 1 or letter not in _ENABLED_MODES:
        return None
    return cast("TriggerEnabledMode", letter)


def trigger_mode_source(row: Mapping[str, Any]) -> object:
    """What a raw trigger row's mode is read from: its ``enabled_mode`` when it has one
    (a DDL upload, where None means the file does not settle it), else its
    ``is_enabled`` (the collection scripts and live reads give the letter there)."""
    return row["enabled_mode"] if "enabled_mode" in row else row.get("is_enabled")


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = float(value)
    else:
        try:
            number = float(str(value))
        except ValueError:
            return None
    return number if number >= 0 else None


def execution_stats(row: Mapping[str, Any]) -> Any:
    """``ExecutionStats`` (or ``TriggerExecutionStats``) fields, as a dict the model
    reads, from a row's ``execution_count`` and ``total_time_ms``; None when the row has
    no count."""
    count = _number(row.get("execution_count"))
    if count is None:
        return None
    stats: dict[str, Any] = {"total_executions": int(count)}
    total = _number(row.get("total_time_ms"))
    if total is not None and count > 0:
        stats["avg_execution_time_ms"] = round(total / count, 3)
    return stats


def attach_routine_stats(
    procedures: Sequence[dict[str, Any]],
    triggers: Sequence[dict[str, Any]],
    stats: object,
    *,
    source_engine: str,
) -> None:
    """Copy each stats row's ``execution_count`` and ``total_time_ms`` onto its routine,
    in place, summed over overloads. A row matches by kind (trigger or routine) and name,
    and by schema when both sides name one (exactly for PostgreSQL, ignoring case for the
    other engines); a row for a routine the collection does not
    list (dropped, system, another schema) is ignored."""
    by_name: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for s in stats if isinstance(stats, list) else []:
        if isinstance(s, Mapping) and s.get("routine_name"):
            kind = "trigger" if str(s.get("object_type") or "").upper() == "TRIGGER" else "routine"
            by_name.setdefault((kind, _fold(s["routine_name"])), []).append(s)
    if not by_name:
        return
    same = _schema_key(source_engine)

    def attach(kind: str, item: dict[str, Any], name_key: str) -> None:
        schema = same(item.get("schema_name"))
        rows = [
            s
            for s in by_name.get((kind, _fold(item.get(name_key))), [])
            if not schema or not same(s.get("schema_name")) or same(s.get("schema_name")) == schema
        ]
        counts = [c for c in (_number(s.get("execution_count")) for s in rows) if c is not None]
        if not counts:
            return
        item["execution_count"] = int(sum(counts))
        times = [t for t in (_number(s.get("total_time_ms")) for s in rows) if t is not None]
        if times:
            item["total_time_ms"] = float(sum(times))

    for p in procedures:
        if isinstance(p, dict):
            attach("routine", p, "routine_name")
    for t in triggers:
        if isinstance(t, dict):
            attach("trigger", t, "trigger_name")


def attach_routine_sources(
    procedures: Sequence[dict[str, Any]], triggers: Sequence[dict[str, Any]], sources: object
) -> None:
    """Oracle: join ``ALL_SOURCE`` lines (``owner``, ``name``, ``type``, ``line``,
    ``text``) into each routine's and trigger's ``definition``, in line order, in place;
    a package is its spec, a blank line, then its body. A row that already has a
    definition keeps it."""
    lines: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
    for r in sources if isinstance(sources, list) else []:
        if isinstance(r, Mapping) and r.get("name"):
            key = (_fold(r.get("owner")), _fold(r["name"]), str(r.get("type") or "").upper())
            line = _number(r.get("line"))
            lines.setdefault(key, []).append((int(line or 0), str(r.get("text") or "")))
    if not lines:
        return
    texts = {k: "".join(t for _, t in sorted(v, key=lambda x: x[0])) for k, v in lines.items()}

    def body(owner: str, name: str, kind: str) -> str | None:
        if kind == "PACKAGE":
            parts = [texts.get((owner, name, "PACKAGE")), texts.get((owner, name, "PACKAGE BODY"))]
            return "\n".join(p for p in parts if p) or None
        return texts.get((owner, name, kind))

    for p in procedures:
        if isinstance(p, dict) and not p.get("definition"):
            text = body(
                _fold(p.get("schema_name")),
                _fold(p.get("routine_name")),
                str(p.get("routine_type") or "").upper(),
            )
            if text:
                p["definition"] = text
    for t in triggers:
        if isinstance(t, dict) and not t.get("definition"):
            text = body(_fold(t.get("schema_name")), _fold(t.get("trigger_name")), "TRIGGER")
            if text:
                t["definition"] = text


def routine_stats_status(raw: object) -> Any:
    """``RoutineStatsSource`` fields, as a dict the model reads, from a collection's
    ``routine_stats_status``, the reason capped; None when there is none or its status is
    not one the contract knows."""
    if not isinstance(raw, Mapping) or raw.get("status") not in _STATS_STATUSES:
        return None
    reason = raw.get("reason")
    return {"status": raw["status"], "reason": str(reason)[:_REASON_CAP] if reason else None}
