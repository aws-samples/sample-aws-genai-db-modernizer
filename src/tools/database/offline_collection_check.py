# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The input check of an offline collection, run before any transform (#131).

An offline collection is the one JSON object a collection script
(``scripts/collect-*.sql``) prints; its sections are listed in
``docs/data-specs/database-collection-matrix.md``. The parser
(:func:`src.tools.database.offline_parser.parse_offline_collection`) used to go
straight from the decoded document to the transform, so a file that was not JSON
raised a decode error naming no file, an entry missing a key the transform reads
raised a ``KeyError`` from inside it, and a JSON document of another shape parsed
into an empty collection with no error at all.

The check refuses only what the transform cannot read, never a stricter schema:
what loads today keeps loading.

- The document is a JSON object holding ``tables`` or ``queries``.
- A section the transform iterates (:data:`ITERATED_SECTIONS`) is a list of objects.
  An empty value of another type (``{}``, ``""``, ``0``, ``false``, ``null``) reads as
  empty, as it always has, and is reported as absent. ``global_stats`` and ``os_stats``
  are objects, or empty. ``metadata`` given as a string holds JSON. ``io_stats`` and
  ``wait_events`` are passed on as given, so they are not checked.
- Each entry carries the keys the transform reads from it with no default
  (:data:`REQUIRED_ENTRY_KEYS`), read where the transform reads them: a column's
  ``column_name`` only for a column of a collected table, an index's ``non_unique``
  only on its first row, a foreign key's ``referenced_table_name`` only on its
  constraint's first row. Every other key is optional; extra keys are kept.

Every problem goes into one :class:`OfflineCollectionError`. The messages name only
the file, sections, positions, types and the format's own key names: never a value
of the document, and of its unknown top-level keys only their count and, for a key
that looks like a misnamed section (``index`` for ``indexes``), the short name.

The Oracle script's SQL*Plus spool ends each list with a ``{"_sentinel": true}``
entry and may carry unescaped control characters: :func:`read_collection_text` and
:func:`without_sentinels` clean both, whichever path the collection takes.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from src.tools.database.collection_addenda import merge_addenda
from src.tools.database.collection_text import extract_json_objects

logger = logging.getLogger(__name__)

#: The stage named in every message: the collect phase, before the parser runs.
STAGE = "collect stage, before parsing"

#: The sections that make a document a raw collection: one with neither is not one.
#: One that is absent (a collection of only tables, or only statements) reads as
#: empty and is reported, like the common sections.
REQUIRED_SECTIONS: tuple[str, ...] = ("tables", "queries")

#: The other sections every collection script writes. One that is absent reads as
#: empty and is reported (``absent_sections``).
COMMON_SECTIONS: tuple[str, ...] = (
    "metadata",
    "columns",
    "indexes",
    "foreign_keys",
    "primary_keys",
    "views",
    "procedures",
    "triggers",
    "global_stats",
)

#: Sections only some engines' scripts write (the SQL Server and Oracle scripts; the
#: statement-to-routine links, ``query_routines``, the SQL Server script alone, #1891):
#: never reported absent.
ENGINE_SECTIONS: tuple[str, ...] = ("io_stats", "wait_events", "os_stats", "query_routines")

#: Optional routine sections some scripts write since #1652 (routine execution counts and
#: how they were read; Oracle's routine source lines; MySQL's routine parameters and
#: scheduled events; PostgreSQL's event triggers, #2023): never reported absent, never
#: unknown.
ROUTINE_SECTIONS: tuple[str, ...] = (
    "event_triggers",  # PostgreSQL's (#2023)
    "events",
    "routine_parameters",
    "routine_sources",
    "routine_stats",
    "routine_stats_status",
)

#: Sections the transform iterates entry by entry: a non-empty one is a list of objects.
ITERATED_SECTIONS: tuple[str, ...] = (
    "tables",
    "columns",
    "indexes",
    "foreign_keys",
    "primary_keys",
    "views",
    "procedures",
    "triggers",
    "queries",
    "query_routines",
)

