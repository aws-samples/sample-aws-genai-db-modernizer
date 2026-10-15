"""Each query's access-pattern class (ADR-031 Decision 14, Amendment 5, #728).

A query is classified by what it retrieves, not by how its SQL is written and
never by what other queries on its tables do. The facts come from the SQL facade
(:func:`src.tools.sql.facade.access_facts`: the tables each scope reads, the
columns it pins to a value or bounds by a range, the column pairs that join two
of its tables, its order, row limit and aggregates); the keys come from the
collector's schema (:class:`SchemaKeys`). Like :mod:`query_shape`, this module
reports a fact about one query, never a score and never an engine.

The seven scored classes, in severity order (``SCORED_CLASSES``; when a statement
shows several shapes, in different scopes or in one, the later class wins):

1. ``key_lookup``: one row (or a batch) by a full primary or unique key, whatever its
   row limit; ``ORDER BY <primary key>`` with a single-row limit at any offset and no
   pinned key (#857); or an ``INSERT`` (a write by key; an ``INSERT ... SELECT`` also
   takes the class of what it reads).
2. ``key_scoped``: rows addressed by a non-unique key value, or a join in which every
   table is reached by key from a table pinned by a key predicate (a key-anchored
   join: one item collection, not a relational need). A ``LIMIT 1`` on such a read
   stays here: only a primary-key read is a key lookup. An aggregate with no ``GROUP
   BY`` over such rows, on one table or over a key-anchored join (``COUNT(*) WHERE
   order_id = ?``), combines one item collection, so it stays here too.
3. ``key_range``: a range or an order over a key with no anchoring value
   (``BETWEEN``, ``ORDER BY id LIMIT n``), a read with no predicate at all (the
   whole key range), and any read that skips locked rows (``SKIP LOCKED``,
   ``READPAST``: an unknown row, Decision 14).
4. ``ad_hoc_filter``: filters on non-key attributes with no anchoring key, an
   ``OR`` of predicates, a prefix ``LIKE`` on a non-key column, and an unkeyed
   bulk write.
5. ``unanchored_join``: a join in which some table is not reached by key from a
   key predicate.
6. ``aggregation``: ``GROUP BY`` with an aggregate function (a ``GROUP BY`` that
   combines nothing only de-duplicates, like ``DISTINCT``, and keeps the class of
   the rows it reads), an aggregate over rows no key anchors, or a
   windowed aggregate (``SUM(...) OVER``; a ranking or offset window function such
   as ``ROW_NUMBER`` combines nothing, the facade's ``SqlFacts`` definition).
7. ``text_search``: in a scope no key anchors, a ``LIKE`` whose pattern starts with a
   wildcard or is a bind parameter (``LIKE ?``: the digest hides the pattern), a
   regular-expression match, a similarity function or a full-text match. On keyed
   rows the same predicate only filters one item collection, so it never outranks
   the key anchor; the reason says so.

Two labels are not access patterns and take no class score: ``utility`` (a
statement that reads or writes no table: ``SET``, ``SHOW``, transaction control,
DDL, ``SELECT 1``, ``SELECT to_tsvector($1)``) and ``unparsed`` (a data statement
the facade cannot parse, such as MySQL ``REPLACE INTO``, which sqlglot reads only
as a generic command; no shape is guessed from it). A statement the facade cannot
parse is ``utility`` when the collector did not type it as a data statement.

A key column is a primary-key column, a foreign-key column, a unique index's
leading column, or the leading column of a non-unique, non-text index unless the
column is a date or timestamp (an index on a point in time orders rows, it does not
address an entity) or the collector's statistics show it low-cardinality
(``_LOW_CARDINALITY_SHARE``). With no statistics the index rule holds, and the
reason names the unconfirmed key. A unique key is the primary key or the columns of
a unique index. A table outside the collected schema has no keys. A join edge is
a ``col = col`` term (or ``JOIN ... USING``) with a key column on at least one
side. A derived table or CTE counts as pinned when its own scope is a key lookup
or a key-scoped read and it is not the optional side of a ``LEFT``/``FULL`` join.
A join to a function of the row (``unnest``, ``CROSS APPLY OPENJSON``) reads no
other table, so it needs no key path. An ``ORDER BY`` with no row limit only sorts
what is read, so it does not change the class; with a limit it decides which rows
are read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.agents.referee.table_resolution import TableNameResolver
from src.tools.sql.facade import AccessFacts, ColumnFact, ScopeFacts, ScopeSource, access_facts

KEY_LOOKUP = "key_lookup"
KEY_SCOPED = "key_scoped"
KEY_RANGE = "key_range"
AD_HOC_FILTER = "ad_hoc_filter"
UNANCHORED_JOIN = "unanchored_join"
AGGREGATION = "aggregation"
TEXT_SEARCH = "text_search"
UTILITY = "utility"
UNPARSED = "unparsed"

SCORED_CLASSES: tuple[str, ...] = (
    KEY_LOOKUP,
    KEY_SCOPED,
    KEY_RANGE,
    AD_HOC_FILTER,
    UNANCHORED_JOIN,
    AGGREGATION,
    TEXT_SEARCH,
)
_SEVERITY = {name: rank for rank, name in enumerate(SCORED_CLASSES)}

CLASS_LABELS: dict[str, str] = {
    KEY_LOOKUP: "key lookup",
    KEY_SCOPED: "key-scoped 1:N read",
    KEY_RANGE: "range or ordered read by key",
    AD_HOC_FILTER: "ad hoc filter",
    UNANCHORED_JOIN: "unanchored join",
    AGGREGATION: "aggregation",
    TEXT_SEARCH: "text search",
    UTILITY: "utility statement",
    UNPARSED: "unparsed statement",
}

# Index types that are not an access key for an equality or a range.
_NON_KEY_INDEX_TYPES = frozenset({"fulltext", "gin", "gist", "spatial"})
# Column types that are a point in time: an index on one orders rows, it does not
# address an entity, so its leading column is not a key.
_TEMPORAL_TYPES = frozenset({"date", "datetime", "timestamp", "time"})
# A non-unique index's leading column whose distinct values (the collector's
# ``cardinality``) number at most this share of the table's rows addresses a large
# slice of the table per value, not an item collection, so it is not a key.
_LOW_CARDINALITY_SHARE = 0.01


@dataclass(frozen=True)
class TableKeys:
    """The keys of one collected table (lower-case column names)."""

    primary_key: tuple[str, ...]
    unique_keys: tuple[frozenset[str], ...]
    key_columns: frozenset[str]
    columns: frozenset[str]
    # Keys that only a non-unique index's leading column makes keys, with no
    # cardinality statistics to confirm them (the reason text says so).
    unconfirmed_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SchemaKeys:
    """Every collected table's keys, and the resolver from a written name to its id."""

    tables: Mapping[str, TableKeys] = field(default_factory=dict)
    resolver: TableNameResolver | None = None

    @classmethod
    def from_collector(cls, collector_output: Mapping) -> SchemaKeys:
        schema = collector_output.get("database_schema") or {}
        tables: dict[str, TableKeys] = {}
        for table in schema.get("tables") or []:
            table_id = str(table.get("table_id") or "")
            if table_id:
                tables[table_id] = _table_keys(table)
        return cls(tables=tables, resolver=TableNameResolver.from_collector(collector_output))

    def table_id(self, source: ScopeSource) -> str | None:
        if source.table is None or self.resolver is None:
            return None
        return self.resolver.resolve(source.table.written) or self.resolver.resolve(
            source.table.name
        )


