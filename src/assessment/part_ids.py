"""The ``query_id`` of a statement part (:func:`src.assessment.cut_collection`, #1633).

A part is a caller's rewrite of a statement that crosses a cut (or that the cut cannot
place), over the cut's tables only. Its id is built by the library from what the caller
gave, never chosen by the caller:

    part-<h>-<from_query_id>

where ``<h>`` is the first 16 lowercase hexadecimal characters of the SHA-256 of the
compact JSON array ``[from_query_id, [the cut's table_ids, sorted]]`` (UTF-8), and
``from_query_id`` is the id of the statement the part was cut from. A cut takes one
part per statement, so the id is unique in the cut. It depends only on the caller's
input, never on what the parser reads in the part's text, so a parser change in a later
release never moves it: the same statement cut to the same tables always gets the same id.

No collection script writes an id of this form: their digests are hexadecimal, ``0x`` and
hexadecimal, a signed integer, ``fms_`` and digits, or an Oracle ``SQL_ID`` of lowercase
letters and digits, and the fallback is 16 hexadecimal characters of SHA-256 of the text;
none starts with ``part-``. A raw collection's ``digest`` is taken as given, so the guard
is ``cut_collection`` itself: it refuses a part whose id is already a statement of the
collection (``PartIdCollisionError``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

#: Every part id starts with this.
PART_ID_PREFIX = "part-"

#: A part id: the prefix, the 16-character hash, and the id of the statement it was cut from.
PART_ID_RE = re.compile(r"^part-(?P<hash>[0-9a-f]{16})-(?P<from_query_id>.+)$", re.DOTALL)


def part_query_id(from_query_id: str, tables: Iterable[str]) -> str:
    """The id of the part of ``from_query_id`` in a cut to ``tables`` (the cut's
    ``table_ids``, in any order)."""
    key = json.dumps([from_query_id, sorted(set(tables))], separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{PART_ID_PREFIX}{digest}-{from_query_id}"


def from_query_id_of(query_id: str) -> str | None:
    """The statement a part was cut from, read from the part's id; None for any id
    that is not a part's."""
    match = PART_ID_RE.match(query_id)
    return match.group("from_query_id") if match else None