#: Sections the transform reads as objects: a non-empty one is an object.
OBJECT_SECTIONS: tuple[str, ...] = ("global_stats", "os_stats")

#: Top-level keys the scripts write besides the sections.
_HEADER_KEYS: frozenset[str] = frozenset({"collection_version", "collected_at"})

_KNOWN_KEYS: frozenset[str] = frozenset(
    {*REQUIRED_SECTIONS, *COMMON_SECTIONS, *ENGINE_SECTIONS, *ROUTINE_SECTIONS, *_HEADER_KEYS}
)

#: The keys the transform and the builder read from every entry with no default.
#: Three more are read from some entries only (see :func:`_entry_problems`).
REQUIRED_ENTRY_KEYS: Mapping[str, tuple[str, ...]] = {
    "tables": ("table_name",),
    "columns": ("table_name",),
    "indexes": ("table_name", "index_name"),
    "foreign_keys": ("table_name", "constraint_name", "column_name", "referenced_column_name"),
    "primary_keys": ("table_name", "column_name"),
}

#: Keys read only on the first row of a group: section -> (group keys, key).
_FIRST_ROW_KEYS: Mapping[str, tuple[tuple[str, str], str]] = {
    "indexes": (("table_name", "index_name"), "non_unique"),
    "foreign_keys": (("table_name", "constraint_name"), "referenced_table_name"),
}

#: How each section's entries are described in a refusal, conditional keys included.
_NEEDS: Mapping[str, str] = {
    "tables": "table_name",
    "columns": "table_name, and column_name for a column of a collected table",
    "indexes": "table_name, index_name, and non_unique on each index's first row",
    "foreign_keys": (
        "table_name, constraint_name, column_name, referenced_column_name, and "
        "referenced_table_name on each constraint's first row"
    ),
    "primary_keys": "table_name, column_name",
}

#: At most this many problems are spelled out in one message; the rest are counted.
MAX_LISTED_PROBLEMS = 50

#: The Oracle script's end-of-list marker (``DBMS_OUTPUT`` cannot drop a trailing comma).
SENTINEL: Mapping[str, bool] = {"_sentinel": True}

# Control characters Oracle 19c's JSON_OBJECT leaves unescaped, newlines aside: each
# becomes a space, so every position in the text stays where it was.
_CONTROL_CHARACTERS = {code: " " for code in range(32) if code != ord("\n")}

# A top-level key short and plain enough to repeat as a hint; only one that also looks
# like a section name is repeated.
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}\Z")


class OfflineCollectionError(ValueError):
    """An offline collection cannot be read as one (#131).

    The message names the file, the stage and every problem found, with no value
    from the document, so a caller shows it as is (a status line, an API error)."""


def _refusal(source: str, reason: str) -> OfflineCollectionError:
    return OfflineCollectionError(f"The offline collection {source} ({STAGE}) {reason}")


def _not_valid(source: str, exc: ValueError) -> OfflineCollectionError:
    detail = str(exc).removeprefix(f"{source}: ")
    if "no matching closing" in detail:
        detail += " (the file looks cut short)"
    return _refusal(source, f"is not valid JSON: {detail}")


def decode_collection_object(body: str, *, source: str, text: str | None = None) -> Any:
    """``json.loads(body)``; a decode error names its line and column, counted in
    ``text`` (the text ``body`` was extracted from) when given.

    Raises:
        OfflineCollectionError: ``body`` is not valid JSON.
    """
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        at, within = exc.pos, body
        if text is not None:
            within = text.lstrip("\ufeff")
            at += max(within.find("{"), 0)
        line = within.count("\n", 0, at) + 1
        column = at - within.rfind("\n", 0, at)
        raise _refusal(
            source, f"is not valid JSON at line {line}, column {column}: {exc.msg}"
        ) from None


