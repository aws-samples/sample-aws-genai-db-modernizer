# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The canonical key of a table name (#1780): one rule for the schema design and the
facts layer, which may not import ``src.agents`` (ADR-032 Decision 3).

A design whose tables come from more than one schema keys each table by its schema too
(#2455): ``sales.orders`` and ``archive.orders`` are two tables, not one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def _cleaned(name: str) -> str:
    return "".join(c for c in name if c not in '`"[]').strip().lower()


def normalize_table_name(name: str) -> str:
    """Canonical table key: unquoted, lower-case, without any ``<db>.``/schema prefix.

    ``wordpress.wp_posts``, ```WordPress`.`wp_posts```` and ``wp_posts`` all
    normalise to ``wp_posts``, so designs and assignments match whether or not
    either side spells the prefix.
    """
    return _cleaned(name).rsplit(".", 1)[-1].strip()


def table_key(table_name: str, schema_name: str | None = None) -> str:
    """A design table's key: ``schema.table`` (unquoted, lower case) when the design names
    the table's schema (#2455), else :func:`normalize_table_name` of the name."""
    if schema_name:
        return f"{_cleaned(schema_name)}.{_cleaned(table_name)}"
    return normalize_table_name(table_name)


def spans_schemas(schemas: Iterable[str | None]) -> bool:
    """Whether tables of these schemas come from more than one schema, the names compared
    as the source spells them (``Sales`` and ``sales`` are two PostgreSQL schemas).

    :func:`table_key` folds case and drops quote marks, so two such schemas' tables of one
    name share a key: a design leaves out every table whose key another table has
    (:func:`colliding_keys`), and :class:`DesignTableKeys` reads such a key as no table.
    Two tables are never merged under one key, whatever the dialect's identifier rules."""
    return len({s for s in schemas if s}) > 1


def colliding_keys(tables: Iterable[tuple[str, str | None]]) -> set[str]:
    """The :func:`table_key` keys more than one of these ``(table_name, schema_name)``
    pairs has (``Sales.orders`` and ``sales.orders``, ``"a""b".orders`` and ``ab.orders``)."""
    seen: set[str] = set()
    shared: set[str] = set()
    for name, schema in tables:
        key = table_key(name, schema)
        (shared if key in seen else seen).add(key)
    return shared


class DesignTableKeys:
    """The keys of one design's tables, and the key a name as a query spells it reads as.

    ``tables`` are ``(table_name, schema_name)`` pairs, ``schema_name`` None for a table
    keyed by its bare name. In a design that names schemas, a qualified name
    (``sales.orders``, ``db.sales.orders``) reads as the table of that schema and name, and
    as no table when the design has no such schema (``crm.orders``) unless the prefix is
    ``source_database`` (``shop.orders`` from a MySQL query). Any other name reads as the
    one table of its bare name, and as no table when two schemas both have one (it is
    ambiguous). A key two pairs share reads as no table (:func:`colliding_keys`).
    """

    def __init__(
        self, tables: Iterable[tuple[str, str | None]], source_database: str | None = None
    ) -> None:
        pairs = list(tables)
        shared = colliding_keys(pairs)
        self._keys: set[str] = set()
        self._schemas: set[str] = set()
        self._by_bare: dict[str, set[str]] = defaultdict(set)
        self._database = _cleaned(source_database) if source_database else None
        for name, schema in pairs:
            key = table_key(name, schema)
            if schema:
                self._schemas.add(_cleaned(schema))
            if key in shared:
                continue
            self._keys.add(key)
            self._by_bare[normalize_table_name(name) if not schema else _cleaned(name)].add(key)

    def key_of(self, name: str) -> str | None:
        """The key of the table ``name`` names, or None (no such table, or ambiguous)."""
        parts = [p.strip() for p in _cleaned(name).split(".")]
        bare = parts[-1]
        if len(parts) > 1 and self._schemas:
            schema = parts[-2]
            if schema in self._schemas:
                key = f"{schema}.{bare}"
                return key if key in self._keys else None
            if len(parts) > 2 or schema != self._database:
                return None
        hits = self._by_bare.get(bare, set())
        return next(iter(hits)) if len(hits) == 1 else None

    def row_key(self, name: str) -> str:
        """The key a query's table counts under: :meth:`key_of`, else a key no design table
        has. A name of a schema the design lacks (``crm.orders``) keeps its schema, so its
        row never reads as another schema's ``orders``; a bare or source-database name keeps
        its bare key (``normalize_table_name``)."""
        key = self.key_of(name)
        if key is not None:
            return key
        parts = [p.strip() for p in _cleaned(name).split(".")]
        if len(parts) > 1 and self._schemas and parts[-2] != self._database:
            return f"{parts[-2]}.{parts[-1]}"
        return normalize_table_name(name)