def _lower(columns: Iterable) -> tuple[str, ...]:
    return tuple(str(c).lower() for c in columns or [] if c)


def _is_temporal(column: Mapping) -> bool:
    normalized = str(column.get("normalized_data_type") or "").lower()
    if normalized:
        return normalized in _TEMPORAL_TYPES
    native = str(column.get("data_type") or "").lower()
    return "date" in native or "time" in native


def _is_low_cardinality(column: Mapping, row_count: object) -> bool | None:
    """True or False from the collector's statistics; None when it has none."""
    cardinality = column.get("cardinality")
    if not isinstance(cardinality, int) or not isinstance(row_count, int) or row_count <= 0:
        return None
    return cardinality <= row_count * _LOW_CARDINALITY_SHARE


def _table_keys(table: Mapping) -> TableKeys:
    primary_key = _lower(table.get("primary_key") or [])
    unique: list[frozenset[str]] = [frozenset(primary_key)] if primary_key else []
    key_columns: set[str] = set(primary_key)
    for fk in table.get("foreign_keys") or []:
        key_columns.update(_lower(fk.get("columns") or []))
    by_name = {str(c.get("column_name") or "").lower(): c for c in table.get("columns") or []}
    index_keys: set[str] = set()
    unconfirmed: set[str] = set()
    for index in table.get("indexes") or []:
        if str(index.get("index_type") or "").lower() in _NON_KEY_INDEX_TYPES:
            continue
        columns = _lower(index.get("columns") or [])
        if not columns:
            continue
        if index.get("is_unique") or index.get("is_primary"):
            unique.append(frozenset(columns))
            key_columns.add(columns[0])
            continue
        column = by_name.get(columns[0], {})
        low = _is_low_cardinality(column, table.get("row_count"))
        if _is_temporal(column) or low:
            continue
        index_keys.add(columns[0])
        if low is None:
            unconfirmed.add(columns[0])
    return TableKeys(
        primary_key=primary_key,
        unique_keys=tuple(dict.fromkeys(unique)),
        key_columns=frozenset(key_columns | index_keys),
        columns=frozenset(n for n in by_name if n),
        unconfirmed_keys=frozenset(unconfirmed - key_columns),
    )