def without_sentinels(data: Any) -> Any:
    """``data`` with the trailing ``{"_sentinel": true}`` entry of each top-level list
    dropped (the Oracle script's end-of-list marker); a new dict when one is dropped,
    ``data`` itself otherwise. A sentinel anywhere else is data and is kept."""
    if not isinstance(data, dict):
        return data
    dropped = {
        key: value[:-1]
        for key, value in data.items()
        if isinstance(value, list) and value and value[-1] == SENTINEL
    }
    return {**data, **dropped} if dropped else data


def read_collection_text(text: str, *, source: str) -> dict[str, Any]:
    """The collection object in ``text``, a collection script's output, cleaned of
    Oracle's control characters and end-of-list sentinels, with any decode error
    placed at its line and column of ``text``.

    Raises:
        OfflineCollectionError: ``text`` holds no JSON object, or its object is
            not valid JSON.
    """
    try:
        body, later = extract_json_objects(text.translate(_CONTROL_CHARACTERS), source=source)
    except ValueError as exc:
        raise _not_valid(source, exc) from None
    data = decode_collection_object(body, source=source, text=text)
    if not isinstance(data, dict):  # never: the extracted text is one object
        raise _refusal(source, f"is {_kind(data)}, not a JSON object")
    try:
        merge_addenda(data, later, source=source)  # a second object some scripts print (#2128)
    except ValueError as exc:
        raise _not_valid(source, exc) from None
    cleaned: dict[str, Any] = without_sentinels(data)
    return cleaned


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    return type(value).__name__


def _section_like(key: str) -> str | None:
    """The section ``key`` looks like a misnaming of (``index`` for ``indexes``), or None."""
    sections = sorted({*REQUIRED_SECTIONS, *COMMON_SECTIONS, *ENGINE_SECTIONS})
    folded = key.lower()
    for section in sections:
        if len(folded) >= 4 and (section.startswith(folded) or folded.startswith(section)):
            return section
    close = difflib.get_close_matches(folded, sections, n=1, cutoff=0.8)
    return close[0] if close else None


def _unknown_keys_note(data: Mapping[str, Any]) -> str:
    """How many top-level keys the format does not know, with any that looks like a
    misnamed section; no other key's text is repeated (it may be customer data)."""
    unknown = [key for key in data if key not in _KNOWN_KEYS]
    if not unknown:
        return ""
    hints = []
    for key in unknown:
        if isinstance(key, str) and _PLAIN_KEY.match(key):
            section = _section_like(key)
            if section:
                hints.append(f"'{key}' (for '{section}'?)")
    noun = "key" if len(unknown) == 1 else "keys"
    return f"{len(unknown)} top-level {noun} the format does not know" + (
        f", among them {', '.join(hints[:5])}" if hints else ""
    )


def _entry_problems(section: str, entries: list, table_names: set[object]) -> list[str]:
    required = REQUIRED_ENTRY_KEYS.get(section, ())
    group, first_row_key = _FIRST_ROW_KEYS.get(section, (("", ""), ""))
    seen_groups: set[tuple[str, str]] = set()
    problems = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"{section}[{position}] is {_kind(entry)}, not an object")
            continue
        missing = [key for key in required if key not in entry]
        # Read only where the transform reads them: a column of a collected table, the
        # first row of an index or of a foreign-key constraint.
        if section == "columns" and "column_name" not in entry:
            if entry.get("table_name") in table_names:
                missing.append("column_name")
        if first_row_key and all(key in entry for key in group):
            key = (repr(entry[group[0]]), repr(entry[group[1]]))
            if key not in seen_groups:
                seen_groups.add(key)
                if first_row_key not in entry:
                    missing.append(first_row_key)
        if missing:
            problems.append(f"{section}[{position}] lacks {', '.join(map(repr, missing))}")
    return problems


def _is_empty(value: object) -> bool:
    """A value the transform reads as an empty section, whatever its type."""
    return value is None or (isinstance(value, list | dict | str | int | float) and not value)


