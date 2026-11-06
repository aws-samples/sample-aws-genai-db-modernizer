# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MySQL CREATE statements rebuilt from information_schema (#1652, routine migration).

The offline collection script is a chain of SELECTs that ends in one JSON object, and
SHOW CREATE PROCEDURE / FUNCTION / TRIGGER / EVENT cannot run inside a SELECT, be read
INTO a variable, or loop without a stored procedure the read-only user may not create.
So the script reads the catalog columns SHOW CREATE prints from (``ROUTINES``,
``PARAMETERS``, ``TRIGGERS``, ``EVENTS``) and this module writes the statement. A live
collector reads SHOW CREATE itself, and falls back to this rebuild only where SHOW
CREATE is not permitted.

The rebuild is not exact. A parameter's type is the catalog's lower-case
``DTD_IDENTIFIER``, and each string parameter names the character set and collation it
resolved to, which load the same. But the catalog's body drops each literal's
character-set introducer: ``_binary'Admin' = 'admin'`` reads ``'Admin' = 'admin'``,
which compares differently, and ``_utf16'a'`` reads ``''``. So a rebuild is exact only
when the body holds no string or hex literal (:func:`rebuild_exactness`); any other is
marked not exact, and is carried only once it is collected again with SHOW CREATE.
``tests/integration/test_mysql_routine_create_round_trip.py`` checks both on a real
server. Names are written unqualified: the statement is loaded in the target database.

Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_INTEGER = re.compile(r"[0-9]+")
# A string, national, hex or bit literal: the catalog may have dropped its introducer.
_LITERAL = re.compile(r"'|\b0x[0-9a-f]|\b0b[01]", re.I)
NOT_EXACT_REASON = (
    "rebuilt from information_schema around a string or hex literal, whose character-set "
    "introducer the catalog may have dropped: collect it with the live collector (SHOW "
    "CREATE), or supply SHOW CREATE output, to carry it"
)
_FIELD = re.compile(r"[A-Z_]+")
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_NAMES_CHARSET = re.compile(r"\b(?:CHARSET|CHARACTER\s+SET)\b", re.I)
_DATA_ACCESS = ("NO SQL", "READS SQL DATA", "MODIFIES SQL DATA")
_MODES = ("IN", "OUT", "INOUT")
_EVENTS = ("INSERT", "UPDATE", "DELETE")
_TIMINGS = ("BEFORE", "AFTER")


def _upper(value: object) -> str:
    return str(value or "").strip().upper()


def quote_ident(name: object) -> str:
    """``name`` in backticks, each backtick doubled: a name in any sql_mode."""
    return "`" + str(name).replace("`", "``") + "`"


def definer_clause(definer: object) -> str:
    """``DEFINER=`user`@`host` `` (with its trailing space) for information_schema's
    ``user@host``; the host is after the last ``@``. Empty for no definer."""
    text = str(definer or "").strip()
    if not text:
        return ""
    user, at, host = text.rpartition("@")
    if not at:
        return f"DEFINER={quote_ident(text)} "
    return f"DEFINER={quote_ident(user)}@{quote_ident(host)} "


def sql_modes(sql_mode: object) -> set[str]:
    """The modes of a sql_mode as the catalog gives it: 'A,B' text, or a set of names
    (a client that reads the SET column as one)."""
    if isinstance(sql_mode, set | frozenset | list | tuple):
        return {_upper(m) for m in sql_mode}
    return {m.strip() for m in _upper(sql_mode).split(",")}


def sql_mode_text(sql_mode: object) -> str | None:
    """A sql_mode as 'A,B' text (sorted when it came as a set); None when not one."""
    if isinstance(sql_mode, str):
        return sql_mode
    if isinstance(sql_mode, set | frozenset | list | tuple):
        return ",".join(sorted(str(m) for m in sql_mode))
    return None


