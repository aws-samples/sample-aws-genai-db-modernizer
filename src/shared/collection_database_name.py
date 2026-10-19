"""Derive a database name from a collection file name (#1096).

``/modernize`` used to state this rule in prose and let the orchestrating model
apply it; the scripts now apply it, so the same file gets the same name in every
mode. The result always satisfies ``validate_database_name`` (#418, #420), so it
is safe to use as an artifact path component.

Rule (first stated in ``.claude/commands/modernize.md`` by #1092): take the file
name, drop a trailing ``.json`` and then a trailing ``-collection``, replace every
character outside ``[A-Za-z0-9_-]``, and a leading ``-``, with ``_`` (one ``_`` per
character), keep the first 128 characters, and use ``database`` if nothing is left.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Any

from src.shared.database_name import validate_database_name

FALLBACK_DATABASE_NAME = "database"
#: Keep equal to the upper bound in ``DATABASE_NAME_PATTERN`` (a test checks it).
_MAX_LENGTH = 128
_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def database_name_from_collection_file(path: str | PurePath) -> str:
    """Return the database name for the collection file at ``path``."""
    stem = PurePath(str(path).replace("\\", "/")).name
    if stem.lower().endswith(".json"):
        stem = stem[: -len(".json")]
    if stem.endswith("-collection"):
        stem = stem[: -len("-collection")]
    name = _DISALLOWED.sub("_", stem)[:_MAX_LENGTH] or FALLBACK_DATABASE_NAME
    if name.startswith("-"):  # a name must not read as a command-line option
        name = "_" + name[1:]
    return validate_database_name(name)


def _metadata_database_name(data: Any) -> tuple[object, str] | None:
    """``(name, source)`` the collection file's own metadata states, or None.

    ``source`` is ``"metadata"`` for a ``database_name`` (also under
    ``source_database``) and ``"schema_name"`` for the ``schema_name`` of a
    metadata object that has no ``database_name``."""
    meta = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    if not isinstance(meta, dict):
        return None
    source = meta.get("source_database")
    if isinstance(source, dict) and source.get("database_name") is not None:
        return source["database_name"], "metadata"
    if meta.get("database_name") is not None:
        return meta["database_name"], "metadata"
    if meta.get("schema_name") is not None:
        return meta["schema_name"], "schema_name"
    return None


def resolve_database_name(
    data: Any, path: str | PurePath, *, from_file_name: bool = False
) -> tuple[str, str]:
    """Return ``(name, source)`` for a collection loaded from ``path`` when the
    caller gave no ``--db``.

    By default the file's own metadata name wins if it passes
    ``validate_database_name`` (source ``"metadata"``, or ``"schema_name"`` when
    only a ``schema_name`` is stated); otherwise, or with ``from_file_name``
    (what ``/modernize`` and ``/collect`` ask for), the name is derived from the
    file name (source ``"file_name"``). A hostile or odd metadata name never fails
    the run; it just falls back."""
    stated = None if from_file_name else _metadata_database_name(data)
    if stated is not None:
        try:
            return validate_database_name(stated[0]), stated[1]
        except ValueError:
            pass
    return database_name_from_collection_file(path), "file_name"
