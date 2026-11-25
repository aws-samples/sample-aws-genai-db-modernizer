# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aurora MySQL's two row limits, counted the way the server counts them (#2239).

``CREATE TABLE`` fails with ERROR 1118 when either limit is passed:

* **The server row, 65,535 bytes.** Every column counts its full width: a ``VARCHAR(n)``
  4 bytes per character in utf8mb4 (3 for ``NVARCHAR``, which is utf8mb3) plus a length
  prefix of 1 byte, or 2 above 255 bytes; a ``VARBINARY(n)`` its length plus the prefix;
  a ``CHAR(n)`` 4 bytes per character. TEXT, BLOB, JSON and spatial columns are stored
  apart and count only their 9 to 12 byte pointer. Nullable columns add one bit each,
  rounded up to bytes.
* **The InnoDB record, 8,125 bytes** (the server reports "> 8126"). A 16 KB page must
  hold two records, so the in-row part of a clustered-index record must fit half a page.
  A fixed-width column (numbers, dates, ``BINARY``, ``ENUM``, ``SET``, ``BIT``) is
  always in the row; a variable-width one (``VARCHAR``, ``VARBINARY``, a utf8mb4
  ``CHAR``) of 255 bytes or less is in the row with a 1-byte length; a longer one, and
  every TEXT and BLOB, can move off the page and counts 41 bytes. Each record also has a
  5-byte header, the NULL bits, a 6-byte transaction id and a 7-byte roll pointer, and a
  6-byte row id when the table has no primary key.

Every number was measured on MySQL 8.0.46 with utf8mb4 and 16 KB pages
(``tests/unit/tools/schema/aurora_common/test_mysql_row_size.py``). The draft writes no
character set, so its tables take the server default, utf8mb4; a source in a one-byte
character set fits more there, so the count errs on the side of moving a column.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.contracts.aurora_design_delta import is_enum_type

MYSQL_MAX_ROW_BYTES = 65535
MYSQL_MAX_RECORD_BYTES = 8125

_RECORD_HEADER_BYTES = 5
_TRANSACTION_BYTES = 6 + 7  # DB_TRX_ID and DB_ROLL_PTR
_ROW_ID_BYTES = 6  # DB_ROW_ID, only in a table with no primary key
_INLINE_MAX_BYTES = 255  # a longer variable-width column may be stored off the page
_OFF_PAGE_BYTES = 41  # 40 bytes kept in the row, and a 1-byte length

_UTF8MB4 = 4
_UTF8MB3 = 3
_LOB_POINTER_BYTES = {
    "tinytext": 9,
    "tinyblob": 9,
    "text": 10,
    "blob": 10,
    "mediumtext": 11,
    "mediumblob": 11,
    "longtext": 12,
    "longblob": 12,
    "json": 12,
}
_SPATIAL = frozenset(
    {
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
    }
)
_FIXED_BYTES = {
    "tinyint": 1,
    "bool": 1,
    "boolean": 1,
    "smallint": 2,
    "mediumint": 3,
    "int": 4,
    "integer": 4,
    "bigint": 8,
    "double": 8,
    "double precision": 8,
    "real": 8,
    "date": 3,
    "year": 1,
}
_DECIMALS = frozenset({"decimal", "dec", "numeric", "fixed"})
# Bytes MySQL packs a group of 0-8 leftover decimal digits into; nine digits take 4.
_DIGIT_BYTES = (0, 1, 1, 2, 2, 3, 3, 4, 4, 4)
_TEMPORAL_BASE = {"time": 3, "datetime": 5, "timestamp": 4}
_CHARACTER = {"char": _UTF8MB4, "character": _UTF8MB4, "nchar": _UTF8MB3, "national char": _UTF8MB3}
_VARYING = {"varchar": _UTF8MB4, "nvarchar": _UTF8MB3, "national varchar": _UTF8MB3}
_TYPE = re.compile(
    r"(?P<base>[a-z]+(?: [a-z]+)*?)\s*(?:\(\s*(?P<a>\d+)\s*(?:,\s*(?P<b>\d+)\s*)?\))?"
    r"(?P<post>(?: [a-z]+)*)"
)


def parse_type(aurora_type: str) -> tuple[str, int | None, int | None, str] | None:
    """``(base, first parameter, second parameter, suffix)`` of a plain type, lower case
    (``("decimal", 10, 2, "")``, ``("int", None, None, "unsigned")``), or None for an ENUM,
    a SET or a type of another shape."""
    m = _TYPE.fullmatch(" ".join(aurora_type.lower().split()))
    if m is None:
        return None
    a, b = m.group("a"), m.group("b")
    return (
        m.group("base"),
        int(a) if a is not None else None,
        int(b) if b is not None else None,
        m.group("post").strip(),
    )


@dataclass(frozen=True)
class ColumnWidth:
    """The bytes one NOT NULL column counts toward each limit."""

    row: int
    record: int


@dataclass(frozen=True)
class RowSize:
    row_bytes: int
    record_bytes: int

    @property
    def fits(self) -> bool:
        return self.row_bytes <= MYSQL_MAX_ROW_BYTES and self.record_bytes <= MYSQL_MAX_RECORD_BYTES


def _variable(max_bytes: int) -> ColumnWidth:
    prefix = 1 if max_bytes <= _INLINE_MAX_BYTES else 2
    record = max_bytes + 1 if max_bytes <= _INLINE_MAX_BYTES else _OFF_PAGE_BYTES
    return ColumnWidth(row=max_bytes + prefix, record=record)


def _fixed(n: int) -> ColumnWidth:
    return ColumnWidth(row=n, record=n)