def rebuild_exactness(body: object, sql_mode: object) -> str | None:
    """None when a CREATE rebuilt around ``body`` is exact: the body holds no string or
    hex literal (a double-quoted string counts unless ANSI_QUOTES makes it a name).
    Else the reason it is not exact."""
    text = str(body or "")
    if _LITERAL.search(text):
        return NOT_EXACT_REASON
    if (
        '"' in text
        and "ANSI_QUOTES" not in sql_modes(sql_mode)
        and "ANSI" not in sql_modes(sql_mode)
    ):
        return NOT_EXACT_REASON
    return None


def string_literal(text: object, sql_mode: object) -> str:
    """``text`` as a single-quoted string read the same under ``sql_mode``: a quote is
    doubled, and a backslash (and NUL) is escaped unless NO_BACKSLASH_ESCAPES makes a
    backslash plain."""
    value = str(text)
    modes = sql_modes(sql_mode)
    if "NO_BACKSLASH_ESCAPES" not in modes:
        value = value.replace("\\", "\\\\").replace("\0", "\\0")
    return "'" + value.replace("'", "''") + "'"


def _type_clause(parameter: Mapping[str, Any]) -> str:
    dtd = str(parameter.get("dtd_identifier") or "").strip()
    charset = parameter.get("character_set_name")
    if charset and not _NAMES_CHARSET.search(_QUOTED.sub("''", dtd)):
        dtd += f" CHARSET {charset}"
        if parameter.get("collation_name"):
            dtd += f" COLLATE {parameter['collation_name']}"
    return dtd


def _ordinal(parameter: Mapping[str, Any]) -> int:
    try:
        return int(parameter.get("ordinal") or 0)
    except (TypeError, ValueError):
        return 0


def routine_create(
    routine: Mapping[str, Any], parameters: Sequence[Mapping[str, Any]]
) -> str | None:
    """The CREATE PROCEDURE or CREATE FUNCTION for an ``information_schema.routines``
    row and its ``information_schema.parameters`` rows (ordinal 0: a function's return
    type), in SHOW CREATE's layout. None when the body was not read, the routine is not
    written in SQL, or a function has no return-type row."""
    body = routine.get("definition")
    kind = _upper(routine.get("routine_type"))
    if not isinstance(body, str) or not body or kind not in ("PROCEDURE", "FUNCTION"):
        return None
    if _upper(routine.get("external_language") or "SQL") != "SQL":
        return None
    args: list[str] = []
    returns: str | None = None
    for p in sorted(parameters, key=_ordinal):
        if _ordinal(p) == 0:
            returns = _type_clause(p)
            continue
        mode = _upper(p.get("mode"))
        prefix = f"{mode} " if kind == "PROCEDURE" and mode in _MODES else ""
        args.append(f"{prefix}{quote_ident(p.get('parameter_name'))} {_type_clause(p)}")
    if kind == "FUNCTION" and not returns:
        return None
    head = (
        f"CREATE {definer_clause(routine.get('definer'))}{kind} "
        f"{quote_ident(routine.get('routine_name'))}({', '.join(args)})"
    )
    if kind == "FUNCTION":
        head += f" RETURNS {returns}"
    lines = [head]
    access = _upper(routine.get("sql_data_access"))
    if access in _DATA_ACCESS:
        lines.append(f"    {access}")
    if _upper(routine.get("is_deterministic")) == "YES":
        lines.append("    DETERMINISTIC")
    if _upper(routine.get("security_type")) == "INVOKER":
        lines.append("    SQL SECURITY INVOKER")
    if routine.get("routine_comment"):
        comment = string_literal(routine["routine_comment"], routine.get("sql_mode"))
        lines.append(f"    COMMENT {comment}")
    return "\n".join(lines) + "\n" + body


