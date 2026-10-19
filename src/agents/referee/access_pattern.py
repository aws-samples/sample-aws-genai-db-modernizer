"""Each query's access-pattern class (ADR-031 Decision 14, Amendment 5, #728).

A query is classified by what it retrieves, not by how its SQL is written and
never by what other queries on its tables do. The facts come from the SQL facade
(:func:`src.tools.sql.facade.access_facts`, read once per query through
:meth:`src.tools.sql.query_facts.QueryFacts.access_facts`, #934: the tables each scope reads, the
columns it pins to a value or bounds by a range, the column pairs that join two
of its tables, its order, row limit and aggregates); the keys come from the
collector's schema (:class:`SchemaKeys`). Like :mod:`query_shape`, this module
reports a fact about one query, never a score and never an engine.

The eight scored classes, in severity order (``SCORED_CLASSES``; when a statement
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
3. ``reference_read``: every row of a small table (a collected row count of at most
   ``SMALL_TABLE_MAX_ROWS``), with no predicate and no row limit: reference data such
   as feature flags, settings or a lookup list (#920).
4. ``key_range``: a range or an order over a key with no anchoring value
   (``BETWEEN``, ``ORDER BY id LIMIT n``), a read with no predicate at all of any
   other table (the whole key range), and any read that skips locked rows (``SKIP
   LOCKED``, ``READPAST``: an unknown row, Decision 14).
5. ``ad_hoc_filter``: filters on non-key attributes with no anchoring key, an
   ``OR`` of predicates, a prefix ``LIKE`` on a non-key column, and an unkeyed
   bulk write.
6. ``unanchored_join``: a join in which some table is not reached by key from a
   key predicate.
7. ``aggregation``: ``GROUP BY`` with an aggregate function (a ``GROUP BY`` that
   combines nothing only de-duplicates, like ``DISTINCT``, and keeps the class of
   the rows it reads), an aggregate over rows no key anchors, or a
   windowed aggregate (``SUM(...) OVER``; a ranking or offset window function such
   as ``ROW_NUMBER`` combines nothing, the facade's ``SqlFacts`` definition).
8. ``text_search``: in a scope no key anchors, a ``LIKE`` whose pattern starts with a
   wildcard, a regular-expression match, a similarity function or a full-text match.
   On keyed rows the same predicate only filters one item collection, so it never
   outranks the key anchor; the reason says so. A ``LIKE`` whose whole pattern is a
   bind parameter (``LIKE ?``: the digest hides the pattern) shows no text search, so
   no text search is visible, so its scope is classed by its other predicates (#912, #920).

Two labels are not access patterns and take no class score: ``utility`` (a
statement that reads or writes no table: ``SET``, ``SHOW``, transaction control,
DDL, ``SELECT 1``, ``SELECT to_tsvector($1)``) and ``unparsed`` (a data statement
the facade cannot parse; no shape is guessed from it). A statement the facade cannot
parse is ``utility`` when the collector did not type it as a data statement. MySQL
``REPLACE INTO`` is read as the ``INSERT`` it is (a write by key), and a digest the
collector cut short is read up to its last complete predicate (the facade's
``truncated_read``), but never as key-anchored: the cut-off predicates are not
evidence of an anchor, so it is classed as the least anchored class its shape allows
(``_cut_short_class``), and the reason says so (#986).

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
from dataclasses import dataclass, field, replace

from src.agents.referee.table_resolution import TableNameResolver
from src.tools.sql.facade import AccessFacts, ColumnFact, ScopeFacts, ScopeSource
from src.tools.sql.query_facts import is_timestamp_type, query_facts

KEY_LOOKUP = "key_lookup"
KEY_SCOPED = "key_scoped"
REFERENCE_READ = "reference_read"
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
    REFERENCE_READ,
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
    REFERENCE_READ: "small reference-table read",
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
# A time of day, which (like a point in time, ``query_facts.is_timestamp_type``)
# orders rows rather than addressing an entity.
_TIME_OF_DAY_TYPES = frozenset({"time", "timetz"})
# A non-unique index's leading column whose distinct values (the collector's
# ``cardinality``) number at most this share of the table's rows addresses a large
# slice of the table per value, not an item collection, so it is not a key.
_LOW_CARDINALITY_SHARE = 0.01
# A table with at most this many collected rows is small: read whole, it is reference
# data (``reference_read``), and DynamoDB's metadata-config-store pattern reads the same
# limit (``src.tools.analysis.dynamodb_analysis_tools``, #920).
SMALL_TABLE_MAX_ROWS = 10_000


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
    # The collector's row count, when it reports one.
    row_count: int | None = None

    def pins_unique_key(self, pinned: Iterable[str]) -> bool:
        """Whether ``pinned`` (lower-case column names held to a value) covers a full
        unique key: the primary key or every column of a unique index."""
        columns = frozenset(pinned)
        return any(u <= columns for u in self.unique_keys)


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
    """A date, datetime or timestamp (the timestamp-column fact's own definition,
    #935) or a time of day: an index on one orders rows, it does not address an
    entity, so its leading column is not a key."""
    if is_timestamp_type(column):
        return True
    normalized = str(column.get("normalized_data_type") or "").lower()
    native = str(column.get("data_type") or "").lower().split("(")[0].split()
    return (normalized or (native[0] if native else "")) in _TIME_OF_DAY_TYPES


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
        row_count=rows if isinstance(rows := table.get("row_count"), int) else None,
    )


@dataclass(frozen=True)
class AccessClassification:
    """A query's class, a short, plain-language reason, and the query facts a class
    score may depend on (``src.shared.catalog.access_classes.QUERY_FACTS``):
    ``time_bucket`` (a GROUP BY over a date/time truncation), the text searches its
    scopes show (``full_text``, ``leading_wildcard``, ``pattern_match``,
    ``similarity_match``), ``window_function``, ``write`` (an INSERT, UPDATE, DELETE
    or MERGE), ``partition_range`` (a scope that pins a key column of one table and
    ranges another of its columns) and the SQL features ``recursive_cte``,
    ``lateral_join`` and ``stored_routine`` (#985)."""

    name: str
    reason: str
    facts: frozenset[str] = frozenset()

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
        if (k := scope.table_keys(a)) is not None and k.pins_unique_key(pinned[a])
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
    # A LIKE whose whole pattern is a bind parameter shows no text search: its value is
    # unknown (#912), so the scope is classed by its other predicates, and the triage
    # text-search signal still names it (#920).
    if facts.text_search and not keyed:
        return TEXT_SEARCH, "text or pattern match with no key anchoring the rows"
    if facts.text_search or facts.like_parameter:
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
    small = (
        table_keys is not None
        and table_keys.row_count is not None
        and table_keys.row_count <= SMALL_TABLE_MAX_ROWS
    )
    if small and not facts.limit and not facts.skip_locked:
        return REFERENCE_READ, (
            f"reads all of {table}, a small table ({table_keys.row_count} rows), "  # type: ignore[union-attr]
            "with no predicate"
        )
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


# Appended to the reason of a class read from a digest the collector cut short (#986).
TRUNCATED_NOTE = (
    " (read from a digest the collector cut short, up to its last complete predicate;"
    " later predicates are not seen)"
)
_ANCHORED_CLASSES = frozenset({KEY_LOOKUP, KEY_SCOPED, REFERENCE_READ, KEY_RANGE})
_DATA_QUERY_TYPES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"})
_WRITE_KINDS = frozenset({"insert", "update", "delete", "merge"})


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
    if name == AD_HOC_FILTER and any(s.like_parameter and not s.text_search for s in facts.scopes):
        reason += " (its LIKE pattern is a bind parameter, so no text search is visible, #912)"
    if facts.truncated_read:
        name, reason = _cut_short_class(facts, name, reason)
    return AccessClassification(name, reason, _class_facts(facts, keys))


def _cut_short_class(facts: AccessFacts, name: str, reason: str) -> tuple[str, str]:
    """The class of a digest the collector cut short, from the prefix the facade read.

    The unseen predicates are not evidence of a key anchor, and a key anchor is not a
    harmless guess: a key-anchored join is not join participation, so one query read as
    anchored can lift the relational evidence off a whole table (``aurora-anti-05``).
    So a prefix that reads as anchored is classed as the least anchored class its shape
    allows, an aggregation (it combines values), an unanchored join (more than one
    table) or an ad hoc filter (one table); a prefix already in one of those classes,
    or in ``text_search``, keeps it (#986).
    """
    if name not in _ANCHORED_CLASSES:
        return name, reason + TRUNCATED_NOTE
    scopes = facts.scopes
    if any(s.aggregate for s in scopes):
        return AGGREGATION, "aggregates rows no visible key anchors" + TRUNCATED_NOTE
    if any(sum(1 for src in s.sources if not src.function) > 1 for s in scopes):
        return UNANCHORED_JOIN, "joins tables with no visible key path" + TRUNCATED_NOTE
    return AD_HOC_FILTER, "filters rows with no visible key anchor" + TRUNCATED_NOTE


# The facade's text-search kinds, by the query-fact name a class score reads
# (``src.shared.catalog.access_classes.QUERY_FACTS``).
_TEXT_FACTS = {
    "fulltext": "full_text",
    "leading_wildcard": "leading_wildcard",
    "pattern": "pattern_match",
    "similarity": "similarity_match",
}


def _class_facts(facts: AccessFacts, keys: SchemaKeys) -> frozenset[str]:
    """The query facts a class score may depend on (``AccessClassification.facts``)."""
    found = {_TEXT_FACTS[k] for s in facts.scopes for k in s.text_search_kinds if k in _TEXT_FACTS}
    if any(s.time_bucket for s in facts.scopes):
        found.add("time_bucket")
    if facts.window_function:
        found.add("window_function")
    if facts.kind in _WRITE_KINDS:
        found.add("write")
    if any(_partition_range(_scope_columns(s, keys)) for s in facts.scopes):
        found.add("partition_range")
    return frozenset(found)


def _partition_range(scope: ScopeColumns) -> bool:
    """A scope that reads one table, pins a key column of it to a value and bounds
    another of its columns by a range (the facade's ``ranges``): the shape of a
    partition-key equality plus a sort-key range, DynamoDB's ``Query`` on one table
    (#933, #920). A join's range is not one: its other tables are an item collection
    reached by key, which the join patterns rate."""
    if len(scope.tables) != 1:
        return False
    (table,) = scope.tables
    pinned = scope.pinned.get(table, frozenset()) & scope.key_columns.get(table, frozenset())
    return bool(pinned) and bool(scope.ranged.get(table, frozenset()) - pinned)


def classify_query(
    query: Mapping, keys: SchemaKeys, source_engine: str | None
) -> AccessClassification:
    """The access-pattern class of one collected query."""
    text = str(query.get("query_text") or "")
    facts = query_facts(text, source_engine).access_facts()
    result = classify_facts(facts, keys, str(query.get("query_type") or ""))
    return _with_feature_facts(result, text, source_engine)


def _with_feature_facts(
    result: AccessClassification, text: str, source_engine: str | None
) -> AccessClassification:
    """``result`` with the SQL features its text shows (#985), read from the shared query
    facts (:mod:`src.tools.sql.query_facts`, one definition each, read in the source's
    dialect): ``recursive_cte``, ``lateral_join`` and ``stored_routine`` (a call to, or
    the definition of, a stored procedure or function; a SQL Server ``EXEC``),
    ``upsert`` (``ON CONFLICT``, ``ON DUPLICATE KEY``, ``REPLACE INTO``; #984),
    ``prefix_regex`` (every regular expression is an index-servable prefix, #982) and
    ``json_access`` (the query reads or builds JSON, #1102)."""
    readings = query_facts(text, source_engine)
    shown = {
        name
        for name, present in (
            ("recursive_cte", readings.recursive_cte),
            ("lateral_join", readings.lateral_join),
            ("stored_routine", readings.stored_routine_keyword),
            ("upsert", readings.upsert),
            ("prefix_regex", readings.prefix_regex),
            ("json_access", readings.json_access),
        )
        if present
    }
    if not shown:
        return result
    return replace(result, facts=result.facts | shown)


@dataclass(frozen=True)
class ScopeColumns:
    """One scope of a query resolved against the schema, by collected table id.

    ``tables``: the stored tables the scope reads (a table outside the collected schema,
    a derived table or a row function is left out). ``pinned``: per table, the columns
    the scope pins to a value. ``ranged``: per table, the columns the scope bounds by a
    range (the facade's ``ranges``, the one range definition). ``joined``: the scope's
    ``col = col`` join terms as ``(table, column, table, column)``. ``key_columns``: per
    table, its key columns (the module docstring's definition).
    """

    tables: frozenset[str]
    pinned: Mapping[str, frozenset[str]]
    joined: frozenset[tuple[str, str, str, str]]
    ranged: Mapping[str, frozenset[str]] = field(default_factory=dict)
    key_columns: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # Per branch of the WHERE's only OR group of plain equalities (the facade's
    # ``or_branches``, #915): per table, the columns that branch pins.
    or_pinned: tuple[Mapping[str, frozenset[str]], ...] = ()


@dataclass(frozen=True)
class QueryAccess:
    """A query's class and its scopes' resolved columns, from one parse."""

    classification: AccessClassification
    scopes: tuple[ScopeColumns, ...]


def _pinned_by_table(scope: _ScopeTables, columns: Iterable[ColumnFact]) -> dict[str, set[str]]:
    pinned: dict[str, set[str]] = {}
    for column in columns:
        owner = scope.owner(column)
        table_id = scope.ids.get(owner) if owner is not None else None
        if table_id:
            pinned.setdefault(table_id, set()).add(column.name)
    return pinned


def _scope_columns(facts: ScopeFacts, keys: SchemaKeys) -> ScopeColumns:
    scope = _ScopeTables(keys, facts)
    pinned = _pinned_by_table(scope, facts.equalities)
    ranged: dict[str, set[str]] = {}
    for column in facts.ranges:
        owner = scope.owner(column)
        table_id = scope.ids.get(owner) if owner is not None else None
        if table_id:
            ranged.setdefault(table_id, set()).add(column.name)
    joined: set[tuple[str, str, str, str]] = set()
    for left, right in facts.joins:
        a, b = scope.owner(left), scope.owner(right)
        ta = scope.ids.get(a) if a is not None else None
        tb = scope.ids.get(b) if b is not None else None
        if ta and tb and a != b:
            joined.add((ta, left.name, tb, right.name))
    tables = frozenset(
        table_id
        for source in facts.sources
        if not source.function and (table_id := scope.ids.get(source.alias))
    )
    return ScopeColumns(
        tables=tables,
        pinned={t: frozenset(c) for t, c in pinned.items()},
        joined=frozenset(joined),
        ranged={t: frozenset(c) for t, c in ranged.items()},
        key_columns={t: keys.tables[t].key_columns for t in tables if t in keys.tables},
        or_pinned=tuple(
            {t: frozenset(c) for t, c in _pinned_by_table(scope, branch).items()}
            for branch in facts.or_branches
        ),
    )


def query_access(query: Mapping, keys: SchemaKeys, source_engine: str | None) -> QueryAccess:
    """:func:`classify_query` plus each scope's resolved columns, from one parse."""
    text = str(query.get("query_text") or "")
    facts = query_facts(text, source_engine).access_facts()
    classification = classify_facts(facts, keys, str(query.get("query_type") or ""))
    classification = _with_feature_facts(classification, text, source_engine)
    if facts.truncated_read:
        # A cut-short digest's class is read from its prefix, but the columns it pins
        # are not evidence for the key-access and attribute analyses: the predicates
        # that were cut off may pin others (#986).
        return QueryAccess(classification, ())
    return QueryAccess(classification, tuple(_scope_columns(s, keys) for s in facts.scopes))