def _decimal_bytes(precision: int, scale: int) -> int:
    def packed(digits: int) -> int:
        return digits // 9 * 4 + _DIGIT_BYTES[digits % 9]

    return packed(precision - scale) + packed(scale)


def _enum_width(aurora_type: str) -> ColumnWidth:
    count = len(re.findall(r"'(?:[^'\\\n\r]|'')*'", aurora_type))
    if aurora_type.lstrip()[:3].upper() == "SET":
        n = (count + 7) // 8
        return _fixed(8 if n > 4 else n)
    return _fixed(1 if count <= 255 else 2)


def column_width(aurora_type: str) -> ColumnWidth:
    """The bytes a NOT NULL column of ``aurora_type`` (a type the allowlist accepted)
    counts toward the server row and the InnoDB record. A type this does not know counts
    as a LONGBLOB would."""
    if is_enum_type(aurora_type.strip()):
        return _enum_width(aurora_type)
    m = _TYPE.fullmatch(" ".join(aurora_type.lower().split()))
    if m is None:
        return ColumnWidth(row=12, record=_OFF_PAGE_BYTES)
    base, a, b = m.group("base"), m.group("a"), m.group("b")
    n = int(a) if a is not None else None
    if base in _FIXED_BYTES:
        return _fixed(_FIXED_BYTES[base])
    if base == "float":
        return _fixed(8 if n is not None and n > 24 else 4)
    if base in _DECIMALS:
        return _fixed(_decimal_bytes(n if n is not None else 10, int(b) if b else 0))
    if base in _TEMPORAL_BASE:
        return _fixed(_TEMPORAL_BASE[base] + ((n or 0) + 1) // 2)
    if base == "bit":
        return _fixed(((n or 1) + 7) // 8)
    if base == "binary":
        return _fixed(n if n is not None else 1)
    if base in _CHARACTER:
        chars = n if n is not None else 1
        max_bytes = chars * _CHARACTER[base]
        # A utf8mb4 CHAR is variable-width in the record, with no length in the server row.
        return ColumnWidth(row=max_bytes, record=_variable(max_bytes).record)
    if base in _VARYING:
        return _variable((n if n is not None else 255) * _VARYING[base])
    if base == "varbinary":
        return _variable(n if n is not None else 255)
    if base in _LOB_POINTER_BYTES:
        return ColumnWidth(row=_LOB_POINTER_BYTES[base], record=_OFF_PAGE_BYTES)
    if base in _SPATIAL:
        return ColumnWidth(row=12, record=_OFF_PAGE_BYTES)
    return ColumnWidth(row=12, record=_OFF_PAGE_BYTES)


def row_size(columns: Sequence[tuple[str, bool]], *, has_primary_key: bool) -> RowSize:
    """The server row and InnoDB record bytes of a table of ``(aurora_type, nullable)``
    columns."""
    widths = [column_width(t) for t, _ in columns]
    null_bytes = (sum(1 for _, nullable in columns if nullable) + 7) // 8
    record = _RECORD_HEADER_BYTES + null_bytes + _TRANSACTION_BYTES
    if not has_primary_key:
        record += _ROW_ID_BYTES
    return RowSize(
        row_bytes=sum(w.row for w in widths) + null_bytes,
        record_bytes=record + sum(w.record for w in widths),
    )


def lob_type(aurora_type: str) -> str | None:
    """The TEXT or BLOB type that holds every value of a VARCHAR, NVARCHAR or VARBINARY
    column, or None for any other type: CHAR and BINARY pad their values, so a LOB would change what
    they hold."""
    m = _TYPE.fullmatch(" ".join(aurora_type.lower().split()))
    if m is None or m.group("a") is None or m.group("post"):
        return None
    n = int(m.group("a"))
    if m.group("base") in _VARYING:
        # NVARCHAR is utf8mb3 (a model's override); TEXT in the table's utf8mb4 holds it.
        return "TEXT" if n * _UTF8MB4 <= MYSQL_MAX_ROW_BYTES else "MEDIUMTEXT"
    if m.group("base") == "varbinary":
        return "BLOB"
    return None


def fit_row(
    columns: Sequence[tuple[str, bool]], *, movable: Sequence[bool], has_primary_key: bool
) -> tuple[list[int], RowSize]:
    """The positions to move to their :func:`lob_type`, in the order they move, and the
    row's size after the moves.

    While the server row is over its limit, the movable column that saves it the most
    bytes moves; then, while the InnoDB record is over, the one that saves the record the
    most. Of equal savings, the last column moves first, so the leading columns keep their
    types. A column whose move saves nothing never moves, and the result may still not
    fit (``RowSize.fits``): nothing left to move."""
    types = [t for t, _ in columns]
    nullable = [n for _, n in columns]
    moved: list[int] = []

    def size() -> RowSize:
        return row_size(list(zip(types, nullable, strict=True)), has_primary_key=has_primary_key)

    current = size()
    while not current.fits:
        over_row = current.row_bytes > MYSQL_MAX_ROW_BYTES
        best: tuple[int, int] | None = None
        for pos, aurora_type in enumerate(types):
            lob = lob_type(aurora_type) if movable[pos] and pos not in moved else None
            if lob is None:
                continue
            before, after = column_width(aurora_type), column_width(lob)
            saved = before.row - after.row if over_row else before.record - after.record
            if saved > 0 and (best is None or (saved, pos) > best):
                best = (saved, pos)
        if best is None:
            break
        pos = best[1]
        types[pos] = lob_type(types[pos]) or types[pos]
        moved.append(pos)
        current = size()
    return moved, current