@dataclass(frozen=True)
class AccessClassification:
    """A query's class and a short, plain-language reason."""

    name: str
    reason: str

    @property
    def label(self) -> str:
        return CLASS_LABELS[self.name]


@dataclass
class _ScopeTables:
    """One scope's sources resolved against the schema."""

    keys: SchemaKeys
    facts: ScopeFacts
    ids: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for source in self.facts.sources:
            self.ids[source.alias] = self.keys.table_id(source)
            if source.table is not None:
                self.ids.setdefault(source.table.name.lower(), self.ids[source.alias])

    def table_keys(self, alias: str) -> TableKeys | None:
        table_id = self.ids.get(alias)
        return self.keys.tables.get(table_id) if table_id else None

    def owner(self, column: ColumnFact) -> str | None:
        """The alias of the source a column belongs to, or None when unknown."""
        if column.qualifier is not None:
            return column.qualifier if column.qualifier in self.ids else None
        aliases = [s.alias for s in self.facts.sources if not s.function]
        if len(aliases) == 1:
            return aliases[0]
        owners = [
            a for a in aliases if (k := self.table_keys(a)) is not None and column.name in k.columns
        ]
        return owners[0] if len(owners) == 1 else None

    def is_key(self, alias: str | None, column: str) -> bool:
        if alias is None:
            return False
        keys = self.table_keys(alias)
        return keys is not None and column in keys.key_columns

    def name(self, alias: str) -> str:
        table_id = self.ids.get(alias)
        return table_id.rsplit(".", 1)[-1] if table_id else alias


