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

_DEFAULT_SOURCE = "<text>"


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

    depth = 0
    in_string = False
    escape = False
    end = None
    for i in range(start, len(stripped)):
        ch = stripped[i]
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
                end = i
                break

    if end is None:
        raise ValueError(f"{source}: no matching closing '}}' for the JSON object")

    # Every collection script prints exactly one object. A second one (or a
    # top-level array of objects) is not client chatter to ignore: returning
    # only the first would silently drop data.
    if "{" in stripped[end + 1 :]:
        raise ValueError(f"{source}: more than one JSON object found; expected exactly one")

    return stripped[start : end + 1]
