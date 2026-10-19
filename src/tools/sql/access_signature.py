"""The access-pattern signature: what makes two collected statements one access pattern.

A collection holds many statements that are the same access pattern written several
ways: the same statement collected twice, copies whose IN-lists differ in length, or
reads that differ only in the columns they return. Counting statements counts those
copies again. This module gives each access one signature, so a reader can count
distinct access patterns instead (#1104 review, maintainer decision 2026-10-19; first
reader: the workload key anchors, ``access_pattern._workload_keys``).

A signature holds:

- ``operation``: ``write`` for the rows an ``UPDATE`` or ``DELETE`` changes (and, for a
  whole statement, any ``INSERT`` or ``MERGE``), ``aggregate`` when the statement's
  outermost scope has a ``COUNT`` or other aggregate or a ``GROUP BY``, ``read`` (an item
  read) otherwise, ``other`` for a statement that reads no table (SET, DDL, ...). A
  subquery inside a write reads its table: ``DELETE FROM a WHERE x IN (SELECT id FROM b
  WHERE k = $1)`` reads ``b``;
- ``tables``: the stored tables the scope reads, as written (lower-case, sorted);
- the predicate shape: the columns pinned by ``=``/``IN``/``ANY`` (``equalities``), bounded
  by a range (``ranges``), joined (``joins``), each plain-equality ``OR`` branch's columns,
  the number of other predicate terms, and the kinds of text search;
- ``order_by``: the sort columns, in order (None for an expression);
- ``limit``: ``"none"``, ``"one"`` (``LIMIT 1``, ``TOP 1``) or ``"many"`` (any other row
  count, a placeholder included).

It is read from the SQL facade's ``AccessFacts`` (``src.tools.sql.facade``), which already
hold no literal, no placeholder number and no IN-list length, so it adds no SQL reading
of its own: values are gone, and ``IN ($1)`` and ``IN ($1, $2)`` are one predicate. The
projection (the columns a read returns) is not part of it.

A whole statement's access pattern (``query_signature``, #1146) is its tables written
plus the signature of every scope it reads in: the outermost with the statement's
operation, each nested one (a subquery, a derived table, a CTE body, a ``UNION``
branch) as ``scope_signature`` reads it, and each scope's aggregate shape (its
``GROUP BY`` items, ``HAVING`` terms and aggregate functions: ``GROUP BY status`` and
``GROUP BY customer_id``, ``COUNT(*)`` and ``SUM(total)`` are different numbers). The
aggregate shape is only in the statement signature: routing's key anchors read
``scope_signature``. So is a write's kind (INSERT, UPDATE, DELETE or upsert): an
``UPDATE`` and a ``DELETE`` by one key are two item operations, so two statement
patterns, while the scope signature still calls both a ``write``. ``DISTINCT`` is read like the projection: it does not separate
two statements. An ``INSERT ... VALUES`` reads no scope: its
signature is the write on its table. A statement the facade could not read, read only
up to where the collector cut it short, or that reads and writes no stored table (``SELECT
NEXT VALUE FOR seq``, a function call) has none: it is its own access pattern.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from src.tools.sql.facade import AccessFacts, ColumnFact, ScopeFacts

Operation = Literal["read", "aggregate", "write", "other"]
LimitClass = Literal["none", "one", "many"]

_WRITES = frozenset({"insert", "update", "delete", "merge"})


@dataclass(frozen=True)
class AccessSignature:
    """One access pattern (the module docstring's definition). Equal signatures are one
    access pattern; the fields are sorted wherever their order carries no meaning."""

    operation: Operation
    tables: tuple[str, ...]
    equalities: tuple[str, ...]
    ranges: tuple[str, ...]
    joins: tuple[tuple[str, str], ...]
    or_branches: tuple[tuple[str, ...], ...]
    other_filters: int
    text_search: tuple[str, ...]
    order_by: tuple[str | None, ...]
    limit: LimitClass


def operation(facts: AccessFacts) -> Operation:
    """The statement's operation: a write by its kind, else an aggregate when its
    outermost scope aggregates or groups, else a read (``other`` for SET, DDL, ...)."""
    if facts.kind in _WRITES:
        return "write"
    if facts.kind != "select":
        return "other"
    top = facts.scopes[0] if facts.scopes else None
    if top is not None and (top.aggregate or top.group_by):
        return "aggregate"
    return "read"


def limit_class(limit: str | None) -> LimitClass:
    """``LIMIT 1`` is one row, any other limit (a placeholder too) many, none none."""
    if limit is None:
        return "none"
    return "one" if limit.strip() == "1" else "many"


def _names(columns: Iterable[ColumnFact]) -> tuple[str, ...]:
    return tuple(sorted({c.name.lower() for c in columns}))


def scope_operation(facts: AccessFacts, scope: ScopeFacts) -> Operation:
    """The operation of the access ``scope`` makes: a write for an ``UPDATE`` or
    ``DELETE``'s own scope (the rows it changes), else a read or an aggregate as the
    statement's outermost scope is one, so a lookup nested in a ``COUNT(*)`` is another
    access pattern than the same lookup nested in an item read."""
    whole = operation(facts)
    if whole == "other":
        return "other"
    if facts.kind in ("update", "delete") and facts.scopes and scope is facts.scopes[0]:
        return "write"
    top = facts.scopes[0] if facts.scopes else None
    return "aggregate" if top is not None and (top.aggregate or top.group_by) else "read"


def scope_signature(
    facts: AccessFacts, scope: ScopeFacts, op: Operation | None = None
) -> AccessSignature:
    """The signature of the access ``scope`` makes, in the statement ``facts`` describe:
    its operation (``scope_operation``, unless ``op`` is given), and the scope's tables,
    predicates, order and limit. What the outer statement selects is not part of it."""
    tables = sorted(
        {s.table.name.lower() for s in scope.sources if s.table is not None and not s.function}
    )
    return AccessSignature(
        operation=op or scope_operation(facts, scope),
        tables=tuple(tables),
        equalities=_names(scope.equalities),
        ranges=_names(scope.ranges),
        joins=tuple(
            sorted({tuple(sorted((a.name.lower(), b.name.lower()))) for a, b in scope.joins})  # type: ignore[misc]
        ),
        or_branches=tuple(sorted({_names(branch) for branch in scope.or_branches})),
        other_filters=scope.other_filters,
        text_search=tuple(
            sorted({*scope.text_search_kinds, *(["parameter"] if scope.like_parameter else [])})
        ),
        order_by=tuple(c.name.lower() if c is not None else None for c in scope.order_by),
        limit=limit_class(scope.limit),
    )


def statement_signature(facts: AccessFacts) -> AccessSignature | None:
    """The signature of the statement's outermost scope, with the whole statement's
    ``operation`` (an ``INSERT ... SELECT`` is a write), or None when the facade read no
    scope (a fallback parse, or a statement with no table access)."""
    if not facts.scopes:
        return None
    return scope_signature(facts, facts.scopes[0], operation(facts))


# A scope's aggregate shape (#1146 review): its GROUP BY items, HAVING terms and aggregate
# functions, as the facade reads them (``ScopeFacts.group_by_items``, ``having``,
# ``aggregate_kinds``).
AggregateShape = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]


# A write's item operation (#1146 review): an INSERT, an UPDATE, a DELETE, or an upsert
# (``ON CONFLICT``, ``ON DUPLICATE KEY UPDATE``, ``REPLACE``, ``MERGE``). Each is a
# different operation on the target item, so two writes of different kinds are two
# statement patterns even on one key.
WriteKind = Literal["insert", "update", "delete", "upsert"]


def write_kind(facts: AccessFacts, upsert: bool = False) -> WriteKind | None:
    """The statement's write kind, or None for a statement that writes nothing.
    ``upsert``: the statement is an upsert (``QueryFacts.upsert``, the one definition)."""
    if operation(facts) != "write":
        return None
    if upsert or facts.kind == "merge":
        return "upsert"
    return facts.kind if facts.kind in ("insert", "update", "delete") else "upsert"  # type: ignore[return-value]


@dataclass(frozen=True)
class QuerySignature:
    """One statement's access pattern (the module docstring): the tables it writes,
    sorted, the signature of each scope, outermost first, in the facade's order, and
    each scope's aggregate shape, in step with ``scopes``, and a write's kind
    (``write_kind``).

    The aggregate shape is a statement-level part only: ``scope_signature``, which the
    routing key anchors read (ADR-031 Amendment 11), leaves it out, so two aggregates
    over the same rows are two statement patterns while routing still counts one
    lookup."""

    writes: tuple[str, ...]
    scopes: tuple[AccessSignature, ...]
    aggregates: tuple[AggregateShape, ...] = ()
    write_kind: WriteKind | None = None


def query_signature(
    facts: AccessFacts, writes: Iterable[str] = (), upsert: bool = False
) -> QuerySignature | None:
    """The statement's access pattern, or None when it has no signature: a fallback parse,
    a digest the collector cut short, or a statement that reads and writes no stored table.
    ``writes``: the tables the statement writes (``QueryFacts.write_targets``);
    ``upsert``: it is an upsert (``QueryFacts.upsert``)."""
    if facts.parsed_by != "sqlglot" or facts.truncated_read:
        return None
    targets = tuple(sorted({t.lower() for t in writes}))
    top = statement_signature(facts)
    if top is None:
        if operation(facts) != "write" or not targets:
            return None
        top = AccessSignature("write", targets, (), (), (), (), 0, (), (), "none")
    nested = tuple(scope_signature(facts, scope) for scope in facts.scopes[1:])
    if not targets and not any(s.tables for s in (top, *nested)):
        return None  # reads no stored table: a sequence, a function, a constant
    shapes = tuple((s.group_by_items, s.having, s.aggregate_kinds) for s in facts.scopes)
    return QuerySignature(
        writes=targets,
        scopes=(top, *nested),
        aggregates=shapes,
        write_kind=write_kind(facts, upsert),
    )


def returned_columns(facts: AccessFacts) -> tuple[str, ...]:
    """The columns the statement's outermost scope returns, by name, sorted, with ``*``
    first when it returns every column; the projection a signature leaves out."""
    if not facts.scopes:
        return ()
    top = facts.scopes[0]
    names = sorted({c.name.lower() for c in top.returned})
    return ("*", *names) if top.returns_all else tuple(names)
