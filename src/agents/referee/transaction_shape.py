"""The shape of a write's transaction, one engine-neutral fact (ADR-031 Decision 3, #493).

DynamoDB supports ACID transactions: ``TransactWriteItems``/``TransactGetItems``
cover up to 100 items in one account and Region, and a conditional write is ACID
on its own item. So a transaction is not a capability DynamoDB lacks; what can
need a redesign is the transaction's *shape*. This module reports that shape for
one write statement, the same way for every engine; the catalog
(``src/catalog/patterns/wide-transaction.toml``) turns it into a score:

- ``SINGLE_ITEM``: the write changes at most one item -- a conditional write.
- ``BOUNDED``: it changes a known number of items, at most
  :data:`TRANSACTION_MAX_ITEMS` -- one ``TransactWriteItems`` call.
- ``KEY_SCOPED``: it changes the rows one value of a key column addresses, a
  column that is not a full primary or unique key but is a key anchor (the #728
  key-column rule, ``TableKeys.key_columns``: a foreign key, an index's leading
  column, part of a unique key). On DynamoDB that column becomes the partition
  key of an item collection (or a GSI's), and the write is a ``Query`` plus a
  batch write: a key-scoped multi-item write, not a wide transaction (maintainer
  decision on #915). Its trace carries that schema-design requirement
  (``schema_note``); it takes no penalty.
- ``UNBOUNDED``: it changes more than :data:`TRANSACTION_MAX_ITEMS` items, or a
  number of items the statement does not bound -- the rows an ``INSERT ...
  SELECT`` copies, or the rows a range, join, subquery or a filter with no key
  anchor picks. On DynamoDB that is a redesign (batches of at most 100 items, or a
  retry-safe multi-step flow), so it is the shape the wide-transaction penalty
  applies to.

The item count comes from the collector's per-call row statistics when it
measured them (``rows_affected_avg``, else ``rows_returned_avg``: PostgreSQL's
statement statistics report an ``UPDATE``'s rows there). Without a measurement
the statement decides: the number of ``VALUES`` rows an ``INSERT`` (or a
``MERGE ... USING (VALUES ...)``) writes, or whether an ``UPDATE``/``DELETE`` (or
the ``SELECT`` an ``INSERT`` copies from) addresses its rows by key on one table
(:func:`~src.agents.referee.query_shape.single_table_access_shape` is ``CRUD``)
and with how many key values. Given the collected schema's keys
(:class:`~src.agents.referee.access_pattern.SchemaKeys`), a key is a full
primary or unique key of that table (#915). A write whose own ``WHERE`` pins a
key anchor but no full key (``WHERE user_id = $1``, or part of a composite key)
is ``KEY_SCOPED``; one that pins only columns with no key anchor
(``WHERE has_messages = $1``), or no column at all (an ``OR`` or ``NOT``), is
``UNBOUNDED``. An ``OR`` of branches that each pin a full key is as many items as
branches. The pinned
columns are the SQL facade's, read in the source engine's dialect; a table
outside the collected schema, or a statement the facade cannot parse, keeps the
text check's answer. A conditional write stays the write it guards
(review of #910): an ``[NOT] EXISTS (...)`` condition reads other rows but
changes none, a scalar subquery inside ``VALUES`` fills one column, and an
``INSERT ... SELECT`` with no ``FROM`` writes one row when its condition holds.
An array of keys (``= ANY($1)``) is a set the statement does not bound. A read is
not a write transaction (a locking read records its lock separately,
``query_shape.locking_clause``), so it has no shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from src.agents.referee.access_pattern import SchemaKeys, query_access
from src.agents.referee.query_shape import (
    CRUD,
    readable_sql,
    single_table_access_shape,
)
from src.agents.referee.table_resolution import SourceScope
from src.tools.sql.regex_facts import (
    EXISTS_CONDITION_RE,
    FROM_WORD_RE,
    IN_LIST_RE,
    SELECT_WORD_RE,
)

SINGLE_ITEM = "single item"
BOUNDED = "bounded"
KEY_SCOPED = "key scoped"
UNBOUNDED = "unbounded"

# DynamoDB's TransactWriteItems / TransactGetItems item limit (AWS service quota).
TRANSACTION_MAX_ITEMS = 100

_WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"})
# The SELECT word, an IN list, a [NOT] EXISTS condition and the FROM word are the shared
# readings in ``regex_facts`` (#930). The detectors below are facts of the write's shape
# no query fact or access fact gives: where the VALUES rows start (the access facts'
# ``inserts_values`` says only whether there are any), an array of key values (the access
# facts read ``= ANY(...)`` as one equality), a MERGE's VALUES source and ON condition,
# and set-returning functions.
_VALUES_RE = re.compile(r"\bVALUES?\b", re.IGNORECASE)
_KEY_ARRAY_RE = re.compile(r"=\s*(?:ANY|SOME)\s*\(", re.IGNORECASE)
_MERGE_VALUES_RE = re.compile(r"\bUSING\s*\(\s*VALUES\b", re.IGNORECASE)
_MERGE_ON_RE = re.compile(
    r"\)\s*(?:AS\s+)?\w+(?:\s*\([^()]*\))?\s+ON\b(?P<on>.*?)\bWHEN\b", re.IGNORECASE | re.DOTALL
)
# A comparison in the ON condition that can match many target rows per source row: any
# ``<``/``>`` operator (``<>`` included), ``BETWEEN`` or ``LIKE``. Not the range predicate
# fact (``regex_facts.RANGE_PREDICATE_RE``), which leaves out ``<>`` and ``LIKE``.
_RANGE_RE = re.compile(r"[<>]|\bBETWEEN\b|\bLIKE\b", re.IGNORECASE)
# Functions that return a set of rows: ``INSERT ... SELECT unnest($1)`` writes
# many rows though it has no FROM clause.
_SET_RETURNING_RE = re.compile(
    r"\b(?:unnest|generate_series|json_array_elements|jsonb_array_elements|"
    r"json_each|jsonb_each|json_to_recordset|jsonb_to_recordset|"
    r"regexp_split_to_table|string_to_table|string_split|openjson)\s*\(",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TransactionShape:
    """One write's shape, its item count when known, and the reason in plain words."""

    shape: str
    items: int | None
    reason: str
    # The schema-design requirement a key-scoped write carries (``KEY_SCOPED``).
    schema_note: str | None = None

    @property
    def needs_redesign(self) -> bool:
        """Whether one DynamoDB transaction cannot hold it (the penalised shape)."""
        return self.shape == UNBOUNDED

    def as_trace(self) -> dict[str, object]:
        trace: dict[str, object] = {"shape": self.shape, "items": self.items, "reason": self.reason}
        if self.schema_note is not None:
            trace["schema_note"] = self.schema_note
        return trace


