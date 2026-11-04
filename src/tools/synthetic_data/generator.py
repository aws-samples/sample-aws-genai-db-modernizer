# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthetic rows for one collector table, from its metadata only (#1763).

No sample rows are read (#387). Leading rows carry the edge values that break type
conversions; the rest are seeded random values. The same table and seed give the
same rows.

A column whose type the generator does not model (an interval, a geometry, ...) still
gets a placeholder text value, and its name goes into ``unchecked_source_columns``, so a
backend lists every output column that reads it as unchecked instead of reporting the
placeholder as a conversion failure.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

DEFAULT_ROWS = 20
# Very long text types such as longtext would otherwise build huge strings.
MAX_EDGE_TEXT = 16384
_SIGNED_MAX = {"tinyint": 127, "smallint": 32767, "mediumint": 8388607, "int": 2147483647,
               "integer": 2147483647, "bigint": 9223372036854775807}  # fmt: skip
_UNSIGNED_MAX = {"tinyint": 255, "smallint": 65535, "mediumint": 16777215, "int": 4294967295,
                 "integer": 4294967295, "bigint": 18446744073709551615}  # fmt: skip
_INT_WORDS = tuple(_UNSIGNED_MAX)
_FOUR_BYTE = "\U0001d11e"  # a 4-byte UTF-8 character


@dataclass(frozen=True)
class SyntheticTable:
    table_id: str
    rows: list[dict[str, object]]
    edge_rows: int
    row_count_basis: Literal["collector", "default"]
    unchecked_source_columns: tuple[str, ...] = ()


_TEXT_WORDS = ("varchar", "char", "text", "tinytext", "mediumtext", "longtext")


def _kind(column: Mapping[str, Any]) -> str:
    t = str(column.get("data_type") or "").lower().strip()
    norm = str(column.get("normalized_data_type") or "").lower()
    if re.fullmatch(r"tinyint\(1\)", t):
        return "bool1"
    if t.split("(")[0].split()[0] in _INT_WORDS if t else False:
        return "uint" if "unsigned" in t else "int"
    if t.startswith(("decimal", "numeric")) or norm == "decimal":
        return "decimal"
    if t.startswith(("float", "double", "real")) or norm == "float":
        return "float"
    if t == "date" or norm == "date":
        return "date"
    if t.startswith(("datetime", "timestamp")) or norm == "datetime":
        return "datetime"
    first = t.split("(")[0].split()[0] if t else ""
    if first in ("bool", "boolean"):
        return "bool"
    if first in ("time", "timetz"):
        return "time"
    if first == "uuid":
        return "uuid"
    if first in ("json", "jsonb"):
        return "json"
    if first == "year":
        return "year"
    if first == "bit":
        return "bit"
    if norm in ("string", "text") or first in _TEXT_WORDS or t.startswith("character"):
        return "string"
    return "other"


def _int_word(column: Mapping[str, Any]) -> str:
    t = str(column.get("data_type") or "").lower().strip()
    return t.split("(")[0].split()[0] if t else ""


def edge_values(column: Mapping[str, Any], *, dialect: str) -> list[object]:
    kind = _kind(column)
    if kind == "date":
        return ["0000-00-00"] if dialect == "mysql" else ["infinity", "-infinity"]
    if kind == "datetime":
        return ["0000-00-00 00:00:00"] if dialect == "mysql" else ["infinity", "-infinity"]
    if kind == "uint":
        return [_UNSIGNED_MAX[_int_word(column)]]
    if kind == "bool1" and dialect == "mysql":
        return [2]
    declared = _declared_precision(column)
    if kind == "decimal" and declared is not None:
        p, s = declared
        return [Decimal("9" * (p - s) + ("." + "9" * s if s else ""))]
    if kind == "string" and column.get("max_length"):
        return [_FOUR_BYTE * min(int(column["max_length"]), MAX_EDGE_TEXT)]
    return []


def _declared_precision(column: Mapping[str, Any]) -> tuple[int, int] | None:
    """Precision and scale: the collector's fields, else parsed from ``data_type``."""
    if column.get("numeric_precision"):
        return int(column["numeric_precision"]), int(column.get("numeric_scale") or 0)
    m = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", str(column.get("data_type") or ""))
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def _precision(column: Mapping[str, Any]) -> tuple[int, int]:
    return _declared_precision(column) or (10, 0)


def _random_value(kind: str, column: Mapping[str, Any], rng: random.Random, i: int) -> object:
    if kind in ("int", "uint"):
        table = _UNSIGNED_MAX if kind == "uint" else _SIGNED_MAX
        return rng.randint(1, min(10**6, table.get(_int_word(column), 10**6)))
    if kind == "bool1":
        return rng.choice([0, 1])
    if kind == "decimal":
        p, s = _precision(column)
        whole = rng.randint(0, 10 ** (p - s - 1)) if p > s else 0
        return Decimal(f"{whole}.{rng.randint(0, 10**s - 1):0{s}d}") if s else Decimal(whole)
    if kind == "float":
        return round(rng.uniform(0, 1000), 3)
    if kind in ("date", "datetime"):
        d = f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        return d if kind == "date" else d + " 12:00:00"
    if kind == "string":
        return f"s{i}-{rng.randint(0, 9999)}"[: int(column.get("max_length") or 32)]
    if kind == "bool":
        return rng.choice(["true", "false"])
    if kind == "time":
        return f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
    if kind == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if kind == "json":
        return json.dumps({"k": i})
    if kind == "year":
        return str(rng.randint(1970, 2030))
    if kind == "bit":
        return rng.choice(["0", "1"])
    return f"v{i}"  # an unmodelled kind: the column is listed as unchecked


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = digits[n % 36]
    while n >= 36:
        n //= 36
        out = digits[n % 36] + out
    return out


def _key_value(
    kind: str, column: Mapping[str, Any], i: int, total: int, edges: list[object]
) -> object:
    """A unique key of the column's own kind, distinct from its edge values."""
    if kind in ("int", "uint", "bool1"):
        key = i + 1
        return key + total + 1 if key in edges else key
    if kind in ("date", "datetime"):
        day = (datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).isoformat()
        return day if kind == "date" else day + " 00:00:00"
    if kind == "decimal":
        p, s = _precision(column)
        dec = Decimal(i + 1)
        return dec if p - s >= len(str(i + 1)) else dec.scaleb(-s)
    if kind == "float":
        return float(i + 1)
    if kind == "string":
        return _base36(i)
    if kind == "bool":
        return ["true", "false"][i]
    if kind == "bit":
        return str(i)
    if kind == "time":
        return f"{i // 3600 % 24:02d}:{i % 3600 // 60:02d}:{i % 60:02d}"
    if kind == "uuid":
        return str(uuid.UUID(int=i + 1))
    if kind == "json":
        return json.dumps({"k": i})
    if kind == "year":
        return str(1901 + i)
    return f"k{i}"


def generate_rows(table: Mapping[str, Any], *, dialect: str, seed: int = 0,
                  max_rows: int = DEFAULT_ROWS) -> SyntheticTable:  # fmt: skip
    columns = list(table.get("columns") or [])
    pk = set(table.get("primary_key") or [])
    rng = random.Random(
        f"{table.get('table_id')}:{seed}"
    )  # nosec B311 -- seeded synthetic rows, not security-sensitive
    row_count = table.get("row_count")
    n = DEFAULT_ROWS if row_count is None else max(1, min(int(row_count), max_rows))
    edges = [(c["column_name"], v) for c in columns for v in edge_values(c, dialect=dialect)]
    total = max(n, len(edges))
    capacity = min(
        [36 ** int(c["max_length"]) for c in columns
         if c["column_name"] in pk and _kind(c) == "string" and c.get("max_length")]
        + [2 for c in columns if c["column_name"] in pk and _kind(c) in ("bool", "bit")]
        + [255 for c in columns if c["column_name"] in pk and _kind(c) == "year"]
        + [86400 for c in columns if c["column_name"] in pk and _kind(c) == "time"],
        default=total * 10**6,
    )  # fmt: skip
    total = min(total, capacity)
    col_edges = {
        c["column_name"]: [v for name, v in edges if name == c["column_name"]] for c in columns
    }

    def build(i: int) -> dict[str, object]:
        row: dict[str, object] = {}
        for c in columns:
            name, kind = c["column_name"], _kind(c)
            if i < len(edges) and edges[i][0] == name:
                row[name] = edges[i][1]
            elif name in pk:
                row[name] = _key_value(kind, c, i, total, col_edges[name])
            elif c.get("nullable") and i % 5 == 4:
                row[name] = None
            else:
                row[name] = _random_value(kind, c, rng, i)
        return row

    rows = [build(i) for i in range(total)]
    for c in columns:
        name = c["column_name"]
        if not c.get("nullable") or name in pk or any(r[name] is None for r in rows):
            continue
        spare = [j for j in range(len(rows)) if not (j < len(edges) and edges[j][0] == name)]
        if not spare and len(rows) < capacity:
            rows.append(build(len(rows)))
            spare = [len(rows) - 1]
        if spare:
            rows[spare[-1]][name] = None
    unchecked = tuple(c["column_name"] for c in columns if _kind(c) == "other")
    return SyntheticTable(str(table.get("table_id")), rows, len(edges),
                          "default" if row_count is None else "collector", unchecked)  # fmt: skip
