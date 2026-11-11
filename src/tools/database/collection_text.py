# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One tolerant reader for raw collection JSON (#396).

Database clients wrap the JSON a collection script prints with extra text
around it: a column header line (``collection_output``) when a MySQL client
is run without ``-N``, a ``---`` separator line some clients (e.g. ``sqlcmd``)
write unless told not to, a leading UTF-8 BOM (easy to pick up by saving a
file from a Windows editor), and/or trailing chatter such as
``(1 rows affected)``. Four call sites each used to repeat the same
two-line, header-only check; this module is the single tolerant reader all
of them use.
"""

from __future__ import annotations

import re

_DEFAULT_SOURCE = "<text>"
# The start of the one object a script may print after the collection (see collection_addenda).
_CUT_ADDENDUM = re.compile(r'\{\s*"lob_sizes"')


def extract_json_object(text: str, *, source: str = _DEFAULT_SOURCE) -> str:
    """Return the JSON object body embedded in ``text``.

    Strips a leading UTF-8 BOM, then finds the first ``{`` and the ``}``
    that balances it (ignoring braces inside JSON string values), so a
    leading header or separator line and trailing client chatter are
    ignored. Everything outside that span is dropped; callers still do
    their own ``json.loads`` (and any cleanup, e.g. control-character
    stripping) on the result.

    Raises:
        ValueError: naming ``source``, when no ``{`` is present, when the
            braces never balance, or when more than one object follows.
    """
    stripped = text.lstrip("\ufeff")
    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"{source}: no JSON object found (no '{{' in the content)")

    end = _object_end(stripped, start)

    if end is None:
        raise ValueError(f"{source}: no matching closing '}}' for the JSON object")

    # Every collection script prints exactly one object. A second one (or a
    # top-level array of objects) is not client chatter to ignore: returning
    # only the first would silently drop data.
    if "{" in stripped[end + 1 :]:
        raise ValueError(f"{source}: more than one JSON object found; expected exactly one")

    return stripped[start : end + 1]


def _object_end(text: str, start: int) -> int | None:
    """The index of the ``}`` that balances the ``{`` at ``start`` (braces inside JSON string
    values ignored), or None when the object never closes."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json_objects(text: str, *, source: str = _DEFAULT_SOURCE) -> tuple[str, list[str]]:
    """The collection object in ``text`` and the complete objects that follow it (#2128).

    A collection script may print a second object after the collection (the MySQL script's
    ``{"lob_sizes": [...]}``), so that a failure of the step that prints it cannot lose the
    collection. The first object is read as :func:`extract_json_object` reads it; each later
    one is returned for the caller to check. A trailing ``lob_sizes`` object that never closes (the measurement was
    interrupted) is dropped: what it held is simply not there. Any other unclosed object
    after the first raises, as a second object always did.

    Raises:
        ValueError: as :func:`extract_json_object` for the first object, and when a later
            object is unclosed and is not a cut ``lob_sizes`` object.
    """
    stripped = text.lstrip("\ufeff")
    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"{source}: no JSON object found (no '{{' in the content)")
    end = _object_end(stripped, start)
    if end is None:
        raise ValueError(f"{source}: no matching closing '}}' for the JSON object")
    extra: list[str] = []
    at = stripped.find("{", end + 1)
    while at != -1:
        close = _object_end(stripped, at)
        if close is None:
            # Only the LOB sizes a script prints last may be cut short (its measurement was
            # interrupted); any other unclosed object is data that would be silently dropped.
            if _CUT_ADDENDUM.match(stripped, at):
                break
            raise ValueError(f"{source}: more than one JSON object found; expected exactly one")
        extra.append(stripped[at : close + 1])
        at = stripped.find("{", close + 1)
    return stripped[start : end + 1], extra