def _by_count(items: int, reason: str) -> TransactionShape:
    if items <= 1:
        return TransactionShape(SINGLE_ITEM, items, reason)
    if items <= TRANSACTION_MAX_ITEMS:
        return TransactionShape(BOUNDED, items, reason)
    return TransactionShape(
        UNBOUNDED,
        items,
        f"{reason}, more than the {TRANSACTION_MAX_ITEMS} items one transaction can hold",
    )


def _measured_items(query: Mapping) -> int | None:
    for field in ("rows_affected_avg", "rows_returned_avg"):
        value = query.get(field)
        if isinstance(value, int | float) and value > 0:
            return max(1, round(value))
    return None


def _balanced_group_end(sql: str, start: int) -> int:
    """Index just past the parenthesised group opening at ``sql[start]``."""
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(sql)


def _values_rows(sql: str) -> int:
    """How many row tuples follow ``VALUES`` (1 when there is no tuple list)."""
    match = _VALUES_RE.search(sql)
    if not match:
        return 1
    rows, i = 0, match.end()
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            rows += 1
            i = _balanced_group_end(sql, i)
            continue
        if ch.isspace() or ch == ",":
            i += 1
            continue
        break
    return max(rows, 1)


def _in_list_items(sql: str) -> int:
    """Items in the widest ``IN (...)`` list (1 when there is none)."""
    widest = 1
    for match in IN_LIST_RE.finditer(sql):
        start = match.end() - 1
        body = sql[start + 1 : _balanced_group_end(sql, start) - 1]
        depth, items = 0, 1
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                items += 1
        widest = max(widest, items)
    return widest


