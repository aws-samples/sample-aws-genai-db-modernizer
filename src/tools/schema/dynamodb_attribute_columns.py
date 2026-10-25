"""Check a DynamoDB design's source columns against the collector's schema (#1431).

A direct attribute (one with no ``join`` and no ``calculation``) is copied from a
source column, so its ``source_table`` must be a collected table and its
``source_column`` must be a column of that table. A design that names a column the
table does not have, or maps an attribute to the wrong table, fails here with one
message per mismatch.

A ``{token}`` in an item-collection entity's ``pk_template`` or ``sk_template`` names
a DynamoDB attribute, not a column (the way ``canon_template`` in
``dynamodb_key_reconcile.py`` reads it). A token that matches an entity attribute
adds nothing to check: a direct attribute's ``source_column`` is checked on its own
and a derived one is built by its join or calculation. Only a token that matches no
attribute is checked as a column name.

An attribute with no source column and no join or calculation is a warning, not a
failure: the contract cannot express a constant (a label, a sparse-index flag), so
such an attribute may be a deliberate literal.

Identifiers match case-insensitively. This is deliberately lenient: PostgreSQL
quoted identifiers and case-sensitive SQL Server collations tell ``Id`` from ``ID``,
so a case-only mismatch can pass here, but a real column is never reported missing.

The check runs where a design is finalized (the external ``--merge`` and
``--finalize``, and ``finalize_schema_design``) and, in Bedrock mode, as part of the
designer's output check on its first design. Both use the collector as filtered to
the engine's assignment, which is what the designer saw.

The names in a message come from the collector and the model, so they are data:
they are truncated and never act as instructions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.agents.referee.table_resolution import TableNameResolver

#: Prefix of an attribute-column message in ``validation_failures``.
ATTRIBUTE_COLUMNS_PREFIX = "source columns: "


@dataclass(frozen=True)
class ColumnCheck:
    """``failures`` fail validation; ``warnings`` are reported but do not."""

    failures: list[str]
    warnings: list[str]


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_MAX_NAME = 80


def _name(value: object) -> str:
    text = str(value)
    return repr(text if len(text) <= _MAX_NAME else text[:_MAX_NAME] + "...")


class _Schema:
    def __init__(self, collector: Mapping[str, Any]) -> None:
        schema = collector.get("database_schema") or {}
        self.tables: dict[str, set[str]] = {
            str(t["table_id"]): {
                str(c.get("column_name", "")).lower() for c in t.get("columns") or []
            }
            for t in schema.get("tables") or []
            if t.get("table_id")
        }
        self.resolver = TableNameResolver.from_collector(collector)

    def resolve(self, name: str) -> str | None:
        if name in self.tables:
            return name
        found = self.resolver.resolve(name) if self.resolver else None
        return found if found in self.tables else None


def _columns_of(attr: Mapping[str, Any]) -> list[str]:
    raw = attr.get("source_column")
    return [str(c) for c in (raw if isinstance(raw, list) else [raw]) if c]


def _check_columns(
    where: str,
    table_name: object,
    columns: Iterable[str],
    schema: _Schema,
    sibling_tables: list[str],
) -> list[str]:
    """Messages for ``columns`` that are not columns of ``table_name``."""
    table_id = schema.resolve(str(table_name))
    if table_id is None:
        return [f"{where}: source table {_name(table_name)} is not a table of the collector"]
    known = schema.tables[table_id]
    messages = []
    for column in columns:
        if column.lower() in known:
            continue
        elsewhere = [
            t for t in sibling_tables if t != table_id and column.lower() in schema.tables[t]
        ]
        hint = (
            f"; it is a column of {', '.join(_name(t) for t in elsewhere)}, so check source_table"
            if elsewhere
            else ""
        )
        messages.append(
            f"{where}: source column {_name(column)} is not a column of {_name(table_id)}{hint}"
        )
    return messages


def _check_attribute(
    where: str,
    attr: object,
    schema: _Schema,
    default_table: object,
    siblings: list[str],
    out: ColumnCheck,
) -> None:
    if not isinstance(attr, Mapping):
        return
    where = f"{where}.{attr.get('name')}"
    if attr.get("join") or attr.get("calculation"):
        return  # derived on purpose; the design says how
    columns = _columns_of(attr)
    if not columns:
        out.warnings.append(
            f"{where}: names no source column and no join or calculation; "
            "it is fine only if it is a constant"
        )
        return
    table = attr.get("source_table") or default_table
    out.failures.extend(_check_columns(where, table, columns, schema, siblings))


def check_attribute_columns(design: Mapping[str, Any], collector: Mapping[str, Any]) -> list[str]:
    """The failures of :func:`check_columns`."""
    return check_columns(design, collector).failures


def check_columns(design: Mapping[str, Any], collector: Mapping[str, Any]) -> ColumnCheck:
    """Failures for each source table or column of ``design`` the collector does not have.

    Empty when every direct attribute and key template token matches the collector, or
    when the collector has no tables to compare with.
    """
    out = ColumnCheck([], [])
    schema = _Schema(collector)
    if not schema.tables:
        return out
    for table in design.get("table_definitions") or []:
        if not isinstance(table, Mapping):
            continue
        label = str(table.get("table_name"))
        sources = [
            r for r in (schema.resolve(str(s)) for s in table.get("source_tables") or []) if r
        ]
        default = sources[0] if len(sources) == 1 else None
        for attr in table.get("attributes") or []:
            _check_attribute(label, attr, schema, default, sources, out)
        for entity in table.get("entities") or []:
            if not isinstance(entity, Mapping):
                continue
            where = f"{label}[{entity.get('entity_type')}]"
            source = entity.get("source_table")
            attributes = [a for a in entity.get("attributes") or [] if isinstance(a, Mapping)]
            for attr in attributes:
                _check_attribute(where, attr, schema, source, sources, out)
            named = {str(a.get("name")).lower() for a in attributes}
            for field in ("pk_template", "sk_template"):
                tokens = [
                    t
                    for t in _PLACEHOLDER.findall(str(entity.get(field) or ""))
                    if t.lower() not in named
                ]
                if tokens and source:
                    out.failures.extend(
                        _check_columns(f"{where}.{field}", source, tokens, schema, sources)
                    )
    return out