def _scope_class(
    facts: ScopeFacts, keys: SchemaKeys, derived_classes: Mapping[int, str]
) -> tuple[str, str] | None:
    """``(class, reason)`` for one scope, or None when it reads no table."""
    if not any(not s.function for s in facts.sources):
        return None
    scope = _ScopeTables(keys, facts)
    # A function of the row (unnest, OPENJSON) reads no other table: no key path needed.
    aliases = [s.alias for s in facts.sources if not s.function]
    pinned: dict[str, set[str]] = {a: set() for a in aliases}
    for column in facts.equalities:
        owner = scope.owner(column)
        if owner in pinned:
            pinned[owner].add(column.name)
    anchored = {a for a, cols in pinned.items() if any(scope.is_key(a, c) for c in cols)}
    for source in facts.sources:
        if (
            source.derived is not None
            and not source.optional
            and derived_classes.get(source.derived) in (KEY_LOOKUP, KEY_SCOPED)
        ):
            anchored.add(source.alias)
    unique = {
        a
        for a in anchored
        if (k := scope.table_keys(a)) is not None and any(u <= pinned[a] for u in k.unique_keys)
    }
    ranged = {
        owner
        for column in facts.ranges
        if (owner := scope.owner(column)) is not None and scope.is_key(owner, column.name)
    }

    # A key-anchored scope: one table pinned by a key value, or a join in which every
    # table is reached by key from a pinned one.
    reached = _reached(scope, facts, anchored) if len(aliases) > 1 else set(anchored)
    keyed = bool(anchored) and reached >= set(aliases)
    note = _key_note(scope, pinned, anchored)
    pattern_match = facts.text_search or facts.like_parameter
    if pattern_match and not keyed:
        return TEXT_SEARCH, "text or pattern match with no key anchoring the rows"
    if pattern_match:
        note += "; its pattern match only filters the keyed rows"

    # A GROUP BY that combines no values only de-duplicates the rows read (like
    # DISTINCT), so the class is the class of those rows.
    if facts.group_by and facts.aggregate:
        return AGGREGATION, "groups rows and combines their values (GROUP BY)"

    if len(aliases) > 1:
        if keyed:
            anchors = ", ".join(sorted({scope.name(a) for a in anchored}))
            what = "aggregate over a key-anchored join" if facts.aggregate else "key-anchored join"
            return KEY_SCOPED, f"{what}: every table reached by key from {anchors}{note}"
        if facts.aggregate:
            return AGGREGATION, "aggregates over a join no key anchors"
        missing = sorted({scope.name(a) for a in set(aliases) - reached})
        return UNANCHORED_JOIN, f"join with no key path to {', '.join(missing)}"

    alias = aliases[0]
    table = scope.name(alias)
    table_keys = scope.table_keys(alias)
    if facts.aggregate:
        if anchored:
            return KEY_SCOPED, f"aggregate over the rows one key value of {table} addresses{note}"
        return AGGREGATION, f"aggregate over {table} with no anchoring key"
    if anchored:
        if facts.skip_locked:
            return KEY_RANGE, "skips locked rows, so the row it returns is unknown"
        if alias in unique:
            return KEY_LOOKUP, f"one row of {table} by its full unique key{note}"
        return KEY_SCOPED, f"rows of {table} addressed by a non-unique key value{note}"
    if ranged:
        range_note = _key_note(scope, {alias: {c.name for c in facts.ranges}}, {alias})
        return KEY_RANGE, f"range over a key of {table}{range_note}"
    if facts.has_where or facts.other_filters:
        return AD_HOC_FILTER, f"filters {table} on non-key attributes"
    # Without a row limit the order only sorts what is read; it picks no rows.
    order = [c.name if c is not None else None for c in facts.order_by] if facts.limit else []
    primary_key = list(table_keys.primary_key) if table_keys else []
    if order:
        if primary_key and order == primary_key and facts.limit == "1":
            if facts.skip_locked:
                return KEY_RANGE, "skips locked rows, so the row it returns is unknown"
            return KEY_LOOKUP, f"one row of {table} in primary-key order"
        if all(o is not None and scope.is_key(alias, o) for o in order[:1]):
            return KEY_RANGE, f"ordered read over a key of {table}"
        return AD_HOC_FILTER, f"orders {table} by non-key attributes"
    return KEY_RANGE, f"reads all of {table} (no predicate)"


