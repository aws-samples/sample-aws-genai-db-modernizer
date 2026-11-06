# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB partition load, estimated per partition key value (#1989).

DynamoDB's partition throughput limits (3,000 read units and 1,000 write units per
second, ``DynamoDBPartitionLimits``) apply to one partition key value. A design's
``hot_partition_analysis`` entry is the load on a whole table or index: the total over
all of its key values. Compared with one key value's limit it is an upper bound, reached
only if every request used the same key value, so it never sets a risk's severity on its
own. This module estimates the load on one key value from what the collection holds:

* ``single_key``: every item has the same key value (the key attribute is a constant,
  ``ConstantCalculation`` (#1531), an entity's ``pk_template`` has no column token, or
  the key's source columns hold one distinct value), so every request on the table or
  index uses that value: the busiest key value carries the whole total;
* ``distinct_values``: the key's source columns have a collected number of distinct
  values (a primary key or unique index: the table's row count; else the collector's
  column cardinality), and the total spread evenly over them is the estimate. The
  collection holds no per-value counts, so skew across values is not measured;
* ``not_measured``: the collection holds neither (for example a non-unique column of a
  MySQL source, which collects no column cardinality): the output says "per-key load
  not measured" and the total stays the only figure, as an upper bound.

A key over several attributes (a multi-attribute GSI key, or an item collection whose
entities spell the key from different columns) has at least as many distinct values as
its most selective known part, so that count is the estimate's divisor: a lower bound on
the key's distinct values, which can only raise the per-key estimate.

The percentages are never capped: 303% of one key value's limit is printed as 303%.

No model, network or Strands dependency; the facts layer and the schema-design tools
both import it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from src.contracts.design_metrics import DynamoDBPartitionLimits
from src.shared.engine_names import canonical_engine_id
from src.shared.row_counts import known_row_count
from src.shared.table_keys import normalize_table_name

KeyBasis = Literal["single_key", "distinct_values", "not_measured"]
SINGLE_KEY: KeyBasis = "single_key"
DISTINCT_VALUES: KeyBasis = "distinct_values"
NOT_MEASURED: KeyBasis = "not_measured"

#: Whether a table whose row count reads 0 gives its unique keys one value (every request
#: on the one row a write creates) or leaves them not measured. The maintainer decides on
#: #2001; until then an estimate of 0 proves nothing, so it is not measured.
ZERO_ROW_TABLE_IS_ONE_KEY = False

#: How a column set's distinct-value count was collected.
UNIQUE_KEY = "unique_key"
COLUMN_STATISTICS = "column_statistics"

_TOKEN = re.compile(r"\{([^{}]+)\}")


def _limits() -> DynamoDBPartitionLimits:
    return DynamoDBPartitionLimits()


def partition_limit(operation: object) -> float:
    """One key value's limit for a ``read`` or ``write`` entry (ValueError otherwise)."""
    limits = _limits()
    if operation == "read":
        return limits.read_units_per_second
    if operation == "write":
        return limits.write_units_per_second
    raise ValueError(f"a hot-partition entry's operation is read or write, not {operation!r}")


def pct_of_limit(units: float, limit: float) -> float:
    """``units / limit × 100``, never capped, to four decimals."""
    return round(units / limit * 100, 4) if limit else 0.0


# ---------------------------------------------------------------------------
# Distinct values of a source table's columns
# ---------------------------------------------------------------------------


def _lower(columns: Iterable[object]) -> list[str]:
    return [str(c).strip().lower() for c in columns or [] if str(c or "").strip()]


def key_distinct_values(table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The column sets of a collector table whose number of distinct values is collected.

    A primary key or unique index has as many distinct values as the table has rows (the
    collector's row count, at least 1: a key that takes writes holds a value); a column
    with a collected ``cardinality`` has that many. Sorted, one entry per column set,
    as ``{"columns", "distinct_values", "basis"}``. Empty when the table has no row
    count and no column statistics.
    """
    out: dict[tuple[str, ...], dict[str, Any]] = {}
    rows = known_row_count(table)
    if rows == 0 and not ZERO_ROW_TABLE_IS_ONE_KEY:
        # An estimate of 0 rows proves nothing about a key that takes requests.
        rows = None
    if rows is not None:
        unique: list[list[str]] = []
        if table.get("primary_key"):
            unique.append(_lower(table.get("primary_key") or []))
        for index in table.get("indexes") or []:
            if isinstance(index, Mapping) and (index.get("is_unique") or index.get("is_primary")):
                unique.append(_lower(index.get("columns") or []))
        for columns in unique:
            if columns:
                key = tuple(sorted(set(columns)))
                out[key] = {
                    "columns": list(key),
                    "distinct_values": max(rows, 1),
                    "basis": UNIQUE_KEY,
                }
    for column in table.get("columns") or []:
        if not isinstance(column, Mapping):
            continue
        card = column.get("cardinality")
        name = _lower([column.get("column_name")])
        if rows == 1 and name:
            card = 1  # a one-row table: every column holds one value
        # A cardinality of 0 or none is not a count (#1989 review): not measured.
        if not name or isinstance(card, bool) or not isinstance(card, int) or card <= 0:
            continue
        key = (name[0],)
        if key not in out:
            out[key] = {
                "columns": [name[0]],
                "distinct_values": min(card, rows) if rows else card,
                "basis": COLUMN_STATISTICS,
            }
    return [out[k] for k in sorted(out)]


class DistinctValues:
    """Look up the distinct values of a source table's columns, by table id or name."""

    def __init__(self, by_table: Mapping[str, Iterable[Mapping[str, Any]]] | None = None):
        self._sets: dict[str, list[tuple[frozenset[str], int]]] = {}
        for table, entries in (by_table or {}).items():
            rows = [
                (frozenset(_lower(e.get("columns") or [])), int(e.get("distinct_values") or 0))
                for e in entries or []
                if isinstance(e, Mapping) and e.get("columns")
            ]
            self._sets.setdefault(normalize_table_name(str(table)), []).extend(rows)

    @classmethod
    def from_collector_tables(cls, tables: Iterable[Mapping[str, Any]] | None) -> DistinctValues:
        """From collector tables (full-contract ``database_schema.tables`` or ``Agent*``)."""
        return cls(
            {
                str(t.get("table_id") or t.get("table_name") or ""): key_distinct_values(t)
                for t in tables or []
                if isinstance(t, Mapping)
            }
        )

    @classmethod
    def from_design_metrics(cls, metrics: Mapping[str, Any] | None) -> DistinctValues:
        """From a ``DesignMetricsContract``'s ``tables[].key_distinct_values`` (JSON)."""
        return cls(
            {
                str(t.get("table_id") or ""): t.get("key_distinct_values") or []
                for t in (metrics or {}).get("tables") or []
                if isinstance(t, Mapping)
            }
        )

    def of(self, table: str, columns: Iterable[str]) -> int | None:
        """Distinct values of ``columns`` together in ``table``, or None when not collected.

        Columns that contain a unique key have that key's count (the largest, when they
        contain several); otherwise the most selective single column with collected
        statistics is a lower bound on the combination's count."""
        wanted = frozenset(_lower(columns))
        if not wanted:
            return None
        sets = self._sets.get(normalize_table_name(table), [])
        unique = [n for cols, n in sets if cols and cols <= wanted]
        if unique:
            return max(unique)
        single = [n for cols, n in sets if len(cols) == 1 and cols <= wanted]
        return max(single) if single else None


# ---------------------------------------------------------------------------
# A design key's distinct values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Count:
    """A key's distinct values: ``n`` collected (exact, or a lower bound when it is the
    most selective known part of a wider key), or at most ``n`` (``at_most``: a ``% n``
    transform or an ``IN`` list bounds it)."""

    n: int
    at_most: bool = False


ONE = _Count(1)

#: ``x % 32``, ``mod(x, 32)``, ``modulo 32``: the transform keeps at most that many values.
_MODULO = re.compile(r"%\s*(\d+)|\bmod\s*\(\s*[^,()]+,\s*(\d+)\s*\)|\bmodulo\s+(\d+)", re.I)


def _modulo(spec: object) -> int | None:
    """The ``n`` of a ``% n`` transform in a calculation or transformation, if any."""
    found = _MODULO.search(json.dumps(spec, default=str))
    if not found:
        return None
    n = int(next(g for g in found.groups() if g))
    return n if n >= 1 else None


def _attributes(table: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out = [a for a in table.get("attributes") or [] if isinstance(a, Mapping)]
    for entity in table.get("entities") or []:
        if isinstance(entity, Mapping):
            out += [a for a in entity.get("attributes") or [] if isinstance(a, Mapping)]
    return out


def _attribute_count(attribute: Mapping[str, Any], values: DistinctValues) -> _Count | None:
    """A key attribute's distinct values. A constant is one value; a ``% n`` calculation
    or transformation (a write shard) has at most ``n``, whatever its source column's
    count; any other calculation, transformation or join is not measured."""
    calc = attribute.get("calculation")
    transform = attribute.get("transformation")
    if isinstance(calc, Mapping) and calc.get("type") == "constant":
        return ONE
    if calc or transform:
        n = _modulo(calc or transform)
        return _Count(n, at_most=True) if n else None
    if attribute.get("join"):
        return None
    source = attribute.get("source_column")
    columns = [source] if isinstance(source, str) else list(source or [])
    if not columns:
        return None
    n = values.of(str(attribute.get("source_table") or ""), columns)
    return _Count(n) if n else None


def _template_count(entity: Mapping[str, Any], values: DistinctValues) -> _Count | None:
    template = str(entity.get("pk_template") or "")
    tokens = _TOKEN.findall(template)
    if not tokens:
        return ONE  # a static template: every item of the entity has the same key value
    if _modulo(template):
        return _Count(int(_modulo(template) or 1), at_most=True)
    n = values.of(str(entity.get("source_table") or ""), tokens)
    return _Count(n) if n else None


def _combine(parts: list[_Count | None]) -> _Count | None:
    """A key spelled by several attributes: constants add nothing. Collected parts give
    a lower bound (the most selective one), even next to an unknown part; bounded parts
    (``at_most``) give an upper bound only when every part is known (their product)."""
    rest = [p for p in parts if p != ONE]
    if not parts:
        return None
    if not rest:
        return ONE
    known = [p for p in rest if p is not None]
    if any(p.at_most for p in known):
        if len(known) < len(rest):
            return None
        product = 1
        for p in known:
            product *= p.n
        return _Count(product, at_most=True)
    return _Count(max(p.n for p in known)) if known else None


def _union(entities: list[_Count | None]) -> _Count | None:
    """The item collection's key values over its entities (#1989 review): an entity with
    a static key carries its own load on that one key, so the collection is single-key;
    otherwise the most selective collected entity is a lower bound, and bounded ones add
    up to an upper bound when every entity is bounded."""
    if not entities:
        return None
    if any(e == ONE for e in entities):
        return ONE
    known = [e for e in entities if e is not None]
    collected = [e.n for e in known if not e.at_most]
    if collected:
        return _Count(max(collected))
    if known and len(known) == len(entities):
        return _Count(sum(e.n for e in known), at_most=True)
    return None


def _key_attribute_count(
    table: Mapping[str, Any], name: str, values: DistinctValues, base_key: bool
) -> _Count | None:
    """Distinct values of one key attribute over every item that carries it."""
    found = [_attribute_count(a, values) for a in _attributes(table) if a.get("name") == name]
    if base_key and table.get("entities"):
        # An item collection's key is spelled per entity by its pk_template.
        return _union(
            [_template_count(e, values) for e in table["entities"] if isinstance(e, Mapping)]
        )
    if not found:
        return None
    if all(f == ONE for f in found):
        return ONE
    return _union(found)


def _key_names(keys: object) -> list[str]:
    items = [keys] if isinstance(keys, Mapping) else keys if isinstance(keys, list) else []
    return [
        str(k.get("attribute_name"))
        for k in items
        if isinstance(k, Mapping) and k.get("attribute_name")
    ]


def _design_key(
    design: Mapping[str, Any], table_name: str, gsi_name: str | None
) -> tuple[Mapping[str, Any] | None, list[str], bool]:
    table = next(
        (
            t
            for t in design.get("table_definitions") or []
            if isinstance(t, Mapping) and str(t.get("table_name") or "") == table_name
        ),
        None,
    )
    if table is None:
        return None, [], True
    if not gsi_name:
        return table, _key_names(table.get("partition_key")), True
    gsi = next(
        (
            g
            for g in table.get("gsis") or []
            if isinstance(g, Mapping) and str(g.get("gsi_name") or "") == gsi_name
        ),
        None,
    )
    return (table, _key_names(gsi.get("partition_key")), False) if gsi else (None, [], False)


def key_count(
    design: Mapping[str, Any], table_name: str, gsi_name: str | None, values: DistinctValues
) -> _Count | None:
    """Distinct partition key values of a design table (``gsi_name`` None) or its GSI."""
    table, names, base = _design_key(design, table_name, gsi_name)
    if table is None or not names:
        return None
    return _combine([_key_attribute_count(table, n, values, base) for n in names])


def key_distinct_count(
    design: Mapping[str, Any], table_name: str, gsi_name: str | None, values: DistinctValues
) -> int | None:
    """:func:`key_count`'s number, or None when not measured."""
    count = key_count(design, table_name, gsi_name, values)
    return count.n if count else None


# ---------------------------------------------------------------------------
# What the access patterns say: one literal key value, or an IN list of k values
# ---------------------------------------------------------------------------

_READS = frozenset({"GetItem", "Query", "BatchGetItem", "TransactGetItems", "Scan"})


def _in_list_size(body: str) -> int | None:
    """``0..31`` is 32 values; ``a, b, c`` is 3."""
    span = re.fullmatch(r"\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*", body)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        return abs(high - low) + 1
    items = [i for i in body.split(",") if i.strip()]
    return len(items) or None


def pattern_key_values(key_condition: str, key_names: Iterable[str]) -> int | None:
    """How many partition key values one call of an access pattern can reach: 1 when its
    key condition pins a key attribute to a quoted literal, k for an ``IN`` list of k
    values on one, else None (each call may use any value)."""
    names = [re.escape(n) for n in [*key_names, "PK"] if n]
    if not names or not key_condition:
        return None
    alt = "|".join(sorted(set(names), key=len, reverse=True))
    if re.search(rf"\b(?:{alt})\s*=\s*['\"]", key_condition):
        return 1
    found = re.search(rf"\b(?:{alt})\s+IN\s*\(([^)]*)\)", key_condition, re.I)
    return _in_list_size(found.group(1)) if found else None


def _bounded_patterns(
    entry: Mapping[str, Any], design: Mapping[str, Any], key_names: list[str]
) -> list[tuple[float, int]]:
    """Each access pattern behind ``entry`` whose calls reach at most k key values, as
    (its share of the entry's patterns' ``design_rps``, k)."""
    wanted = {str(q) for q in entry.get("contributing_patterns") or [] if q}
    read = entry.get("operation") == "read"
    gsi = entry.get("gsi_name") or None
    mine = [
        p
        for p in design.get("access_patterns") or []
        if isinstance(p, Mapping)
        and p.get("table_name") == entry.get("table_name")
        and (p.get("gsi_name") or None) == gsi
        and (p.get("operation") in _READS) == read
        and (not wanted or wanted & {str(q) for q in p.get("query_ids") or []})
    ]
    total = sum(float(p.get("design_rps") or 0) for p in mine)
    out = []
    for p in mine:
        k = pattern_key_values(str(p.get("key_condition") or ""), key_names)
        if k:
            share = float(p.get("design_rps") or 0) / total if total else 1 / len(mine)
            out.append((share, k))
    return out


# ---------------------------------------------------------------------------
# One entry's load per key value
# ---------------------------------------------------------------------------


def _units(entry: Mapping[str, Any], limit: float) -> float:
    try:
        return float(entry.get("rcu_or_wcu_per_second"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # A design without the units: its own (possibly capped) utilization.
        return float(entry.get("utilization_pct") or 0.0) * limit / 100


def partition_load(
    entry: Mapping[str, Any],
    design: Mapping[str, Any],
    values: DistinctValues,
    engine: str = "",
) -> dict[str, Any]:
    """One ``hot_partition_analysis`` entry, with its load per key value estimated.

    ``total_*`` is the entry's own load (the table or index total, always an upper bound
    for one key value, ``total_is_upper_bound``) and ``per_key_*`` the estimate: the
    total over the key's distinct values (``distinct_key_bound`` ``at_most`` when a
    ``% n`` transform bounds them), raised by any access pattern whose calls reach at
    most k values (a quoted literal key, an ``IN`` list: its share of the load over k).
    None when neither is known (``per_key_basis`` ``not_measured``). Raises ValueError
    for an operation that is neither ``read`` nor ``write``."""
    operation = entry.get("operation")
    limit = partition_limit(operation)
    total = round(_units(entry, limit), 4)
    gsi = entry.get("gsi_name") or None
    table_name = str(entry.get("table_name") or "")
    _table, names, _base = _design_key(design, table_name, gsi)
    count = key_count(design, table_name, gsi, values)
    candidates: list[tuple[float, int, str]] = []
    if count is not None:
        candidates.append((total / count.n, count.n, "key_values"))
    for share, k in _bounded_patterns(entry, design, names):
        candidates.append((total * share / k, k, "access_pattern"))
    if not candidates:
        basis, per_key, n, source = NOT_MEASURED, None, None, None
    else:
        best = max(candidates, key=lambda c: c[0])
        per_key, n, source = round(best[0], 4), best[1], best[2]
        basis = SINGLE_KEY if n == 1 else DISTINCT_VALUES
    bound = (
        "at_most"
        if source == "access_pattern" or (source == "key_values" and count and count.at_most)
        else ("collected" if source else None)
    )
    return {
        "engine": canonical_engine_id(engine),
        "table_name": table_name,
        "gsi_name": str(gsi) if gsi else None,
        "operation": operation,
        "partition_limit": limit,
        "total_units_per_second": total,
        "total_pct_of_key_limit": pct_of_limit(total, limit),
        "total_is_upper_bound": True,
        "distinct_key_values": n,
        "distinct_key_bound": bound,
        "per_key_source": source,
        "per_key_units_per_second": per_key,
        "per_key_pct_of_limit": None if per_key is None else pct_of_limit(per_key, limit),
        "per_key_basis": basis,
        "query_ids": sorted({str(q) for q in entry.get("contributing_patterns") or [] if q}),
    }


def over_key_limit(row: Mapping[str, Any]) -> bool:
    """The estimated busiest key value is above its limit: the one HIGH case."""
    pct = row.get("per_key_pct_of_limit")
    return isinstance(pct, (int, float)) and pct > 100


def hot_partition_rows(
    schema_designs: Mapping[str, Mapping[str, Any]] | None,
    source_tables: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every design entry whose total is above the at-risk share of one key value's
    limit, with its per-key estimate (:func:`partition_load`), by engine then table.

    ``source_tables`` are the collector's tables (``database_schema.tables``); without
    them every key reads as not measured unless the design makes it a single key."""
    threshold = _limits().at_risk_threshold_pct
    values = DistinctValues.from_collector_tables(source_tables)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for engine in sorted(schema_designs or {}, key=canonical_engine_id):
        canonical = canonical_engine_id(engine)
        if canonical in seen:
            continue
        seen.add(canonical)
        design = (schema_designs or {})[engine] or {}
        if not isinstance(design, Mapping):
            continue
        for entry in design.get("hot_partition_analysis") or []:
            if not isinstance(entry, Mapping) or entry.get("operation") not in ("read", "write"):
                continue
            row = partition_load(entry, design, values, canonical)
            if row["total_pct_of_key_limit"] > threshold:
                rows.append(row)
    return sorted(
        rows,
        key=lambda r: (r["engine"], r["table_name"], r["gsi_name"] or "", r["operation"]),
    )


__all__ = [
    "COLUMN_STATISTICS",
    "DISTINCT_VALUES",
    "NOT_MEASURED",
    "SINGLE_KEY",
    "UNIQUE_KEY",
    "DistinctValues",
    "KeyBasis",
    "hot_partition_rows",
    "key_distinct_count",
    "key_distinct_values",
    "over_key_limit",
    "partition_limit",
    "partition_load",
    "pct_of_limit",
]
