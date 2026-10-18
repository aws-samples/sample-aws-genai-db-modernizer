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
