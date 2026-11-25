# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which Aurora MySQL foreign key column pairs the server refuses, and why (#2239).

MySQL needs "similar" types on the two sides of a foreign key and refuses the key with
ERROR 3780 otherwise. Measured on MySQL 8.0.46 with utf8mb4:

* refused: integers of another size or sign (INT and BIGINT, INT and INT UNSIGNED), a
  character string and a binary string, a utf8mb4 and a utf8mb3 (NVARCHAR) string, DATE
  and DATETIME, FLOAT and DOUBLE, YEAR and an integer; a TEXT or BLOB column can be in no
  foreign key at all;
* accepted: string lengths that differ, CHAR and VARCHAR, BINARY and VARBINARY, the
  fractional-second digits of DATETIME, TIMESTAMP or TIME, DATETIME and TIMESTAMP, BIT
  and ENUM of different widths.

DECIMAL columns of a different precision or scale were accepted too, but the MySQL
reference says fixed-precision types need the same size, so they count as refused.
A string length is never part of the check, so a child keeps its own length.
"""

from __future__ import annotations

from src.contracts.aurora_design_delta import is_enum_type
from src.tools.schema.aurora_common.mysql_row_size import parse_type

_INTEGERS = {
    "tinyint": "tinyint",
    "bool": "tinyint",
    "boolean": "tinyint",
    "smallint": "smallint",
    "mediumint": "mediumint",
    "int": "int",
    "integer": "int",
    "bigint": "bigint",
}
_DECIMALS = frozenset({"decimal", "dec", "numeric", "fixed"})
_CHARACTER = frozenset({"char", "character", "varchar"})
_NATIONAL = frozenset({"nchar", "national char", "nvarchar", "national varchar"})
_BINARY = frozenset({"binary", "varbinary"})
_LOBS = frozenset(
    {
        "tinytext", "text", "mediumtext", "longtext", "tinyblob", "blob", "mediumblob",
        "longblob", "json", "geometry", "point", "linestring", "polygon", "multipoint",
        "multilinestring", "multipolygon", "geometrycollection",
    }
)  # fmt: skip
_TEMPORAL = {"datetime": "datetime", "timestamp": "datetime", "time": "time", "date": "date"}

_INTEGER_REASON = "MySQL needs the integer columns of a foreign key to have the same size and sign"
_DECIMAL_REASON = (
    "MySQL needs the DECIMAL columns of a foreign key to have the same precision and scale"
)
_LOB_REASON = "MySQL cannot put a TEXT or BLOB column in a foreign key"
_STRING_REASON = (
    "MySQL needs the two columns of a foreign key to be strings of one kind: character "
    "strings in the same character set, or binary strings (their lengths may differ)"
)


def _kind(aurora_type: str) -> tuple[object, ...]:
    """What MySQL compares on a foreign key column: a string's kind but not its length,
    an integer's size and sign, a DECIMAL's precision and scale."""
    if is_enum_type(aurora_type.strip()):
        return ("set",) if aurora_type.lstrip()[:3].upper() == "SET" else ("enum",)
    parsed = parse_type(aurora_type)
    if parsed is None:
        return ("other", " ".join(aurora_type.upper().split()))
    base, a, b, post = parsed
    unsigned = "unsigned" in post.split()
    if base in _INTEGERS:
        return ("integer", _INTEGERS[base], unsigned)
    if base in _DECIMALS:
        return ("decimal", a if a is not None else 10, b or 0, unsigned)
    if base in _CHARACTER:
        return ("string", "utf8mb4")
    if base in _NATIONAL:
        return ("string", "utf8mb3")
    if base in _BINARY:
        return ("string", "binary")
    if base in _LOBS:
        return ("lob",)
    if base in _TEMPORAL:
        return ("temporal", _TEMPORAL[base])
    if base == "float":
        return ("double",) if a is not None and a > 24 else ("float",)
    if base in ("double", "double precision", "real"):
        return ("double",)
    return (base,)


def refused_pair(child: str, parent: str) -> str | None:
    """Why Aurora MySQL refuses a foreign key from a ``child`` column to a ``parent``
    column (a sentence with no full stop), or None when it accepts the pair."""
    mine, theirs = _kind(child), _kind(parent)
    if mine[0] == "lob" or theirs[0] == "lob":
        return _LOB_REASON
    if mine == theirs:
        return None
    if mine[0] == theirs[0] == "integer":
        return _INTEGER_REASON
    if mine[0] == theirs[0] == "decimal":
        return _DECIMAL_REASON
    if mine[0] == theirs[0] == "string":
        return _STRING_REASON
    return "MySQL refuses a foreign key between columns of these two types"


def is_lob(aurora_type: str) -> bool:
    return bool(_kind(aurora_type)[0] == "lob")
