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

Two optional column fields shape the values further when a collection has them (the
collector contract does not carry them yet): ``null_rate`` (0 to 1, the share of rows
that are NULL in a nullable column) and ``min_value`` / ``max_value`` (a numeric
column's range: random values stay inside it and both bounds become edge values).

Rows for a real engine (#1942). ``loadable=True`` keeps only edge values the source
engine accepts with its default settings, so the rows load: no MySQL zero dates, ASCII
instead of 4-byte text at the maximum length, MySQL ``bit`` and ``boolean`` as integers, one
of its own values in a MySQL ``ENUM`` or ``SET`` column, a short byte string in a
``bytea``, ``BLOB`` or ``BINARY`` column, and NULL in a
nullable column of an unmodelled type. ``generate_related_rows`` builds the rows for a
set of tables at once: parents before children, every foreign key pointing at a parent
row, primary keys and unique indexes kept unique, and a foreign-key cycle broken at a
nullable foreign key (left NULL).
"""

from __future__ import annotations

import datetime
import heapq
import json
import random
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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
    # Boolean source columns (``true``/``false`` text): a backend feeds them to Aurora
    # MySQL as 1/0, the way a boolean reaches a MySQL TINYINT (#1830).
    boolean_source_columns: tuple[str, ...] = ()
    # Byte-string source columns given as PostgreSQL hex text (``\x0a``): a backend
    # decodes them to bytes before it checks or loads a byte target (#1830).
    hex_source_columns: tuple[str, ...] = ()


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
    # Checked after the string kinds, so an ENUM a collector normalizes as a string keeps
    # the string values generate_rows_golden.json recorded (#1942).
    if first in ("enum", "set") and enum_values(column):
        return "enum"
    if first in _BYTE_TYPES:
        return "bytes"
    return "other"


def _int_word(column: Mapping[str, Any]) -> str:
    t = str(column.get("data_type") or "").lower().strip()
    return t.split("(")[0].split()[0] if t else ""


_ENUM_TYPE = re.compile(r"^\s*(?:enum|set)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_QUOTED = re.compile(r"'((?:[^']|'')*)'")


def enum_values(column: Mapping[str, Any]) -> list[str]:
    """A MySQL ``ENUM`` or ``SET`` column's values, from its type; [] for any other."""
    m = _ENUM_TYPE.match(str(column.get("data_type") or ""))
    return [v.replace("''", "'") for v in _QUOTED.findall(m.group(1))] if m else []


_BYTE_TYPES = {"bytea", "blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary"}


def is_bytes(column: Mapping[str, Any]) -> bool:
    """A byte-string column: PostgreSQL ``bytea``, MySQL ``BLOB``, ``BINARY``, ..."""
    return _int_word(column) in _BYTE_TYPES


def _byte_width(column: Mapping[str, Any], dialect: str) -> int | None:
    """A MySQL ``BINARY(n)`` or ``VARBINARY(n)`` column's length in bytes, else None."""
    word = _int_word(column)
    if dialect != "mysql" or word not in ("binary", "varbinary"):
        return None
    if column.get("max_length"):
        return int(column["max_length"])
    m = re.search(r"\(\s*(\d+)\s*\)", str(column.get("data_type") or ""))
    return int(m.group(1)) if m else (1 if word == "binary" else None)


def _as_stored(column: Mapping[str, Any], value: object, dialect: str) -> object:
    """A MySQL ``BINARY(n)`` value as the engine stores it: padded with zero bytes to n."""
    width = _byte_width(column, dialect)
    if _int_word(column) != "binary" or width is None or not isinstance(value, str):
        return value
    return value + "\x00" * max(0, width - len(value.encode("utf-8")))


def _bytes_value(column: Mapping[str, Any], dialect: str, rng: random.Random) -> str:
    """A short byte string, never longer than the column: PostgreSQL's hex input form,
    or MySQL text bytes."""
    if dialect == "postgresql":
        return f"\\x{rng.randint(0, 255):02x}"
    n = _byte_width(column, dialect) or int(column.get("max_length") or 2)
    return "ab"[: max(1, min(2, n))]


def column_kind(column: Mapping[str, Any]) -> str:
    """The value kind the generator models a column (or parameter) type as: ``int``,
    ``uint``, ``bool1`` (MySQL ``tinyint(1)``), ``decimal``, ``float``, ``date``,
    ``datetime``, ``bool``, ``time``, ``uuid``, ``json``, ``year``, ``bit``, ``string``,
    ``enum`` (a MySQL ``ENUM`` or ``SET`` that lists its values), ``bytes`` (``bytea``,
    ``BLOB``, ``BINARY``, ...), or ``other`` for a type it does not model."""
    return _kind(column)


def integer_bounds(column: Mapping[str, Any]) -> tuple[int, int]:
    """The lowest and highest value of an integer column's type (``int`` when unknown)."""
    word = _int_word(column)
    if "unsigned" in str(column.get("data_type") or "").lower():
        return 0, _UNSIGNED_MAX.get(word, _UNSIGNED_MAX["int"])
    high = _SIGNED_MAX.get(word, _SIGNED_MAX["int"])
    return -high - 1, high


def edge_values(column: Mapping[str, Any], *, dialect: str, loadable: bool = False) -> list[object]:
    """The column's edge values: its type's own, then its range's bounds (when given).

    ``loadable`` keeps only values the source engine takes with its default settings.
    """
    out = _type_edges(column, dialect=dialect, loadable=loadable)
    for bound in _range(column):
        if bound not in out:
            out.append(bound)
    return out


def _type_edges(column: Mapping[str, Any], *, dialect: str, loadable: bool) -> list[object]:
    kind = _kind(column)
    if kind == "date":
        if dialect == "mysql":
            return [] if loadable else ["0000-00-00"]
        return ["infinity", "-infinity"]
    if kind == "datetime":
        if dialect == "mysql":
            return [] if loadable else ["0000-00-00 00:00:00"]
        return ["infinity", "-infinity"]
    if kind == "uint":
        return [_UNSIGNED_MAX[_int_word(column)]]
    if kind == "bool1" and dialect == "mysql":
        return [2]
    declared = _declared_precision(column)
    if kind == "decimal" and declared is not None:
        p, s = declared
        return [Decimal("9" * (p - s) + ("." + "9" * s if s else ""))]
    if kind == "string" and column.get("max_length"):
        char = "z" if loadable else _FOUR_BYTE
        return [char * min(int(column["max_length"]), MAX_EDGE_TEXT)]
    if kind == "bytes" and not loadable and _byte_width(column, dialect) is not None:
        width = _byte_width(column, dialect) or 0  # the full length, in bytes
        return ["z" * min(width, MAX_EDGE_TEXT)]
    return []


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _range(column: Mapping[str, Any]) -> list[object]:
    """A numeric column's ``min_value`` and ``max_value``, in the column's own kind."""
    kind = _kind(column)
    if kind not in ("int", "uint", "decimal", "float"):
        return []
    bounds = [_number(column.get("min_value")), _number(column.get("max_value"))]
    out: list[object] = []
    for b in bounds:
        if b is None:
            continue
        if kind in ("int", "uint"):
            out.append(int(b))
        elif kind == "float":
            out.append(float(b))
        else:
            out.append(b)
    return out


def _null_rate(column: Mapping[str, Any]) -> float | None:
    rate = _number(column.get("null_rate"))
    return None if rate is None else min(1.0, max(0.0, float(rate)))


def _null_at(column: Mapping[str, Any], i: int) -> bool:
    """Whether row ``i`` of a nullable column is NULL: every fifth row, or the
    collection's ``null_rate`` spread evenly over the rows."""
    rate = _null_rate(column)
    if rate is None:
        return i % 5 == 4
    return int((i + 1) * rate) > int(i * rate)


def _declared_precision(column: Mapping[str, Any]) -> tuple[int, int] | None:
    """Precision and scale: the collector's fields, else parsed from ``data_type``."""
    if column.get("numeric_precision"):
        return int(column["numeric_precision"]), int(column.get("numeric_scale") or 0)
    m = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", str(column.get("data_type") or ""))
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def _precision(column: Mapping[str, Any]) -> tuple[int, int]:
    return _declared_precision(column) or (10, 0)


def decimal_precision(column: Mapping[str, Any]) -> tuple[int, int]:
    """A decimal column's precision and scale: declared, else ``(10, 0)``."""
    return _precision(column)


def _random_value(
    kind: str, column: Mapping[str, Any], rng: random.Random, i: int, dialect: str
) -> object:
    bounds = _range(column)
    if len(bounds) == 2 and kind in ("int", "uint", "decimal", "float"):
        low, high = sorted(Decimal(str(b)) for b in bounds)
        value = low + (high - low) * Decimal(rng.randint(0, 1000)) / 1000
        if kind in ("int", "uint"):
            return int(value)
        if kind == "float":
            return float(value)
        return value.quantize(Decimal(1).scaleb(-_precision(column)[1]))
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
    if kind == "enum":
        return rng.choice(enum_values(column))
    if kind == "bytes":
        return _bytes_value(column, dialect, rng)
    return f"v{i}"  # an unmodelled kind: the column is listed as unchecked


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = digits[n % 36]
    while n >= 36:
        n //= 36
        out = digits[n % 36] + out
    return out


def _key_value(
    kind: str, column: Mapping[str, Any], i: int, total: int, edges: list[object], dialect: str
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
    if kind == "enum":
        return enum_values(column)[i]
    if kind == "bytes":
        if dialect == "postgresql":
            digits = f"{i:x}"
            return "\\x" + "0" * (len(digits) % 2) + digits
        return _base36(i)
    return f"k{i}"


def generate_rows(table: Mapping[str, Any], *, dialect: str, seed: int = 0,
                  max_rows: int = DEFAULT_ROWS, unique: Iterable[str] = (),
                  loadable: bool = False) -> SyntheticTable:  # fmt: skip
    """Rows for one table. ``unique`` names more columns that get unique key values
    (a unique index's), like the primary key's; ``loadable`` see the module docstring."""
    columns = list(table.get("columns") or [])
    pk = set(table.get("primary_key") or []) | set(unique)
    rng = random.Random(
        f"{table.get('table_id')}:{seed}"
    )  # nosec B311 -- seeded synthetic rows, not security-sensitive
    row_count = table.get("row_count")
    n = DEFAULT_ROWS if row_count is None else max(1, min(int(row_count), max_rows))
    edges = [
        (c["column_name"], v)
        for c in columns
        for v in edge_values(c, dialect=dialect, loadable=loadable)
    ]
    total = max(n, len(edges))
    capacity = min(
        [36 ** int(c["max_length"]) for c in columns
         if c["column_name"] in pk and _kind(c) in ("string", "bytes") and c.get("max_length")]
        + [len(enum_values(c)) for c in columns if c["column_name"] in pk and _kind(c) == "enum"]
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
                row[name] = _key_value(kind, c, i, total, col_edges[name], dialect)
            elif c.get("nullable") and _null_at(c, i):
                row[name] = None
            elif loadable and kind == "other" and c.get("nullable"):
                row[name] = None  # a placeholder would not load into an unmodelled type
            elif loadable and kind in ("bit", "bool") and dialect == "mysql":
                row[name] = rng.choice([0, 1])
            else:
                row[name] = _random_value(kind, c, rng, i, dialect)
            if kind == "bytes" and not loadable:  # loadable rows: the engine pads them
                row[name] = _as_stored(c, row[name], dialect)
        return row

    rows = [build(i) for i in range(total)]
    for c in columns:
        name = c["column_name"]
        if not c.get("nullable") or name in pk or any(r[name] is None for r in rows):
            continue
        if _null_rate(c) == 0.0:
            continue  # the collection says the column is never NULL
        spare = [j for j in range(len(rows)) if not (j < len(edges) and edges[j][0] == name)]
        if not spare and len(rows) < capacity:
            rows.append(build(len(rows)))
            spare = [len(rows) - 1]
        if spare:
            rows[spare[-1]][name] = None
    unchecked = tuple(c["column_name"] for c in columns if _kind(c) == "other")
    booleans = tuple(c["column_name"] for c in columns if _kind(c) == "bool")
    hexes = tuple(
        c["column_name"] for c in columns if _kind(c) == "bytes" and dialect == "postgresql"
    )
    return SyntheticTable(str(table.get("table_id")), rows, len(edges),
                          "default" if row_count is None else "collector", unchecked,
                          booleans, hexes)  # fmt: skip


# --------------------------------------------------------------------------- related tables


@dataclass(frozen=True)
class RelatedRows:
    """Rows for a set of tables that load together (#1942).

    - ``order``: every table id, parents before children (the insert order); the
      tables of a foreign-key cycle in id order.
    - ``tables``: the rows to insert, by table id (empty for an unseeded table).
    - ``held_back``: per ``reserve`` table, rows that are valid but not inserted (for a
      test ``INSERT``); the same number of the table's last inserted rows is referenced
      by no foreign key (for a test ``DELETE``).
    - ``cycle_breaks``: ``(table id, constraint)`` of each foreign key left NULL to
      break a cycle.
    - ``ignored_references``: ``(table id, constraint)`` of each foreign key whose
      table is not in the set; its columns get no parent value.
    - ``unseeded``: table id to the reason it has no rows.
    """

    order: tuple[str, ...]
    tables: dict[str, SyntheticTable]
    held_back: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    cycle_breaks: tuple[tuple[str, str], ...] = ()
    ignored_references: tuple[tuple[str, str], ...] = ()
    unseeded: dict[str, str] = field(default_factory=dict)


class TableIndex:
    """Tables by id and by lower-case name, built once, so resolving every foreign key
    of a large collection stays linear."""

    def __init__(self, by_id: Mapping[str, Mapping[str, Any]]) -> None:
        self.by_id = by_id
        self.by_name: dict[str, list[str]] = {}
        for i, t in sorted(by_id.items()):
            name = str(t.get("table_name") or i.rpartition(".")[2]).lower()
            self.by_name.setdefault(name, []).append(i)

    def resolve(self, ref: str, child_id: str) -> str | None:
        """The table id ``ref`` names: an id, the only table of that name, or the one of
        that name in the child's schema; None otherwise."""
        if ref in self.by_id:
            return ref
        same = self.by_name.get(ref.rpartition(".")[2].lower(), [])
        if len(same) == 1:
            return same[0]
        schema = child_id.rpartition(".")[0].lower()
        mine = [i for i in same if i.rpartition(".")[0].lower() == schema]
        return mine[0] if len(mine) == 1 else None


def resolve_reference(
    ref: str, child: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """The table id a foreign key's ``referenced_table`` names (an id, or a table name
    in the child's schema or the only table of that name), or None. For many lookups
    over the same tables, build a ``TableIndex`` once instead."""
    return TableIndex(by_id).resolve(ref, str(child.get("table_id") or ""))


def _sccs(nodes: Sequence[str], edges: Mapping[str, set[str]]) -> list[list[str]]:
    """Strongly connected components (iterative Tarjan), each sorted."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                elif nxt in on:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    top = stack.pop()
                    on.discard(top)
                    comp.append(top)
                    if top == node:
                        break
                out.append(sorted(comp))
    return out


def _insert_order(nodes: Sequence[str], parents: Mapping[str, set[str]]) -> list[list[str]]:
    """Components in insert order: a component after every component it references,
    ties by smallest table id."""
    comps = _sccs(nodes, parents)
    comp_of = {n: k for k, comp in enumerate(comps) for n in comp}
    needs: dict[int, set[int]] = {k: set() for k in range(len(comps))}
    for child, ps in parents.items():
        for p in ps:
            if comp_of[p] != comp_of[child]:
                needs[comp_of[child]].add(comp_of[p])
    feeds: dict[int, set[int]] = {k: set() for k in needs}
    for k, needed in needs.items():
        for p_comp in needed:
            feeds[p_comp].add(k)
    waiting = {k: len(ps) for k, ps in needs.items()}
    ready = [(comps[k][0], k) for k, w in waiting.items() if w == 0]
    heapq.heapify(ready)
    out: list[list[str]] = []
    while ready:
        _, k = heapq.heappop(ready)
        out.append(comps[k])
        for f in feeds[k]:
            waiting[f] -= 1
            if waiting[f] == 0:
                heapq.heappush(ready, (comps[f][0], f))
    return out


def _unique_keys(table: Mapping[str, Any]) -> list[list[str]]:
    """The primary key, then every plain unique index's columns, each once."""
    keys: list[list[str]] = []
    if table.get("primary_key"):
        keys.append(list(table["primary_key"]))
    for idx in table.get("indexes") or []:
        if not idx.get("is_unique") or idx.get("predicate") or idx.get("has_expressions"):
            continue
        cols = list(idx.get("columns") or [])
        if cols and cols not in keys:
            keys.append(cols)
    return keys


def _dedupe(rows: list[dict[str, object]], keys: list[list[str]]) -> list[dict[str, object]]:
    """The rows whose every non-NULL key is not taken by an earlier row."""
    seen: list[set[tuple[str, ...]]] = [set() for _ in keys]
    out = []
    for row in rows:
        tuples = [tuple(repr(row.get(c)) for c in key) for key in keys]
        clash = any(
            t in seen[k]
            for k, t in enumerate(tuples)
            if all(row.get(c) is not None for c in keys[k])
        )
        if clash:
            continue
        for k, t in enumerate(tuples):
            seen[k].add(t)
        out.append(row)
    return out


def generate_related_rows(
    tables: Sequence[Mapping[str, Any]],
    *,
    dialect: str,
    seed: int = 0,
    max_rows: int = DEFAULT_ROWS,
    reserve: Mapping[str, int] | None = None,
    skip: Mapping[str, str] | None = None,
    loadable: bool = True,
) -> RelatedRows:
    """Rows for ``tables`` that load together, in foreign-key order (see ``RelatedRows``).

    Each table gets at most ``max_rows`` rows (plus its edge rows), fewer when its row
    count is lower. ``reserve`` asks for that many held-back rows of a table (and as many
    unreferenced inserted ones); ``skip`` names tables that must get no rows, with why.
    A table with a NOT NULL foreign key to a table with no rows gets none either.
    """
    reserve = dict(reserve or {})
    unseeded = dict(skip or {})
    by_id = {str(t["table_id"]): t for t in tables}
    index = TableIndex(by_id)
    nodes = sorted(by_id)
    fks: dict[str, list[tuple[Mapping[str, Any], str | None]]] = {}
    parents: dict[str, set[str]] = {n: set() for n in nodes}
    ignored: list[tuple[str, str]] = []
    for tid in nodes:
        resolved = []
        for fk in sorted(
            by_id[tid].get("foreign_keys") or [], key=lambda f: str(f.get("constraint_name"))
        ):
            parent = index.resolve(str(fk.get("referenced_table") or ""), tid)
            if parent is None:
                ignored.append((tid, str(fk.get("constraint_name"))))
            elif parent != tid:
                parents[tid].add(parent)
            resolved.append((fk, parent))
        fks[tid] = resolved
    components = _insert_order(nodes, parents)
    position = {n: i for i, n in enumerate(n for comp in components for n in comp)}
    nullable = {
        tid: {c["column_name"] for c in by_id[tid].get("columns") or [] if c.get("nullable")}
        for tid in nodes
    }
    broken: set[tuple[str, str]] = set()
    for tid in nodes:
        for fk, parent in fks[tid]:
            if parent is None or parent == tid or position[parent] < position[tid]:
                continue
            name = str(fk.get("constraint_name"))
            broken.add((tid, name))
            if not set(fk.get("columns") or []) <= nullable[tid]:
                unseeded.setdefault(
                    tid, f"foreign key {name} is on a cycle and has a NOT NULL column"
                )

    out: dict[str, SyntheticTable] = {}
    held: dict[str, list[dict[str, object]]] = {}
    usable: dict[str, list[dict[str, object]]] = {}
    for tid in (n for comp in components for n in comp):
        table = by_id[tid]
        keys = _unique_keys(table)
        extra = 2 * reserve.get(tid, 0)
        rows: list[dict[str, object]] = []
        base: SyntheticTable | None = None
        if tid not in unseeded:
            row_count = table.get("row_count")
            n = DEFAULT_ROWS if row_count is None else max(1, int(row_count))
            sized = dict(table, row_count=min(n, max_rows) + extra)
            single = [k[0] for k in keys[1:] if len(k) == 1]
            base = generate_rows(sized, dialect=dialect, seed=seed, max_rows=max_rows + extra,
                                 unique=single, loadable=loadable)  # fmt: skip
            strict = {
                c["column_name"]
                for c in table.get("columns") or []
                if not c.get("nullable") and not (loadable and (enum_values(c) or is_bytes(c)))
            }
            if loadable and (bad := sorted(set(base.unchecked_source_columns) & strict)):
                unseeded[tid] = (
                    f"column {bad[0]} is NOT NULL and of a type the generator does not model"
                )
            else:
                rows = [dict(r) for r in base.rows]
        if rows:
            rows = _with_parents(tid, rows, fks[tid], broken, nullable[tid], usable, unseeded)
        k = 0
        if rows:
            rows = _dedupe(rows, keys)
            k = min(reserve.get(tid, 0), max(0, (len(rows) - 1) // 2))
            # the held-back rows and the k inserted rows before them are pointed at by
            # no row, so a test INSERT and DELETE of them never meets a foreign key
            rows = _with_self_references(tid, rows, fks[tid], nullable[tid], len(rows) - 2 * k)
        if k:
            held[tid] = rows[len(rows) - k :]
            rows = rows[: len(rows) - k]
        usable[tid] = rows[: len(rows) - k] if k else rows
        if tid in unseeded:
            rows = []
            usable[tid] = []
        out[tid] = SyntheticTable(
            tid,
            rows,
            min(base.edge_rows, len(rows)) if base else 0,
            "default" if table.get("row_count") is None else "collector",
            base.unchecked_source_columns if base else (),
        )
    return RelatedRows(
        order=tuple(n for comp in components for n in comp),
        tables=out,
        held_back=held,
        cycle_breaks=tuple(sorted(broken)),
        ignored_references=tuple(ignored),
        unseeded=unseeded,
    )


def _with_parents(
    tid: str,
    rows: list[dict[str, object]],
    fks: list[tuple[Mapping[str, Any], str | None]],
    broken: set[tuple[str, str]],
    nullable: set[str],
    usable: Mapping[str, list[dict[str, object]]],
    unseeded: dict[str, str],
) -> list[dict[str, object]]:
    """Each foreign key's columns set from a parent row (mixed radix over the parents,
    so a key made of foreign keys stays unique), NULL where the row drew NULL, or left
    NULL to break a cycle."""
    stride = 1
    for fk, parent in fks:
        cols = list(fk.get("columns") or [])
        refs = list(fk.get("referenced_columns") or [])
        name = str(fk.get("constraint_name"))
        if parent is None or parent == tid or len(cols) != len(refs):
            continue
        all_null = set(cols) <= nullable
        if (tid, name) in broken:
            for row in rows:
                for c in cols:
                    row[c] = None
            continue
        pool = usable.get(parent) or []
        if not pool:
            if not all_null:
                unseeded.setdefault(tid, f"its parent {parent} has no rows")
                return []
            for row in rows:
                for c in cols:
                    row[c] = None
            continue
        for i, row in enumerate(rows):
            if all_null and any(row.get(c) is None for c in cols):
                for c in cols:
                    row[c] = None
                continue
            source = pool[(i // stride) % len(pool)]
            for c, r in zip(cols, refs, strict=True):
                row[c] = source.get(r)
        stride *= len(pool)
    return rows


def _with_self_references(
    tid: str,
    rows: list[dict[str, object]],
    fks: list[tuple[Mapping[str, Any], str | None]],
    nullable: set[str],
    targets: int,
) -> list[dict[str, object]]:
    """A foreign key to its own table: row i points at row (i - 1) // 2, never past the
    first ``targets`` rows, so the rest are pointed at by none; the first row is NULL,
    or itself when NOT NULL."""
    last = max(1, targets) - 1
    for fk, parent in fks:
        cols = list(fk.get("columns") or [])
        refs = list(fk.get("referenced_columns") or [])
        if parent != tid or len(cols) != len(refs):
            continue
        all_null = set(cols) <= nullable
        for i, row in enumerate(rows):
            values: list[object]
            if i == 0 and all_null:
                values = [None] * len(cols)
            else:
                target = rows[min((i - 1) // 2, last)] if i else row
                values = [target.get(r) for r in refs]
            for c, v in zip(cols, values, strict=True):
                row[c] = v
    return rows
