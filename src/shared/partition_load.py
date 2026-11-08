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
its most selective counted part. When every part is counted it also has at most their
product (several attributes or columns) or their sum (the entities of an item
collection), so the total over that ceiling is a lower bound on the busiest value, as a
``% n`` transform's is. When some part is not counted, only the most selective counted
part is known: a lower bound on the key's values, so the total over it bounds only the
average per key value, from above (#1993).

Each per-key figure says what it is (``per_key_measure``, #1993), so no figure passes
through as more than its evidence shows:

* ``measured``: every request on the entry (or a measured share of it) uses one key
  value, so the figure is that value's load;
* ``lower_bound``: the total spread evenly over a count the key's values cannot exceed
  (a unique key's rows, a column's cardinality, a ``% n`` transform, or the product or
  sum of every counted part). The busiest value carries at least the average, so a
  figure over the limit shows a key value over it;
* ``upper_bound``: the total over a count that is only a lower bound on the key's
  values (some part of the key is not counted): an upper bound on the average per key
  value that says nothing about the busiest value. It is the row's figure only when
  nothing firmer is known and it is under the limit; over the limit the entry is not
  measured, with the bound kept as its evidence (``per_key_upper_bound``). It never sets
  a severity, so an uncounted part of a key can turn what main rated HIGH into "not
  measured" (refined decision 1 on #1987); a counted part never lowers it;
* ``not_measured``: no per-key figure.

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

#: What a per-key figure is (#1993): see the module docstring.
KeyMeasure = Literal["measured", "lower_bound", "upper_bound", "not_measured"]
MEASURED: KeyMeasure = "measured"
LOWER_BOUND: KeyMeasure = "lower_bound"
UPPER_BOUND: KeyMeasure = "upper_bound"
UNMEASURED: KeyMeasure = "not_measured"

#: Whether a table whose row count reads 0 gives its unique keys one value (every request
#: on the one row a write creates) or leaves them not measured. The maintainer decides on
#: #2001; until then an estimate of 0 proves nothing, so it is not measured.
ZERO_ROW_TABLE_IS_ONE_KEY = False

#: What set a per-key estimate when a static-key entity's own share did (#2001 review S2).
STATIC_ENTITY = "static_entity"

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
    exact = table.get("row_count_basis") == "exact"
    if rows == 0 and not ZERO_ROW_TABLE_IS_ONE_KEY:
        # An estimate of 0 rows proves nothing about a key that takes requests.
        rows = None
    elif rows == 1 and not exact:
        # Nor does an estimated single row (#2001 review): only an exact count of one row
        # makes a key one value.
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
        if rows == 1 and name and table.get("row_count_basis") == "exact":
            card = 1  # an exactly counted one-row table: every column holds one value
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
        # (columns, distinct values, a unique key: one value per row)
        self._sets: dict[str, list[tuple[frozenset[str], int, bool]]] = {}
        for table, entries in (by_table or {}).items():
            rows = [
                (
                    frozenset(_lower(e.get("columns") or [])),
                    int(e.get("distinct_values") or 0),
                    e.get("basis") == UNIQUE_KEY,
                )
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
        statistics, a lower bound on the combination's count."""
        found = self.count(table, columns)
        return found.n if found else None

    def count(self, table: str, columns: Iterable[str]) -> _Count | None:
        """:meth:`of`'s count, with its bounds (#1993): exact for a unique key or one
        column; for several columns, at least the most selective one's count and, when
        every column is counted, at most the product of their counts."""
        wanted = frozenset(_lower(columns))
        if not wanted:
            return None
        sets = self._sets.get(normalize_table_name(table), [])
        # Columns that contain a unique key take one value per row: the rows' count.
        unique = [n for cols, n, is_unique in sets if is_unique and cols and cols <= wanted]
        if unique:
            return _Count(max(unique))
        single = {next(iter(cols)): n for cols, n, _ in sets if len(cols) == 1 and cols <= wanted}
        if not single:
            return None
        ceiling = _product(single.values()) if set(single) == wanted else None
        return _bounded(max(single.values()), ceiling)


# ---------------------------------------------------------------------------
# A design key's distinct values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Count:
    """A key's distinct values: ``n`` collected, at least ``n`` (``at_least``: the most
    selective counted part of a wider key, with ``ceiling`` the most it can take when
    every part is counted, #1993), or at most ``n`` (``at_most``: a ``% n`` transform or
    an ``IN`` list bounds it)."""

    n: int
    at_most: bool = False
    at_least: bool = False
    ceiling: int | None = None


ONE = _Count(1)


def _product(counts: Iterable[int]) -> int:
    out = 1
    for c in counts:
        out *= c
    return out


def _bounded(low: int, ceiling: int | None) -> _Count:
    """At least ``low`` values, at most ``ceiling`` (None: unknown); exact when equal."""
    if ceiling is not None and ceiling <= low:
        return _Count(low)
    return _Count(low, at_least=True, ceiling=ceiling)


def _high(count: _Count | None) -> int | None:
    """The most values ``count`` can take, or None when unknown."""
    if count is None:
        return None
    return count.ceiling if count.at_least else count.n


#: ``x % 32``, ``mod(x, 32)``, ``modulo 32``: the transform keeps at most that many values.
#: Only a ``%`` between two operands (``id % 32``, ``hash(x) % 4``) or a ``mod()`` call:
#: a printf format such as ``'%08d'`` has no left operand and is not a modulo.
_MODULO = re.compile(
    r"[\w)\]]\s*%\s*(\d+)\b|\bmod\s*\(\s*[^,()]+,\s*(\d+)\s*\)|\bmodulo\s+(\d+)\b",
    re.I,
)


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
    return values.count(str(attribute.get("source_table") or ""), columns)


def _template_count(entity: Mapping[str, Any], values: DistinctValues) -> _Count | None:
    template = str(entity.get("pk_template") or "")
    tokens = _TOKEN.findall(template)
    if not tokens:
        return ONE  # a static template: every item of the entity has the same key value
    if _modulo(template):
        return _Count(int(_modulo(template) or 1), at_most=True)
    return values.count(str(entity.get("source_table") or ""), tokens)


def _combine(parts: list[_Count | None]) -> _Count | None:
    """A key spelled by several attributes: constants add nothing. Collected parts give
    at least the most selective one's count, even next to an unknown part, and at most
    the product of the counts when every part is counted; bounded parts (``at_most``)
    give an upper bound only when every part has one (the product)."""
    rest = [p for p in parts if p != ONE]
    if not parts:
        return None
    if not rest:
        return ONE
    if len(rest) == 1:
        return rest[0]
    highs = [_high(p) for p in rest]
    ceiling = None if any(h is None for h in highs) else _product(h for h in highs if h)
    known = [p for p in rest if p is not None]
    if any(p.at_most for p in known):
        return _Count(ceiling, at_most=True) if ceiling else None
    return _bounded(max(p.n for p in known), ceiling) if known else None


def _union(entities: list[_Count | None]) -> _Count | None:
    """The item collection's key values over its entities (#1989 review): an entity with
    a static key carries its own load on that one key, so the collection is single-key;
    otherwise at least the most selective collected entity's count and, when every
    entity is counted, at most their sum (#1993); bounded ones add up to an upper bound
    when every entity is bounded."""
    if not entities:
        return None
    if any(e == ONE for e in entities):
        return ONE
    if len(entities) == 1:
        return entities[0]
    highs = [_high(e) for e in entities]
    ceiling = None if any(h is None for h in highs) else sum(h for h in highs if h)
    known = [e for e in entities if e is not None]
    collected = [e.n for e in known if not e.at_most]
    if collected:
        return _bounded(max(collected), ceiling)
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


_LITERAL_ITEM = re.compile(r"-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\"")


def _in_list_size(body: str) -> int | None:
    """How many literal key values an ``IN`` list names: a numeric range ``0..31`` is 32,
    ``'a', 'b', 'c'`` is 3. Only literal items count (#2001 review): a column name, a
    placeholder (``:id1``, ``?``, ``$1``), a range of placeholders, an ellipsis or prose
    (``post_id list``, ``post_id, ...``) says nothing about how many values a call
    reaches, so None."""
    span = re.fullmatch(r"\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*", body)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        return abs(high - low) + 1
    items = [i.strip() for i in body.split(",")]
    if not items or any(not i for i in items):
        return None
    for item in items:
        if not _LITERAL_ITEM.fullmatch(item) or _PLACEHOLDER.search(item.strip("'\"")):
            return None
    return len(items)


#: A placeholder inside a quoted key (``'USER#<id>'``, ``'USER#{id}'``, ``'#{id}'``,
#: ``':user_id'``, ``'${id}'``, ``'?'``, ``'$1'``): the quotes hold a template, whose
#: values follow the attribute's, not one literal value.
_PLACEHOLDER = re.compile(r"<[^<>]*>|\{[^{}]*\}|(?<![\w:]):\w+|\?|\$\d+|\$\{")


def pattern_key_values(key_condition: str, key_names: Iterable[str]) -> int | None:
    """How many partition key values one call of an access pattern can reach: 1 when its
    key condition pins a key attribute to a fully literal quoted value (one holding a
    placeholder is a template, never one value), k for an ``IN`` list of k values on
    one, else None (each call may use any value)."""
    names = [re.escape(n) for n in [*key_names, "PK"] if n]
    if not names or not key_condition:
        return None
    alt = "|".join(sorted(set(names), key=len, reverse=True))
    for literal in re.finditer(rf"\b(?:{alt})\s*=\s*(['\"])(.*?)\1", key_condition):
        joined = re.match(r"\s*(\|\||\+|\bCONCAT\b)", key_condition[literal.end() :], re.I)
        if not _PLACEHOLDER.search(literal.group(2)) and not joined:
            return 1  # a fully literal key value: every call uses it
    found = re.search(rf"\b(?:{alt})\s+IN\s*\(([^)]*)\)", key_condition, re.I)
    return _in_list_size(found.group(1)) if found else None


def _entry_patterns(entry: Mapping[str, Any], design: Mapping[str, Any]) -> list[Mapping]:
    """The access patterns behind ``entry``: same table, GSI and operation kind, and (when
    it names any) a contributing query."""
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
    return mine


def _share(patterns: list[Mapping], part: list[Mapping]) -> float:
    total = sum(float(p.get("design_rps") or 0) for p in patterns)
    if total:
        return sum(float(p.get("design_rps") or 0) for p in part) / total
    return len(part) / len(patterns) if patterns else 0.0


def _bounded_patterns(
    entry: Mapping[str, Any], design: Mapping[str, Any], key_names: list[str]
) -> list[tuple[float, int]]:
    """Each access pattern behind ``entry`` whose calls reach at most k key values, as
    (its share of the entry's patterns' ``design_rps``, k)."""
    mine = _entry_patterns(entry, design)
    out = []
    for p in mine:
        k = pattern_key_values(str(p.get("key_condition") or ""), key_names)
        if k:
            out.append((_share(mine, [p]), k))
    return out


def _static_split(
    entry: Mapping[str, Any],
    design: Mapping[str, Any],
    table: Mapping[str, Any] | None,
    values: DistinctValues,
) -> list[_Candidate] | None:
    """For an item collection with a static-key entity (S2 of the #2001 review): when the
    access patterns say what share of the load is the static entities' (patterns whose
    source tables are all theirs), that share on its one key and the rest spread over
    the other entities' values. None when there is no static entity next to a dynamic
    one, or no pattern gives the share (the whole entry is then one key)."""
    entities = [e for e in (table or {}).get("entities") or [] if isinstance(e, Mapping)]
    static = [e for e in entities if _template_count(e, values) == ONE]
    dynamic = [e for e in entities if e not in static]
    if not static or not dynamic:
        return None
    mine = _entry_patterns(entry, design)
    static_tables = {normalize_table_name(str(e.get("source_table") or "")) for e in static}
    on_static = [
        p
        for p in mine
        if p.get("source_tables")
        and {normalize_table_name(str(t)) for t in p["source_tables"]} <= static_tables
    ]
    if not on_static:
        return None
    share = _share(mine, on_static)
    total = _units(entry, partition_limit(entry.get("operation")))
    out = [_Candidate(total * share, 1, STATIC_ENTITY, MEASURED, "at_most")]
    if share < 1:
        rest = _union([_template_count(e, values) for e in dynamic])
        if rest is None:
            # The other entities' load is not measured (#2001 review): the static share is
            # only a lower bound on the busiest key value.
            out.append(_Candidate(0.0, 0, NOT_MEASURED, UNMEASURED, None))
        else:
            out += _spread(total * (1 - share), rest)
    return out


@dataclass(frozen=True)
class _Candidate:
    """One per-key figure: ``units`` on one key value over ``n`` values, what set it,
    what it is (``measure``) and how ``n`` bounds the key (``bound``)."""

    units: float
    n: int
    source: str
    measure: KeyMeasure
    bound: str | None


def _spread(units: float, count: _Count) -> list[_Candidate]:
    """``units`` spread evenly over ``count``'s values (#1993): one value carries it all
    (measured); over a count the key cannot exceed, the busiest value carries at least
    the average (lower bound); over a count that is only a lower bound, the figure bounds
    the average from above, next to the lower bound its ceiling gives when there is one."""
    if count.at_least:
        out = [_Candidate(units / count.n, count.n, "key_values", UPPER_BOUND, "collected")]
        if count.ceiling:
            out.append(
                _Candidate(
                    units / count.ceiling, count.ceiling, "key_values", LOWER_BOUND, "at_most"
                )
            )
        return out
    measure = MEASURED if count == ONE else LOWER_BOUND
    bound = "at_most" if count.at_most else "collected"
    return [_Candidate(units / count.n, count.n, "key_values", measure, bound)]


# ---------------------------------------------------------------------------
# One entry's load per key value
# ---------------------------------------------------------------------------


def _by_units(candidate: _Candidate) -> float:
    return candidate.units


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
    table, names, base = _design_key(design, table_name, gsi)
    count = key_count(design, table_name, gsi, values)
    split = _static_split(entry, design, table, values) if base else None
    unmeasured_rest = any(c.source == NOT_MEASURED for c in split or [])
    candidates = [c for c in split or [] if c.source != NOT_MEASURED]
    if count is not None and split is None:
        candidates += _spread(total, count)
    for share, k in _bounded_patterns(entry, design, names):
        measure_k: KeyMeasure = MEASURED if k == 1 else LOWER_BOUND
        candidates.append(_Candidate(total * share / k, k, "access_pattern", measure_k, "at_most"))
    # A measured figure or a lower bound leads; an upper bound on the average is only
    # evidence, and the row's figure only when nothing firmer is known and it is under
    # the limit (#1993).
    firm = [c for c in candidates if c.measure != UPPER_BOUND]
    upper = max((c for c in candidates if c.measure == UPPER_BOUND), key=_by_units, default=None)
    best = max(firm, key=_by_units, default=None)
    set_aside = None
    if best is None and upper is not None:
        if upper.units > limit:
            set_aside = upper
        else:
            best = upper
    elif best is not None and upper is not None and upper.units > max(limit, best.units):
        set_aside = upper
    if best is not None and unmeasured_rest and best.units <= limit:
        # Only a lower bound under the limit: the rest of the load is not measured.
        best = None
    if best is None:
        basis: KeyBasis = NOT_MEASURED
        per_key, n, source, measure, bound = None, None, None, UNMEASURED, None
    else:
        per_key, n, source = round(best.units, 4), best.n, best.source
        measure, bound = best.measure, best.bound
        basis = SINGLE_KEY if n == 1 and measure != UPPER_BOUND else DISTINCT_VALUES
    upper_bound = (
        None
        if set_aside is None
        else {
            "at_least_key_values": set_aside.n,
            "units_per_second": round(set_aside.units, 4),
            "pct_of_limit": pct_of_limit(set_aside.units, limit),
        }
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
        "per_key_measure": measure,
        "per_key_upper_bound": upper_bound,
        "query_ids": sorted({str(q) for q in entry.get("contributing_patterns") or [] if q}),
    }


def per_key_measure(row: Mapping[str, Any]) -> KeyMeasure:
    """A row's ``per_key_measure``; for a row written before #1993, read from its basis
    (a single key measured, an even spread a lower bound)."""
    stored = row.get("per_key_measure")
    for measure in (MEASURED, LOWER_BOUND, UPPER_BOUND, UNMEASURED):
        if stored == measure:
            return measure
    basis = row.get("per_key_basis")
    if basis == SINGLE_KEY:
        return MEASURED
    return LOWER_BOUND if basis == DISTINCT_VALUES else UNMEASURED


def over_key_limit(row: Mapping[str, Any]) -> bool:
    """The estimated busiest key value is above its limit: the one HIGH case. Only a
    measured figure or a lower bound shows it; an upper bound never does (#1993)."""
    pct = row.get("per_key_pct_of_limit")
    if per_key_measure(row) not in (MEASURED, LOWER_BOUND):
        return False
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
    "busiest_key",
    "DISTINCT_VALUES",
    "NOT_MEASURED",
    "SINGLE_KEY",
    "UNIQUE_KEY",
    "DistinctValues",
    "KeyBasis",
    "KeyMeasure",
    "LOWER_BOUND",
    "MEASURED",
    "UNMEASURED",
    "UPPER_BOUND",
    "hot_partition_rows",
    "key_distinct_count",
    "key_distinct_values",
    "over_key_limit",
    "partition_limit",
    "partition_load",
    "pct_of_limit",
    "per_key_measure",
]


def busiest_key(
    schema_designs: Mapping[str, Mapping[str, Any]] | None,
    source_tables: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The design entry whose estimated key value is the busiest share of its limit, over
    every entry of every design (not only those above the at-risk share): the one figure
    the risk register and the growth scenario both state (``facts.busiest_partition_key``,
    #2001 review S1). A figure that only bounds the average per key value is skipped
    (#1993). None when no entry's per-key load is measured."""
    values = DistinctValues.from_collector_tables(source_tables)
    best: dict[str, Any] | None = None
    seen: set[str] = set()
    for engine in sorted(schema_designs or {}, key=canonical_engine_id):
        canonical = canonical_engine_id(engine)
        design = (schema_designs or {})[engine] or {}
        if canonical in seen or not isinstance(design, Mapping):
            continue
        seen.add(canonical)
        for entry in design.get("hot_partition_analysis") or []:
            if not isinstance(entry, Mapping) or entry.get("operation") not in ("read", "write"):
                continue
            row = partition_load(entry, design, values, canonical)
            pct = row["per_key_pct_of_limit"]
            if row["per_key_measure"] == UPPER_BOUND:
                continue  # bounds only the average per key value, never the busiest (#1993)
            if pct is not None and (best is None or pct > best["per_key_pct_of_limit"]):
                best = row
    return best