def validate_offline_collection(data: object, *, source: str) -> tuple[str, ...]:
    """Check ``data`` (a decoded collection, sentinels dropped) before the parser
    transforms it.

    Leaves ``data`` as given. Returns the sections of :data:`REQUIRED_SECTIONS` and
    :data:`COMMON_SECTIONS` the collection lacks or holds empty in a type other than
    its own (``null``, ``{}`` for a list, a ``metadata`` that is no object), which read
    as empty (:func:`warn_absent_sections` logs them).

    Raises:
        OfflineCollectionError: ``data`` is not an object, has neither ``tables``
            nor ``queries``, has a non-empty section of a type the transform cannot
            read, or entries that are not objects or miss a key the transform reads;
            one error lists every problem.
    """
    if not isinstance(data, dict):
        raise _refusal(
            source,
            f"is {_kind(data)}, not an offline collection; expected one JSON object "
            "with 'tables' and 'queries', as the collection scripts write",
        )
    if not any(key in data for key in REQUIRED_SECTIONS):
        note = _unknown_keys_note(data) or "it has no top-level keys"
        raise _refusal(
            source,
            f"is not an offline collection: it has neither 'tables' nor 'queries' ({note}); "
            "expected the collection scripts' output",
        )

    shape: list[str] = []
    entries: list[str] = []
    tables = data.get("tables")
    table_names: set[object] = (
        {t.get("table_name") for t in tables if isinstance(t, dict)}
        if isinstance(tables, list)
        else set()
    )
    for section in ITERATED_SECTIONS:
        value = data.get(section)
        if isinstance(value, list):
            entries.extend(_entry_problems(section, value, table_names))
        elif not _is_empty(value):
            shape.append(f"'{section}' is {_kind(value)}; expected a list of objects")
    for section in OBJECT_SECTIONS:
        value = data.get(section)
        if not isinstance(value, dict) and not _is_empty(value):
            shape.append(f"'{section}' is {_kind(value)}; expected an object")
    metadata = data.get("metadata")
    if isinstance(metadata, str):
        try:
            json.loads(metadata)
        except ValueError:
            shape.append("'metadata' is a string that does not hold JSON; expected an object")

    if shape or entries:
        raise _refusal(source, _problem_summary(shape, entries))

    return tuple(
        section
        for section in (*REQUIRED_SECTIONS, *COMMON_SECTIONS)
        if _reads_as_absent(section, data.get(section))
    )


def _reads_as_absent(section: str, value: object) -> bool:
    if section == "metadata":
        if isinstance(value, str):
            value = json.loads(value)  # valid: checked above
        return not isinstance(value, dict)
    expected = dict if section in OBJECT_SECTIONS else list
    return value is None or (not isinstance(value, expected) and _is_empty(value))


def warn_absent_sections(data: Mapping[str, Any], absent: tuple[str, ...], *, source: str) -> None:
    """Log the sections ``data`` lacks (:func:`validate_offline_collection`'s result),
    with how many top-level keys the format does not know and any that looks like a
    misnamed section (``index`` for ``indexes``); no other key is repeated."""
    if not absent:
        return
    note = _unknown_keys_note(data)
    logger.warning(
        "offline collection check: %s has no %s section%s, read as empty%s",
        source,
        ", ".join(absent),
        "s" if len(absent) > 1 else "",
        f"; {note}" if note else "",
    )


def _problem_summary(shape: list[str], entries: list[str]) -> str:
    parts = []
    if shape:
        parts.append("has the wrong shape: " + "; ".join(shape))
    if entries:
        listed = entries[:MAX_LISTED_PROBLEMS]
        more = len(entries) - len(listed)
        noun = "entry" if len(entries) == 1 else "entries"
        parts.append(
            f"has {len(entries)} {noun} the parser cannot read: "
            + "; ".join(listed)
            + (f"; and {more} more" if more else "")
            + " (positions count from 0). Each entry is an object"
            + "".join(
                f"; {section} entries need {needs}"
                for section, needs in _NEEDS.items()
                if any(problem.startswith(f"{section}[") for problem in entries)
            )
        )
    return ", and ".join(parts) + "."
