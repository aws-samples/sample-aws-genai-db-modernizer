# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reads of an entity with variable attributes, an engine-neutral fact (#920).

A document model stores an entity whose attribute set varies (by type or category, or
as a list of name/value pairs) as one document with only the fields it has. The
relational schemas for that shape are recognisable from the collected schema, and the
access pattern that benefits is a read of one entity by its key. This module reports
both as facts; the pattern catalog turns them into scores (ADR-031 Decision 8).

Schema facts (from the collected schema only, never from the SQL text):

- an **attribute table** (entity-attribute-value): a narrow table (at most
  ``_ATTRIBUTE_TABLE_MAX_COLUMNS`` columns) with an attribute-name column of a string
  type, a value column, and an entity column, a key column (ADR-031 Decision 14
  Amendment 5's definition) other than those two and the table's own primary key, such
  as ``wp_postmeta (post_id, meta_key, meta_value)`` or
  ``user_custom_fields (user_id, name, value)``;
- a **typed entity**: a table with a discriminator column (``type``, ``kind``,
  ``category``, ``post_type``, ...). A ``<x>_type`` column next to a ``<x>_id`` column
  is the type half of a polymorphic association (the row points to an entity of that
  type), not the row's own type, so it is not one;
- a **document column**: a JSON column.

Column count and nullability are never evidence: nullable columns are ordinary
relational design, and the collector reports no null fraction to measure sparseness.

Per query, only a SELECT whose access-pattern class is a key lookup or a key-scoped read
(:func:`src.agents.referee.access_pattern.query_access`) can show one of these reads:

- ``ATTRIBUTE_ROWS``: it reads an attribute table with the entity column pinned to a
  value or joined (one entity's attribute set);
- ``ENTITY_WITH_ATTRIBUTE_ROWS``: it joins a typed entity to its attribute table on the
  attribute table's entity column (the entity and its variable fields in one read);
- ``TYPED_DOCUMENT``: it reads a typed entity that has a document column.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.agents.referee.access_pattern import (
    KEY_LOOKUP,
    KEY_SCOPED,
    QueryAccess,
    SchemaKeys,
    query_access,
)

ATTRIBUTE_ROWS = "attribute_rows"
ENTITY_WITH_ATTRIBUTE_ROWS = "entity_with_attribute_rows"
TYPED_DOCUMENT = "typed_document"

_ATTRIBUTE_TABLE_MAX_COLUMNS = 8
_ATTRIBUTE_NAME_COLUMNS = frozenset(
    {
        "meta_key",
        "name",
        "key",
        "attribute",
        "attribute_name",
        "field",
        "field_name",
        "property",
        "property_name",
        "setting",
        "setting_name",
    }
)
_ATTRIBUTE_VALUE_COLUMNS = frozenset(
    {"meta_value", "value", "val", "attribute_value", "field_value", "property_value"}
)
_DISCRIMINATOR_COLUMNS = frozenset(
    {"type", "kind", "category", "class", "discriminator", "entity_type", "subtype"}
)
_STRING_TYPES = frozenset({"string", "text"})
_DOCUMENT_TYPES = frozenset({"json", "jsonb"})
_DOCUMENT_READ_CLASSES = frozenset({KEY_LOOKUP, KEY_SCOPED})


def _type_of(column: Mapping) -> str:
    return str(column.get("normalized_data_type") or column.get("data_type") or "").lower()


def _is_string(column: Mapping) -> bool:
    kind = _type_of(column)
    return kind in _STRING_TYPES or "char" in kind or kind == "text"


def _discriminators(names: frozenset[str]) -> frozenset[str]:
    found = set(names & _DISCRIMINATOR_COLUMNS)
    for name in names:
        if name.endswith("_type") and f"{name[: -len('_type')]}_id" not in names:
            found.add(name)
    return frozenset(found)


@dataclass(frozen=True)
class VariantAttributeSchema:
    """The schema facts of every collected table (see the module docstring)."""

    entity_columns: Mapping[str, frozenset[str]]
    typed: frozenset[str]
    documents: frozenset[str]
    keys: SchemaKeys

    @classmethod
    def from_collector(cls, collector_output: Mapping) -> VariantAttributeSchema:
        keys = SchemaKeys.from_collector(collector_output)
        entity_columns: dict[str, frozenset[str]] = {}
        typed: set[str] = set()
        documents: set[str] = set()
        schema = collector_output.get("database_schema") or {}
        for table in schema.get("tables") or []:
            table_id = str(table.get("table_id") or "")
            columns = {
                str(c.get("column_name") or "").lower(): c for c in table.get("columns") or []
            }
            columns.pop("", None)
            names = frozenset(columns)
            if not table_id or not names:
                continue
            if _discriminators(names):
                typed.add(table_id)
            if any(_type_of(c) in _DOCUMENT_TYPES for c in columns.values()):
                documents.add(table_id)
            entity = _entity_columns(table_id, columns, keys)
            if entity:
                entity_columns[table_id] = entity
        return cls(entity_columns, frozenset(typed), frozenset(documents), keys)

    def reads(self, query: Mapping, source_engine: str | None) -> frozenset[str]:
        """The variable-attribute reads ``query`` shows (empty for anything else)."""
        if str(query.get("query_type") or "").upper() != "SELECT":
            return frozenset()
        touched = set(query.get("tables_accessed") or [])
        if not touched & (set(self.entity_columns) | self.typed):
            return frozenset()
        return self._reads(query_access(query, self.keys, source_engine))

    def _reads(self, access: QueryAccess) -> frozenset[str]:
        if access.classification.name not in _DOCUMENT_READ_CLASSES:
            return frozenset()
        found: set[str] = set()
        for scope in access.scopes:
            for table_id in scope.tables & set(self.entity_columns):
                entity = self.entity_columns[table_id]
                if scope.pinned.get(table_id, frozenset()) & entity:
                    found.add(ATTRIBUTE_ROWS)
                for left, left_col, right, right_col in scope.joined:
                    for mine, col, other in (
                        (left, left_col, right),
                        (right, right_col, left),
                    ):
                        if mine == table_id and col in entity:
                            found.add(ATTRIBUTE_ROWS)
                            if other in self.typed and other not in self.entity_columns:
                                found.add(ENTITY_WITH_ATTRIBUTE_ROWS)
            if scope.tables & self.typed & self.documents:
                found.add(TYPED_DOCUMENT)
        return frozenset(found)


def _entity_columns(
    table_id: str, columns: Mapping[str, Mapping], keys: SchemaKeys
) -> frozenset[str]:
    """The entity columns of an attribute table, or none when it is not one."""
    if len(columns) > _ATTRIBUTE_TABLE_MAX_COLUMNS:
        return frozenset()
    names = [n for n in _ATTRIBUTE_NAME_COLUMNS if n in columns and _is_string(columns[n])]
    values = [n for n in _ATTRIBUTE_VALUE_COLUMNS if n in columns]
    table_keys = keys.tables.get(table_id)
    if not names or not values or table_keys is None:
        return frozenset()
    own_key = set(table_keys.primary_key) if len(table_keys.primary_key) == 1 else set()
    return frozenset(table_keys.key_columns - set(names) - set(values) - own_key)
