# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine sources of an ``assess()`` run for contract 1.4 (#2022).

A 1.4 run writes the job's routine sources, the index and its chunks, with the writer
the scripts and the live collectors use
(:func:`~src.agents.collector.routine_sources.write_routine_sources`), from two places:

- ``AssessmentRequest.routine_sources``: each routine's ``CREATE`` in the
  routine-sources format (``RoutineSource`` entries, ``src/contracts/routine_sources.py``;
  an unknown key is refused), used for its routine as given;
- a raw collection's own bodies, for every routine no entry names, extracted as the
  scripts extract them from the file
  (:func:`~src.agents.collector.routine_sources.iter_collection_sources`): its status,
  exact and opt-out rules unchanged. A collector output holds no body, so none is
  extracted from one.

Both go through the one writer, so under the same rules:

- the text stays only there: never in the redacted collector output, the result, a log
  line, an error message or a model input;
- an index plus gzip chunks of at most 5,000 routines. The writer holds one chunk at a
  time; the request itself holds every entry, so "one chunk at a time" holds on the
  writer side only;
- every body goes through the repository's credential scan (``src/shared/secret_scan.py``),
  whose findings the index names by kind;
- a collection made with ``include_routine_bodies = 0`` writes none, whatever is passed;
- only a routine the collection lists with its body collected gets a source: one listed
  ``not_readable``, ``opted_out`` or ``not_supported``, with no ``body_status`` (code
  alone, #2124), or not listed at all, gets none, as no collector writes one for it;
- a given source is written exact only when the caller says so (``exact``, true unless
  given) and the collection does not mark the routine's body not exact
  (``body_exact: false``, a MySQL rebuild around a literal): routine migration carries
  only exact text. An extracted one is exact as the collectors decide;
- only PostgreSQL, MySQL and MariaDB routines are carried, and an entry's ``origin`` must
  be one of its collection's engine.

:func:`check_request_routine_sources` checks the entries before anything runs, also for
a request that skipped validation. No entry (None or ``[]``) and no body in the
collection writes none. Every write replaces the job's earlier routine sources whole, as
a script's re-run does. A 1.3 request writes none (``src/assessment/runner.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ConfigDict, ValidationError

from src.agents.collector.routine_sources import (
    SOURCE_ENGINES,
    clear_routine_sources,
    collection_engine,
    iter_collection_sources,
    write_routine_sources,
)
from src.contracts.routine_sources import RoutineSource

logger = logging.getLogger(__name__)

RoutineKey = tuple[str, str, str]  # (kind, schema, specific name), as RoutineSource.key()

_COLLECTED = "collected"
#: What a skipped source's routine was, when the collection does not list it.
_NOT_LISTED = "not listed"
#: The origins each carried engine's collectors write.
_ORIGINS: dict[str, frozenset[str]] = {
    "postgresql": frozenset({"pg_get_functiondef", "pg_get_triggerdef", "pg_event_trigger"}),
    "mysql": frozenset({"show_create", "information_schema"}),
    "mariadb": frozenset({"show_create", "information_schema"}),
}
_NOT_EXACT = (
    "the collection marks this routine's body not exact (a rebuild from the catalog "
    "around a string or hex literal): collect it with the live collector, or supply SHOW "
    "CREATE output, to carry it"
)


class _RequestRoutineSource(RoutineSource):
    """A request's entry: a ``RoutineSource`` that refuses an unknown key, so a misspelt
    ``exact`` or ``sha256`` is never dropped silently."""

    model_config = ConfigDict(extra="forbid")


def _errors(exc: ValidationError) -> str:
    """The entry's errors by field and message, never the input (it is routine code)."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'entry'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False, include_context=False)
    )


def _read(entry: Any, number: int) -> RoutineSource:
    try:
        checked = _RequestRoutineSource.model_validate(entry)
    except ValidationError as exc:
        message = f"routine_sources[{number}] is not a RoutineSource: {_errors(exc)}"
    else:
        return RoutineSource.model_validate(checked.model_dump())
    raise ValueError(message)  # outside the except block: nothing chained


def validate_routine_sources(entries: Sequence[Any]) -> list[dict[str, Any]]:
    """Each entry read as a ``RoutineSource`` (an unknown key refused) and written back
    as JSON (its ``sha256`` filled in). Error messages name the entry and the field,
    never its text, and chain no exception.

    Raises:
        ValueError: an entry is not a ``RoutineSource``, or two name one routine
            (kind, schema, specific name).
    """
    if not isinstance(entries, list | tuple):
        raise ValueError("routine_sources must be a list of RoutineSource entries")
    out: list[dict[str, Any]] = []
    seen: set[RoutineKey] = set()
    for number, entry in enumerate(entries):
        source = _read(entry, number)
        if source.key() in seen:
            raise ValueError(
                f"routine_sources[{number}]: two routine sources for {source.kind} "
                f"{source.schema_name or ''}.{source.specific_name}"
            )
        seen.add(source.key())
        out.append(source.model_dump(mode="json"))
    return out


def request_collection_engine(collection: Mapping[str, Any]) -> str:
    """The source engine a request's collection names: a collector output's
    ``metadata.source_database.engine``, else the raw collection's own detector."""
    metadata = collection.get("metadata")
    source = metadata.get("source_database") if isinstance(metadata, Mapping) else None
    engine = source.get("engine") if isinstance(source, Mapping) else None
    if engine:
        return str(getattr(engine, "value", engine)).strip().lower()
    return collection_engine(collection)


def check_request_routine_sources(
    entries: Sequence[Any] | None, collection: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    """The request's entries, checked before anything runs (also for a request that
    skipped validation): each a ``RoutineSource``, one per routine, and each ``origin``
    one the collection's engine writes (for an engine routines are carried from).

    Raises:
        ValueError: as :func:`validate_routine_sources`, or an origin of another engine.
    """
    if entries is None:
        return None
    validated = validate_routine_sources(entries)
    check_origins(validated, collection)
    return validated


def check_origins(entries: Sequence[Mapping[str, Any]], collection: object) -> None:
    """Refuse an entry whose ``origin`` the collection's engine never writes
    (``pg_get_functiondef`` on a MySQL collection, say). An engine routines are not
    carried from is not checked: none of its sources is written.

    Raises:
        ValueError: naming the entry and its origin.
    """
    if not isinstance(collection, Mapping):
        return
    engine = request_collection_engine(collection)
    allowed = _ORIGINS.get(engine)
    if allowed is None:
        return
    for number, entry in enumerate(entries):
        if entry.get("origin") not in allowed:
            raise ValueError(
                f"routine_sources[{number}]: origin {entry.get('origin')!r} is not one a "
                f"{engine} collection writes ({', '.join(sorted(allowed))})"
            )


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


@dataclass(frozen=True)
class ListedRoutine:
    """What the collector output records of one routine: its ``body_status`` ("" when
    none) and ``body_exact`` (None when not recorded)."""

    status: str
    body_exact: bool | None


def collection_routines(collector_output: Mapping[str, Any]) -> dict[RoutineKey, ListedRoutine]:
    """Every routine the collector output lists, by its routine-sources key. The keys are
    the ones the collectors give their sources and routine migration reads them by: a
    function or procedure by its specific name, a trigger by ``<table>.<trigger>``, an
    event by name, a PostgreSQL event trigger (a ``non_dml_triggers`` entry outside a
    table) by name."""
    schema = collector_output.get("database_schema") or {}
    found: dict[RoutineKey, ListedRoutine] = {}

    def add(kind: str, row: Mapping[str, Any], name: str) -> None:
        if name:
            exact = row.get("body_exact")
            found.setdefault(
                (kind, _text(row.get("schema_name")), name),
                ListedRoutine(
                    _value(row.get("body_status")), exact if isinstance(exact, bool) else None
                ),
            )

    for row in schema.get("procedures") or []:
        if isinstance(row, Mapping):
            is_procedure = _value(row.get("procedure_type")).upper() == "PROCEDURE"
            name = _text(row.get("specific_name")) or _text(row.get("procedure_name"))
            add("procedure" if is_procedure else "function", row, name)
    for row in schema.get("triggers") or []:
        if isinstance(row, Mapping) and _text(row.get("trigger_name")):
            add("trigger", row, f"{_text(row.get('table_id'))}.{_text(row.get('trigger_name'))}")
    for row in schema.get("non_dml_triggers") or []:
        if not isinstance(row, Mapping) or not _text(row.get("trigger_name")):
            continue
        if _value(row.get("scope")) == "table":
            add("trigger", row, f"{_text(row.get('table_id'))}.{_text(row.get('trigger_name'))}")
        else:
            add("event_trigger", row, _text(row.get("trigger_name")))
    for row in schema.get("events") or []:
        if isinstance(row, Mapping):
            add("event", row, _text(row.get("event_name")))
    return found


def _includes_bodies(collector_output: Mapping[str, Any]) -> bool:
    metadata = collector_output.get("metadata")
    capture = metadata.get("routine_capture") if isinstance(metadata, Mapping) else None
    return not (isinstance(capture, Mapping) and capture.get("include_bodies") is False)


def _names(names: list[str]) -> str:
    more = f" and {len(names) - 20} more" if len(names) > 20 else ""
    return ", ".join(names[:20]) + more


def write_request_routine_sources(
    store: Any,
    database_name: str,
    job_id: str,
    entries: Sequence[Mapping[str, Any]],
    raw_collection: Mapping[str, Any] | None = None,
) -> str | None:
    """Write the job's routine sources for a 1.4 request whose collector output the
    ingest has written (module docstring): the request's ``entries``, then every
    routine ``raw_collection`` holds a body for that no entry names, extracted as the
    scripts extract them (:func:`~src.agents.collector.routine_sources.iter_collection_sources`
    of the file as given). Returns the index's key; None when none is written (and the
    job's earlier ones are cleared). Logs a warning for what it leaves out and for each
    given source it writes as not exact, naming each routine by kind and name, never its
    text."""
    output = store.read_json(f"{database_name}/{job_id}/collector/output.json")
    if not _includes_bodies(output):
        clear_routine_sources(store, database_name, job_id)
        if entries:
            logger.warning(
                "assess(): the collection ran with include_routine_bodies = 0, so none of "
                "the %d routine source(s) given is written",
                len(entries),
            )
        return None
    from src.tools.sql.redact import collector_output_engine

    engine = collector_output_engine(output)
    if engine not in SOURCE_ENGINES:
        clear_routine_sources(store, database_name, job_id)
        if entries:
            logger.warning(
                "assess(): routines are not carried from %s (only PostgreSQL, MySQL and "
                "MariaDB), so none of the %d routine source(s) given is written",
                engine or "this engine",
                len(entries),
            )
        return None
    listed = collection_routines(output)
    skipped: dict[str, list[str]] = {}
    made_inexact: list[str] = []

    def kept() -> Iterator[RoutineSource]:
        for entry in entries:
            source = RoutineSource.model_validate(entry)
            routine = listed.get(source.key())
            name = f"{source.kind} {source.schema_name or ''}.{source.specific_name}"
            status = routine.status if routine else _NOT_LISTED
            if status != _COLLECTED:
                skipped.setdefault(status or "no body status", []).append(name)
                continue
            if source.exact and routine is not None and routine.body_exact is False:
                made_inexact.append(name)
                source = source.model_copy(update={"exact": False, "inexact_reason": _NOT_EXACT})
            yield source
        if raw_collection is None:
            return
        # The collection's own bodies, for every routine no entry names: the collectors'
        # extraction, unchanged (their status, exact and opt-out rules).
        given = {RoutineSource.model_validate(entry).key() for entry in entries}
        for source in iter_collection_sources(raw_collection):
            if source.key() not in given:
                yield source

    key = write_routine_sources(store, database_name, job_id, output, kept())
    if made_inexact:
        logger.warning(
            "assess(): routine sources written as not exact, because the collection marks "
            "their bodies not exact: %s",
            _names(made_inexact),
        )
    if skipped:
        logger.warning(
            "assess(): routine sources not written, because the collection does not list "
            "the routine with its body collected (no collector writes one for it): %s",
            "; ".join(f"{status}: {_names(names)}" for status, names in sorted(skipped.items())),
        )
    return key
