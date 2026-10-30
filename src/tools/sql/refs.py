# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two small value types the SQL readings share: a table reference and a column use.

They live apart from the facade so the regex-facts module (which the facade imports) can
carry a column use on its leading-wildcard ``LIKE`` fact (#942) without importing the
facade. :mod:`src.tools.sql.facade` re-exports both, so callers keep importing them from
there.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableRef:
    """One table reference as written: ``[catalog.][schema.]name``.

    ``quoted`` says the name itself was a quoted identifier (``"Topics"``,
    ``[Orders]``), which keeps its case in PostgreSQL, Oracle and DB2. It is
    not part of the reference's identity: ``"topics"`` and ``topics`` are one
    reference (the first one seen is kept). A regex-fallback reference is
    never marked quoted.
    """

    name: str
    schema: str | None = None
    catalog: str | None = None
    quoted: bool = field(default=False, compare=False)

    @property
    def qualifiers(self) -> list[str]:
        """``[catalog, schema]`` without the empty parts (what the resolver takes)."""
        return [q for q in (self.catalog, self.schema) if q]

    @property
    def written(self) -> str:
        return ".".join([*self.qualifiers, self.name])


@dataclass(frozen=True)
class ColumnUse:
    """A column the statement reads or writes, and the stored tables it can belong to
    (#935: the timestamp-column fact looks the column's type up in the collection).

    ``name`` is the column's name as written. ``scopes`` are the candidate tables,
    one tuple per enclosing scope, innermost first: a qualified column has one,
    holding the table its qualifier names (an alias or a table name, looked up from
    its own scope outward), or none when the qualifier names a derived table, a CTE
    or a table function; an unqualified column has every scope's stored tables, so
    the caller takes the innermost scope in which one table holds a column of that
    name, the way SQL resolves it; an ``INSERT`` column list's is the target table.
    ``from_regex``: the facade fell back to the regex, so the text was not parsed, the
    column's tables are unknown (``scopes`` is empty) and only the timestamp names
    (``regex_facts.TIMESTAMP_COLUMN_NAMES``) are read. A flag rather than a ``None``
    scope, so the stored form (``stored_facts``, which leaves empty values out) tells
    "not parsed" from "parsed, no stored table" (#935).
    """

    name: str
    scopes: tuple[tuple[TableRef, ...], ...] = ()
    from_regex: bool = False
