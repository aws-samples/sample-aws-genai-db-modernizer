"""The facade's readings of one query, stored on its collected query pattern (#934).

A collector parses every query once (:func:`src.tools.sql.facade.read_sql`). Every
later step read its query facts from the facade again, which parsed each query once
per step and process: on the discourse sample the deterministic pipeline went from
about 9 s to about 16 s once the routing and analysis facts were parsed (#893). So the
collector stores the two readings those steps use, :class:`~src.tools.sql.facade.SqlFacts`
and :class:`~src.tools.sql.facade.AccessFacts`, on each query pattern
(``QueryPattern.parsed_facts``), and :mod:`src.tools.sql.query_facts` reads them
back instead of parsing (:func:`src.tools.sql.query_facts.remember_collected_facts`).

The stored form is a JSON object:

- ``version``: :data:`FACTS_VERSION`, a digest of the code that reads the facts (the
  sqlglot version and the facade, regex-fact, normalizer, comment-scanner and codec
  sources). Any
  change to that code changes it, so facts an older build stored are never read: the
  query is parsed again, as for a collection with no stored facts;
- ``source_engine``: the source engine the facts were read for (its dialect);
- ``text_sha256``: the first 16 hex digits of the SHA-256 of the query text, so a
  pattern whose text was replaced after it was read (a Performance Insights text
  that is longer than the digest's) is parsed again;
- ``sql`` and ``access``: the two readings, each frozen dataclass as an object of its
  fields, tuples and frozensets as lists, leaving out every field that is ``None``,
  ``False``, ``0`` or empty (most of them, for most queries).

:func:`readings_from_stored` returns None for anything that is not exactly that, and
the caller parses instead: a stored reading is a shortcut, never a second definition.
"""

from __future__ import annotations

import dataclasses
import hashlib
import types
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from src.tools.sql.facade import PARSER_VERSION, AccessFacts, SqlFacts, SqlReading

__all__ = [
    "FACTS_VERSION",
    "readings_from_stored",
    "stored_reading",
    "stored_readings",
    "text_digest",
]

# The code whose answer the stored facts are (under ``src/``): the facade, its regex
# fallback, the digest normalizer, the comment scanner and span grammar it uses, and this codec. A
# change to any of them changes the version.
_READER_SOURCES = (
    "tools/sql/facade.py",
    "tools/sql/refs.py",
    "tools/sql/regex_facts.py",
    "tools/sql/normalize.py",
    "shared/sql_comments.py",
    "shared/sql_spans.py",
    "tools/sql/stored_facts.py",
)


def _facts_version() -> str | None:
    digest = hashlib.sha256(PARSER_VERSION.encode())
    src = Path(__file__).resolve().parents[2]
    try:
        for name in _READER_SOURCES:
            digest.update((src / name).read_bytes())
    except OSError:  # no sources to read: never store or trust stored facts
        return None
    return digest.hexdigest()[:16]


FACTS_VERSION: str | None = _facts_version()


def text_digest(text: str) -> str:
    """The query text's fingerprint in a stored reading."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _engine(source_engine: str | None) -> str:
    return (source_engine or "").strip().lower()


def stored_readings(
    text: str, source_engine: str | None, facts: SqlFacts, access: AccessFacts
) -> dict[str, Any] | None:
    """The stored form of one query's readings (see the module docstring), or None when
    this build can't version them."""
    if FACTS_VERSION is None:
        return None
    return {
        "version": FACTS_VERSION,
        "source_engine": _engine(source_engine),
        "text_sha256": text_digest(text),
        "sql": _encode(facts),
        "access": _encode(access),
    }


def stored_reading(
    text: str, source_engine: str | None, reading: SqlReading
) -> dict[str, Any] | None:
    """:func:`stored_readings` of a collector's reading, or None for a reading that is not
    the facade's answer (a ``facade_error`` fallback): a step parses that text itself."""
    if reading.facts.fallback_reason == "facade_error":
        return None
    return stored_readings(text, source_engine, reading.facts, reading.access)


def readings_from_stored(
    stored: object, text: str, source_engine: str | None
) -> tuple[SqlFacts, AccessFacts] | None:
    """The readings ``stored`` holds for ``text`` read for ``source_engine``, or None when
    it holds none this build can trust (another version, engine or text, or a malformed
    value)."""
    if not isinstance(stored, Mapping) or FACTS_VERSION is None:
        return None
    if (
        stored.get("version") != FACTS_VERSION
        or stored.get("source_engine") != _engine(source_engine)
        or stored.get("text_sha256") != text_digest(text)
    ):
        return None
    try:
        return _decode(SqlFacts, stored.get("sql")), _decode(AccessFacts, stored.get("access"))
    except (TypeError, ValueError, KeyError):
        return None


def _is_empty(value: Any) -> bool:
    """A value a stored dataclass leaves out: ``None``, ``False``, ``0``, empty."""
    return (
        value is None
        or value is False
        or (type(value) is int and value == 0)
        or value in ((), frozenset(), "")
    )


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {}
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            if not _is_empty(item):
                out[f.name] = _encode(item)
        return out
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(_encode(v) for v in value)
    if value is None or isinstance(value, bool | int | str):
        return value
    raise TypeError(f"no stored form for {type(value).__name__}")


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def _empty(tp: Any) -> Any:
    """The value a field of type ``tp`` has when the stored form leaves it out."""
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, types.UnionType) and type(None) in args:
        return None
    if origin is tuple:
        return ()
    if origin is frozenset:
        return frozenset()
    if origin is Literal and "" in args:
        return ""
    if tp in (bool, int, str):
        return tp()
    raise ValueError(f"a {tp!r} field can't be left out")


def _decode(tp: Any, value: Any) -> Any:
    """``value`` (a stored form) as type ``tp``; ValueError or TypeError if it is not one."""
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        if not isinstance(value, Mapping):
            raise TypeError(f"{tp.__name__}: not an object")
        hints = _hints(tp)
        names = {f.name for f in dataclasses.fields(tp)}
        if set(value) - names:
            raise ValueError(f"{tp.__name__}: unknown fields {sorted(set(value) - names)}")
        return tp(
            **{
                f.name: (
                    _decode(hints[f.name], value[f.name])
                    if f.name in value
                    else _empty(hints[f.name])
                )
                for f in dataclasses.fields(tp)
            }
        )
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        (inner,) = [a for a in args if a is not type(None)]
        return _decode(inner, value)
    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError("not a list")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v) for v in value)
        if len(args) != len(value):
            raise ValueError("wrong length")
        return tuple(_decode(a, v) for a, v in zip(args, value, strict=True))
    if origin is frozenset:
        if not isinstance(value, list):
            raise TypeError("not a list")
        return frozenset(_decode(args[0], v) for v in value)
    if origin is Literal:
        if value not in args:
            raise ValueError(f"{value!r} is not one of {args}")
        return value
    if tp in (bool, int, str):
        if type(value) is not tp:
            raise TypeError(f"{value!r} is not a {tp.__name__}")
        return value
    raise TypeError(f"no stored form for {tp!r}")