def _top_level(sql: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    """The first match of ``pattern`` outside every parenthesised group."""
    for match in pattern.finditer(sql):
        prefix = sql[: match.start()]
        if prefix.count("(") == prefix.count(")"):
            return match
    return None


def _without_exists_conditions(sql: str) -> str:
    """``sql`` with each ``[NOT] EXISTS (...)`` condition replaced by ``1=1``.

    The condition reads rows but changes none, so it never widens the write it
    guards (a conditional write, ADR-031 Decision 3). ``1=1`` is not a key
    predicate (``query_shape``), so a write guarded by nothing else stays unkeyed.
    """
    while (match := EXISTS_CONDITION_RE.search(sql)) is not None:
        end = _balanced_group_end(sql, match.end() - 1)
        sql = f"{sql[: match.start()]} 1=1 {sql[end:]}"
    return sql


def _tables_named_in(sql: str, tables: list) -> list:
    """The ``tables_accessed`` entries ``sql`` still names (an ``EXISTS`` table drops out)."""
    return [
        t
        for t in tables
        if re.search(rf"\b{re.escape(str(t).rsplit('.', 1)[-1])}\b", sql, re.IGNORECASE)
    ]


_UNKEYED = TransactionShape(
    UNBOUNDED,
    None,
    "changes the rows a range, join, subquery or unkeyed predicate picks: "
    "the statement does not bound them",
)


_KEY_ARRAY = TransactionShape(
    UNBOUNDED, None, "addresses its rows by an array of key values the statement does not bound"
)


def _unique_key_shape(
    query: Mapping, keys: SchemaKeys | None, scope: SourceScope | None, reason: str
) -> TransactionShape | None:
    """The shape the collected keys give a keyed write (#915), or ``None`` when the
    text check's key-value count stands.

    ``None`` when the write's own scope pins a full primary or unique key of its
    table. When its ``WHERE`` is an ``OR`` of branches that each pin a full key
    (``id = $1 OR id = $2``), as many items as branches. ``KEY_SCOPED`` when it pins
    a key anchor (``TableKeys.key_columns``) but no full key. Otherwise
    ``UNBOUNDED``: the rows a filter with no key anchor matches.

    The pinned columns are the SQL facade's for the statement's outermost scope (an
    ``EXISTS`` condition or a subquery is a scope of its own), read in ``scope``'s
    source-engine dialect. ``None`` too when there is no fact to read: no schema keys,
    a statement the facade cannot parse, a target outside the collected tables (a
    view, say), or no pinned column read without the source dialect."""
    if keys is None:
        return None
    access = query_access(query, keys, scope.engine if scope else None)
    if not access.scopes or len(access.scopes[0].tables) != 1:
        return None
    own = access.scopes[0]
    (table,) = own.tables
    table_keys = keys.tables.get(table)
    if table_keys is None:
        # A collected view resolves to an id but has no keys: the text check stands.
        return None
    pinned = own.pinned.get(table, frozenset())
    if table_keys.pins_unique_key(pinned):
        return None
    if own.or_pinned and all(
        table_keys.pins_unique_key(pinned | branch.get(table, frozenset()))
        for branch in own.or_pinned
    ):
        branches = len(own.or_pinned)
        return _by_count(branches, f"{reason} {branches} key value(s) on one table")
    if not pinned and not (scope and scope.engine):
        # With no source dialect a bind parameter such as ``$1`` may not read as a
        # value, so an empty reading is no evidence the predicate pins no key.
        return None
    name = table.rsplit(".", 1)[-1]
    anchors = sorted(pinned & table_keys.key_columns)
    if not anchors and own.or_pinned:
        # An OR whose every branch pins a key anchor reads one item collection per
        # branch: key scoped, like one anchor (maintainer decision on #915).
        per_branch = [
            (pinned | branch.get(table, frozenset())) & table_keys.key_columns
            for branch in own.or_pinned
        ]
        if all(per_branch):
            anchors = sorted(set().union(*per_branch))
    if anchors:
        verb = "copies" if reason.startswith("copies") else "changes"
        if len(anchors) == 1:
            (column,) = anchors
            what = f"the rows one {column} value of {name} addresses"
            must = f"{name}.{column} must become the partition key"
        else:
            what = f"the rows of {name} that one value of each of {', '.join(anchors)} addresses"
            must = f"{', '.join(f'{name}.{a}' for a in anchors)} must each become the partition key"
        return TransactionShape(
            KEY_SCOPED,
            None,
            f"{verb} {what}, a key that is not unique: a key-scoped multi-item write "
            "(a Query plus a batch write)",
            schema_note=(
                f"{must} of an item collection (or a GSI's) so this write reads its items by key"
            ),
        )
    if pinned:
        by = f"by {', '.join(sorted(pinned))}, no key of {name}"
    else:
        # An OR or a NOT of equalities pins no single column to one value.
        by = f"by a predicate that pins no primary or unique key of {name}"
    return TransactionShape(
        UNBOUNDED, None, f"addresses its rows {by}: the statement does not bound them"
    )


def _keyed(
    query: Mapping,
    query_type: str,
    sql: str,
    reason: str,
    scope: SourceScope | None,
    keys: SchemaKeys | None,
) -> TransactionShape:
    """The shape of a write whose rows ``sql``'s ``WHERE`` picks on one table, read in
    ``scope``'s source-engine dialect (#893), keyed only by a unique key of it when
    ``keys`` are known (#915)."""
    tables = _tables_named_in(sql, list(query.get("tables_accessed") or []))
    probe = {**query, "query_type": query_type, "query_text": sql, "tables_accessed": tables}
    if single_table_access_shape(probe, source=scope, text_is_readable=True) != CRUD:
        return _UNKEYED
    if _KEY_ARRAY_RE.search(sql):
        return _KEY_ARRAY
    by_key = _unique_key_shape(query, keys, scope, reason)
    if by_key is not None:
        return by_key
    count = _in_list_items(sql)
    return _by_count(count, f"{reason} {count} key value(s) on one table")


def _insert_shape(
    query: Mapping, sql: str, scope: SourceScope | None, keys: SchemaKeys | None
) -> TransactionShape:
    values = _top_level(sql, _VALUES_RE)
    select = _top_level(sql, SELECT_WORD_RE)
    if select is None or (values is not None and values.start() < select.start()):
        # A scalar subquery inside VALUES fills a column of the same row.
        rows = _values_rows(sql)
        return _by_count(rows, f"inserts {rows} row(s) per call")
    body = sql[select.start() :]
    if len(SELECT_WORD_RE.findall(body)) == 1 and not _SET_RETURNING_RE.search(body):
        source = _top_level(body, FROM_WORD_RE)
        if source is None:
            return _by_count(1, "inserts one row when its condition holds")
        shape = _keyed(
            query, "DELETE", f"DELETE {body[source.start() :]}", "copies the rows of", scope, keys
        )
        # A count of key values stands (more than 100 of them included); the rows a
        # non-unique filter matches fall through to the reason below.
        if shape is not _UNKEYED and (shape.shape != UNBOUNDED or shape.items is not None):
            return shape
    return TransactionShape(
        UNBOUNDED, None, "inserts the rows a query returns: the statement does not bound them"
    )


def _merge_shape(sql: str) -> TransactionShape:
    using = _MERGE_VALUES_RE.search(sql)
    if using is not None:
        group = sql.rfind("(", 0, using.end())
        on = _MERGE_ON_RE.search(sql, _balanced_group_end(sql, group) - 1)
        if on is not None and "=" in on.group("on") and not _RANGE_RE.search(on.group("on")):
            rows = _values_rows(sql[group:])
            return _by_count(rows, f"merges {rows} row(s) per call by key")
    return _UNKEYED


def _readable_sql(text: str, source_engine: str | None) -> str:
    """``text`` without comments and literals, identifiers unquoted (see ``readable_sql``)."""
    return readable_sql(text, source_engine)


def transaction_shape(
    query: Mapping, source: SourceScope | None = None, keys: SchemaKeys | None = None
) -> TransactionShape | None:
    """The transaction shape of ``query``, or ``None`` when it is not a write.

    ``source`` is the collected database; its engine is the dialect the query facts
    behind the single-table check are read in (#893). ``keys`` are the collected
    schema's keys: with them, only a full primary or unique key addresses one item
    (#915); without them, any key predicate does."""
    query_type = str(query.get("query_type") or "").upper()
    if query_type not in _WRITE_TYPES:
        return None
    measured = _measured_items(query)
    if measured is not None:
        return _by_count(measured, f"changes about {measured} row(s) per call (measured)")
    sql = _without_exists_conditions(
        _readable_sql(str(query.get("query_text") or ""), source.engine if source else None)
    )
    if query_type in ("INSERT", "REPLACE"):
        return _insert_shape(query, sql, source, keys)
    if query_type == "MERGE":
        return _merge_shape(sql)
    return _keyed(query, query_type, sql, "addresses its rows by", source, keys)
