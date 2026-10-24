"""Load a collection for ``assess()`` from the collection scripts' raw output.

The scripts write text, often with a header line before the JSON object (for example
``collection_output`` followed by ``{...}``). :func:`load_collection` reads that text,
a file holding it, or a ``.zip`` with one collection file (as the bundled samples are
shipped) and returns the collection as a dict, using the reader the command line uses.

>>> from src.assessment import AssessmentRequest, assess, load_collection
>>> collection = load_collection(Path("wordpress.zip"))  # doctest: +SKIP
>>> assess(AssessmentRequest(database_name="shop", collection=collection))  # doctest: +SKIP
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

from src.tools.database.collection_text import extract_json_object

__all__ = ["CollectionLoadError", "load_collection"]

_REQUIRED_KEYS = ("tables", "queries")


class CollectionLoadError(ValueError):
    """The input could not be read as a collection; the message says why."""


def load_collection(source: str | bytes | os.PathLike[str]) -> dict[str, Any]:
    """Return the collection held by ``source``.

    ``source`` is one of:

    - ``str`` or ``bytes``: the scripts' output text (UTF-8 for bytes), with or without
      a header line before the JSON object or client chatter after it;
    - a path (``pathlib.Path`` or any ``os.PathLike``): a ``.json`` or ``.txt`` file with
      that text, or a ``.zip`` holding exactly one ``.json`` or ``.txt`` collection file.

    A ``str`` is always read as text, never as a path: pass ``Path(...)`` for a file.

    Raises:
        CollectionLoadError: the file is missing, no JSON object is found, a zip holds
            no collection or more than one, or the object is not a collection (it needs
            ``tables`` and ``queries``).
    """
    if isinstance(source, str):
        return _parse(source, "the text")
    if isinstance(source, bytes):
        return _parse(_decode(source, "the bytes"), "the bytes")
    if isinstance(source, os.PathLike):
        return _load_path(Path(source))
    raise CollectionLoadError(f"expected text, bytes or a path, got {type(source).__name__}")


def _decode(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CollectionLoadError(f"{label} is not UTF-8 text") from exc


def _load_path(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CollectionLoadError(f"file not found: {path}")
    if path.suffix.lower() == ".zip":
        return _load_zip(path)
    return _parse(_decode(path.read_bytes(), str(path)), str(path))


def _load_zip(path: Path) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise CollectionLoadError(f"{path} is not a valid zip file") from exc
    with archive:
        members = [
            name
            for name in archive.namelist()
            if not name.endswith("/")
            and not name.startswith("__MACOSX/")
            and name.lower().endswith((".json", ".txt"))
        ]
        if not members:
            raise CollectionLoadError(f"{path} holds no collection (.json or .txt) file")
        if len(members) > 1:
            raise CollectionLoadError(
                f"{path} holds more than one collection ({', '.join(sorted(members))}); "
                "expected exactly one"
            )
        label = f"{path}:{members[0]}"
        return _parse(_decode(archive.read(members[0]), label), label)


def _parse(text: str, label: str) -> dict[str, Any]:
    try:
        body = extract_json_object(text, source=label)
    except ValueError as exc:
        raise CollectionLoadError(str(exc)) from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CollectionLoadError(f"{label}: the JSON object is not valid JSON ({exc})") from exc
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise CollectionLoadError(
            f"{label}: not a collection (no {' or '.join(repr(k) for k in missing)} key); "
            "expected the collection scripts' output"
        )
    loaded: dict[str, Any] = data
    return loaded