def _key_note(scope: _ScopeTables, pinned: Mapping[str, set[str]], anchored: set[str]) -> str:
    """Says so when the only keys used are index leading columns with no statistics."""
    used = [
        (a, c)
        for a in anchored
        if (keys := scope.table_keys(a)) is not None
        for c in pinned.get(a, ())
        if c in keys.key_columns
    ]
    if used and all(c in scope.table_keys(a).unconfirmed_keys for a, c in used):  # type: ignore[union-attr]
        columns = ", ".join(sorted({f"{scope.name(a)}.{c}" for a, c in used}))
        return (
            f" (key {columns}: an index's leading column, with no cardinality statistics "
            "to confirm it)"
        )
    return ""


def _reached(scope: _ScopeTables, facts: ScopeFacts, anchored: set[str]) -> set[str]:
    """The aliases reachable from ``anchored`` over key join edges."""
    edges: dict[str, set[str]] = {}
    for left, right in facts.joins:
        a, b = scope.owner(left), scope.owner(right)
        if a is None or b is None or a == b:
            continue
        if scope.is_key(a, left.name) or scope.is_key(b, right.name):
            edges.setdefault(a, set()).add(b)
            edges.setdefault(b, set()).add(a)
    reached = set(anchored)
    frontier = list(anchored)
    while frontier:
        for nxt in edges.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return reached


_DATA_QUERY_TYPES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"})


def classify_facts(
    facts: AccessFacts, keys: SchemaKeys, query_type: str | None = None
) -> AccessClassification:
    """The class of a statement from its facade facts (see the module docstring).

    ``query_type`` (the collector's) tells a statement sqlglot reads as a generic
    command apart: a ``SHOW`` is a utility statement, a ``REPLACE INTO`` (type
    ``INSERT``) is a write the facade could not read, so it is unparsed.
    """
    data = str(query_type or "").upper() in _DATA_QUERY_TYPES
    if facts.parsed_by != "sqlglot":
        if not data:
            return AccessClassification(UTILITY, "reads or writes no table data")
        return AccessClassification(UNPARSED, f"not parsed ({facts.fallback_reason})")
    reads_a_table = any(not s.function for scope in facts.scopes for s in scope.sources)
    if not reads_a_table and facts.kind in ("select", "other"):
        return AccessClassification(UTILITY, "reads or writes no table data")
    found: list[tuple[str, str]] = []
    if facts.windowed_aggregate:
        found.append((AGGREGATION, "windowed aggregate"))
    if facts.kind == "insert":
        found.append((KEY_LOOKUP, "inserts rows by key"))
    # Inner scopes first, so a derived table's class is known when its reader is classified.
    derived: dict[int, str] = {}
    for index in range(len(facts.scopes) - 1, -1, -1):
        scope_facts = facts.scopes[index]
        if facts.kind in ("update", "delete") and index == 0 and not scope_facts.has_where:
            if len(scope_facts.sources) <= 1:
                found.append((AD_HOC_FILTER, "writes every row (no predicate)"))
                continue
        result = _scope_class(scope_facts, keys, derived)
        if result is None:
            continue
        derived[index] = result[0]
        found.append(result)
    if not found:
        return AccessClassification(UTILITY, "reads or writes no table data")
    name, reason = max(found, key=lambda item: _SEVERITY[item[0]])
    return AccessClassification(name, reason)


def classify_query(
    query: Mapping, keys: SchemaKeys, source_engine: str | None
) -> AccessClassification:
    """The access-pattern class of one collected query."""
    facts = access_facts(str(query.get("query_text") or ""), source_engine or "")
    return classify_facts(facts, keys, str(query.get("query_type") or ""))
