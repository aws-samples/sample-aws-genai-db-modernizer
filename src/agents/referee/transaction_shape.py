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
- ``UNBOUNDED``: it changes more than :data:`TRANSACTION_MAX_ITEMS` items, or a
  number of items the statement does not bound -- the rows an ``INSERT ...
  SELECT`` copies, or the rows a range, join, subquery or unkeyed predicate
  picks. On DynamoDB that is a redesign (batches of at most 100 items, or a
  retry-safe multi-step flow), so it is the shape the wide-transaction penalty
  applies to.

The item count comes from the collector's per-call row statistics when it
measured them (``rows_affected_avg``, else ``rows_returned_avg``: PostgreSQL's
statement statistics report an ``UPDATE``'s rows there). Without a measurement
the statement decides: the number of ``VALUES`` rows an ``INSERT`` (or a
``MERGE ... USING (VALUES ...)``) writes, or whether an ``UPDATE``/``DELETE`` (or
the ``SELECT`` an ``INSERT`` copies from) addresses its rows by key on one table
(:func:`~src.agents.referee.query_shape.single_table_access_shape` is ``CRUD``)
and with how many key values. A conditional write stays the write it guards
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

from src.agents.referee.query_shape import (
    CRUD,
    _unquote_identifiers,
    single_table_access_shape,
)
from src.shared.unsupported_pattern import strip_comments_and_literals

SINGLE_ITEM = "single item"
BOUNDED = "bounded"
UNBOUNDED = "unbounded"

# DynamoDB's TransactWriteItems / TransactGetItems item limit (AWS service quota).
TRANSACTION_MAX_ITEMS = 100

_WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"})
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_VALUES_RE = re.compile(r"\bVALUES?\b", re.IGNORECASE)
_IN_LIST_RE = re.compile(r"\bIN\s*\(", re.IGNORECASE)
_EXISTS_RE = re.compile(r"\b(?:NOT\s+)?EXISTS\s*\(", re.IGNORECASE)
_FROM_RE = re.compile(r"\bFROM\b", re.IGNORECASE)
_KEY_ARRAY_RE = re.compile(r"=\s*(?:ANY|SOME)\s*\(", re.IGNORECASE)
_MERGE_VALUES_RE = re.compile(r"\bUSING\s*\(\s*VALUES\b", re.IGNORECASE)
_MERGE_ON_RE = re.compile(
    r"\)\s*(?:AS\s+)?\w+(?:\s*\([^()]*\))?\s+ON\b(?P<on>.*?)\bWHEN\b", re.IGNORECASE | re.DOTALL
)
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

    @property
    def needs_redesign(self) -> bool:
        """Whether one DynamoDB transaction cannot hold it (the penalised shape)."""
        return self.shape == UNBOUNDED

    def as_trace(self) -> dict[str, object]:
        return {"shape": self.shape, "items": self.items, "reason": self.reason}


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
    for match in _IN_LIST_RE.finditer(sql):
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
    while (match := _EXISTS_RE.search(sql)) is not None:
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


def _keyed(query: Mapping, query_type: str, sql: str, reason: str) -> TransactionShape:
    """The shape of a write whose rows ``sql``'s ``WHERE`` picks on one table."""
    tables = _tables_named_in(sql, list(query.get("tables_accessed") or []))
    probe = {**query, "query_type": query_type, "query_text": sql, "tables_accessed": tables}
    if single_table_access_shape(probe) != CRUD:
        return _UNKEYED
    if _KEY_ARRAY_RE.search(sql):
        return _KEY_ARRAY
    keys = _in_list_items(sql)
    return _by_count(keys, f"{reason} {keys} key value(s) on one table")


def _insert_shape(query: Mapping, sql: str) -> TransactionShape:
    values = _top_level(sql, _VALUES_RE)
    select = _top_level(sql, _SELECT_RE)
    if select is None or (values is not None and values.start() < select.start()):
        # A scalar subquery inside VALUES fills a column of the same row.
        rows = _values_rows(sql)
        return _by_count(rows, f"inserts {rows} row(s) per call")
    body = sql[select.start() :]
    if len(_SELECT_RE.findall(body)) == 1 and not _SET_RETURNING_RE.search(body):
        source = _top_level(body, _FROM_RE)
        if source is None:
            return _by_count(1, "inserts one row when its condition holds")
        shape = _keyed(query, "DELETE", f"DELETE {body[source.start():]}", "copies the rows of")
        if shape is not _UNKEYED:
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


def transaction_shape(query: Mapping) -> TransactionShape | None:
    """The transaction shape of ``query``, or ``None`` when it is not a write."""
    query_type = str(query.get("query_type") or "").upper()
    if query_type not in _WRITE_TYPES:
        return None
    measured = _measured_items(query)
    if measured is not None:
        return _by_count(measured, f"changes about {measured} row(s) per call (measured)")
    sql = _without_exists_conditions(
        strip_comments_and_literals(_unquote_identifiers(str(query.get("query_text") or "")))
    )
    if query_type in ("INSERT", "REPLACE"):
        return _insert_shape(query, sql)
    if query_type == "MERGE":
        return _merge_shape(sql)
    return _keyed(query, query_type, sql, "addresses its rows by")