def trigger_follows(triggers: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], str]:
    """For each trigger after the first of its table, timing and event (by
    ``action_order``), the trigger it follows, keyed by (schema, table, name): its
    CREATE says FOLLOWS, so the order is explicit in the statement."""
    groups: dict[tuple[str, str, str, str], list[tuple[int, str]]] = {}
    for t in triggers:
        try:
            order = int(t.get("action_order") or 0)
        except (TypeError, ValueError):
            continue
        if order < 1 or not t.get("trigger_name"):
            continue
        key = (
            str(t.get("schema_name") or ""),
            str(t.get("table_name") or ""),
            _upper(t.get("timing")),
            _upper(t.get("event_type")),
        )
        groups.setdefault(key, []).append((order, str(t["trigger_name"])))
    follows: dict[tuple[str, str, str], str] = {}
    for (schema, table, _timing, _event), members in groups.items():
        ordered = sorted(members)
        for (_, previous), (_, name) in zip(ordered, ordered[1:], strict=False):
            follows[(schema, table, name)] = previous
    return follows


def trigger_create(trigger: Mapping[str, Any], follows: str | None = None) -> str | None:
    """The CREATE TRIGGER for an ``information_schema.triggers`` row; None without a
    body, a BEFORE/AFTER timing, a DML event, a name or a table."""
    body = trigger.get("definition")
    timing, event = _upper(trigger.get("timing")), _upper(trigger.get("event_type"))
    if (
        not isinstance(body, str)
        or not body
        or timing not in _TIMINGS
        or event not in _EVENTS
        or not trigger.get("trigger_name")
        or not trigger.get("table_name")
    ):
        return None
    text = (
        f"CREATE {definer_clause(trigger.get('definer'))}TRIGGER "
        f"{quote_ident(trigger['trigger_name'])} {timing} {event} "
        f"ON {quote_ident(trigger['table_name'])} FOR EACH ROW"
    )
    if follows:
        text += f" FOLLOWS {quote_ident(follows)}"
    return f"{text} {body}"


def event_schedule(event: Mapping[str, Any]) -> str | None:
    """The ON SCHEDULE clause of an ``information_schema.events`` row (times in the
    event's own time zone, which the CREATE is replayed in); None when incomplete."""
    mode = event.get("sql_mode")
    kind = _upper(event.get("event_type"))
    if kind == "ONE TIME":
        at = event.get("execute_at")
        return f"AT {string_literal(at, mode)}" if at else None
    if kind != "RECURRING":
        return None
    value = str(event.get("interval_value") or "").strip()
    field = _upper(event.get("interval_field"))
    if not value or not _FIELD.fullmatch(field):
        return None
    amount = value if _INTEGER.fullmatch(value) else string_literal(value, mode)
    schedule = f"EVERY {amount} {field}"
    if event.get("starts"):
        schedule += f" STARTS {string_literal(event['starts'], mode)}"
    if event.get("ends"):
        schedule += f" ENDS {string_literal(event['ends'], mode)}"
    return schedule


def event_create(event: Mapping[str, Any]) -> str | None:
    """The CREATE EVENT for an ``information_schema.events`` row; None when the body or
    the schedule is missing."""
    body = event.get("definition")
    schedule = event_schedule(event)
    if not isinstance(body, str) or not body or schedule is None or not event.get("event_name"):
        return None
    completion = (
        "ON COMPLETION PRESERVE"
        if _upper(event.get("on_completion")) == "PRESERVE"
        else "ON COMPLETION NOT PRESERVE"
    )
    status = _upper(event.get("status"))
    # The replica-side disabled state is reported under two spellings, both ending in
    # SIDE_DISABLED; DISABLE ON REPLICA (MySQL 8.0.22+) is its clause.
    if status == "ENABLED":
        state = "ENABLE"
    elif status.endswith("SIDE_DISABLED"):
        state = "DISABLE ON REPLICA"
    else:
        state = "DISABLE"
    comment = (
        f" COMMENT {string_literal(event['event_comment'], event.get('sql_mode'))}"
        if event.get("event_comment")
        else ""
    )
    return (
        f"CREATE {definer_clause(event.get('definer'))}EVENT {quote_ident(event['event_name'])} "
        f"ON SCHEDULE {schedule} {completion} {state}{comment} DO {body}"
    )
