# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table's key, and whether AWS DMS can validate on it: one rule for step 1's contract, the
data movement plan and the DMS gate (#2042).

A table's **key** is its primary key; otherwise its smallest unique index (by column count,
then name) that is non-partial, has no expressions and whose columns are all NOT NULL. The
plan keys Debezium topics on it, and the contract records it (``Rule.key_columns``).

DMS validates on that key only when none of its columns has a type validation refuses (AWS DMS
User Guide, "AWS DMS data validation", Limitations: "Primary key columns cannot be of type
CLOB, BLOB, BINARY, or BYTE"; a VARCHAR or CHAR key column must declare a length under 1024,
"You can't use unbounded data types as a primary key for data validation"; "Validation of NULL
PK/UK values aren't supported"). "Source data types for PostgreSQL" maps TEXT, JSON, JSONB,
ARRAY, CITEXT, XML, HSTORE and the like to NCLOB and BYTEA to BLOB, and partly supports a
VARCHAR with no length: the catalog's ``lob.types`` (``src/catalog/dms_sizing.toml``) lists
them, and this module reads that list rather than keeping its own. DMS 3.6.1 applies the
NOT NULL rule to a PostgreSQL source too: it logs ``No eligible primary/unique key found`` for
a table whose only unique index allows NULLs.

A type this module does not know counts as validatable: when DMS disagrees, the gate fails
on that table instead of naming it an exception, so a doubt never hides a difference.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.contracts.dms_gate import NOT_VALIDATED, NOT_VALIDATED_PREFIX
from src.shared.catalog.dms_sizing import dms_sizing

KeySource = Literal["primary_key", "unique_index"]
KeyProblemCode = Literal["no_key", "key_not_validatable"]

NO_KEY_MESSAGE = (
    "No primary or unique key: DMS cannot validate this table, "
    "and Debezium needs declared key columns."
)
# Key column types validation refuses whatever the dialect's LOB list says (MySQL TEXT and
# BLOB map to WSTRING or BYTES, not to a LOB, but DMS still refuses them as key columns).
_REFUSED_KEY_TYPES = frozenset({
    "text", "tinytext", "mediumtext", "longtext", "ntext", "clob", "nclob", "blob", "tinyblob",
    "mediumblob", "longblob", "bytea", "binary", "varbinary", "byte", "image",
})  # fmt: skip
_LENGTH_BOUNDED = frozenset(
    {"varchar", "char", "character varying", "character", "nvarchar", "nchar", "bpchar"}
)
# A bare CHAR is CHAR(1); every other length-bounded type without a length is unbounded.
_ONE_CHARACTER = frozenset({"char", "character", "nchar"})
MAX_KEY_LENGTH = 1024


@dataclass(frozen=True)
class TableKey:
    columns: tuple[str, ...]
    source: KeySource
    index_name: str | None = None  # the unique index's name, for a unique_index key

    @property
    def label(self) -> str:
        return "primary key" if self.source == "primary_key" else f"unique index {self.index_name}"


@dataclass(frozen=True)
class KeyProblem:
    """Why DMS cannot validate a table: ``no_key`` (no key at all, so the plan and Debezium have
    none either) or ``key_not_validatable`` (a key, on a column type validation refuses)."""

    code: KeyProblemCode
    message: str


def _unique_candidates(table: Mapping[str, Any]) -> list[tuple[int, str, list[str], bool]]:
    """(column count, name, columns, all NOT NULL) of each non-partial, expression-free unique
    index, smallest first."""
    not_null = {str(c["column_name"]) for c in table.get("columns") or [] if not c.get("nullable")}
    out = []
    for ix in table.get("indexes") or []:
        cols = [str(c) for c in ix.get("columns") or []]
        if not ix.get("is_unique") or ix.get("predicate") or ix.get("has_expressions") or not cols:
            continue
        out.append(
            (len(cols), str(ix.get("index_name") or ""), cols, all(c in not_null for c in cols))
        )
    return sorted(out)


def table_key(table: Mapping[str, Any]) -> TableKey | None:
    """The table's key (see the module docstring), or None when it has none."""
    pk = [str(c) for c in table.get("primary_key") or []]
    if pk:
        return TableKey(tuple(pk), "primary_key")
    for _, name, cols, not_null in _unique_candidates(table):
        if not_null:
            return TableKey(tuple(cols), "unique_index", name)
    return None


def key_type_problem(
    data_type: str, max_length: int | None = None, dialect: str | None = None
) -> str | None:
    """Why DMS cannot validate on a key column of this type, or None when it can. A length in
    the type (``varchar(255)``) wins over ``max_length``; ``dialect`` None reads every dialect's
    LOB list."""
    t = " ".join(str(data_type).lower().split())
    if t.startswith("_") or t.endswith("]") or t == "array":
        return f"an array ({data_type}), a LOB to DMS"
    base, _, rest = t.partition("(")
    base = base.strip()
    lob = dms_sizing().lob.types
    lob_types = set(lob.get(dialect, ())) if dialect else {n for v in lob.values() for n in v}
    if base in _REFUSED_KEY_TYPES or base in lob_types:
        return f"{data_type}, a LOB or binary type to DMS"
    if base in _LENGTH_BOUNDED:
        declared = rest.partition(")")[0].split(",")[0].strip()
        length = int(declared) if declared.isdigit() else max_length
        if length is None:
            return None if base in _ONE_CHARACTER else f"an unbounded {base}"
        if length >= MAX_KEY_LENGTH:
            return f"{base}({length}), {MAX_KEY_LENGTH} characters or more"
    return None


def key_type_message(label: str, bad: Sequence[tuple[str, str]]) -> str:
    """The finding message of a key with columns DMS cannot validate on: (column, why) pairs."""
    named = ", ".join(f"{column} is {why}" for column, why in bad)
    return f"Its {label} has a column type DMS cannot validate on: {named}."


def gate_reason(message: str) -> str:
    """The gate's exception reason for a key finding's message: the fixed ``not validated: no
    primary or unique key`` for a keyless table, else the finding's own words."""
    if message == NO_KEY_MESSAGE:
        return NOT_VALIDATED
    text = message.strip().rstrip(".")
    return f"{NOT_VALIDATED_PREFIX}{text[:1].lower()}{text[1:]}"


def validation_problem(table: Mapping[str, Any], dialect: str | None) -> KeyProblem | None:
    """Why DMS cannot validate the table (a collector table), or None when it can."""
    key = table_key(table)
    if key is None:
        nullable = next(
            ((name, cols) for _, name, cols, nn in _unique_candidates(table) if not nn), None
        )
        if nullable is None:
            return KeyProblem("no_key", NO_KEY_MESSAGE)
        name, cols = nullable
        not_null = {
            str(c["column_name"]) for c in table.get("columns") or [] if not c.get("nullable")
        }
        nulls = ", ".join(c for c in cols if c not in not_null)
        return KeyProblem(
            "no_key",
            f"No primary key, and its unique index {name} allows NULLs in {nulls}, "
            "which DMS cannot validate on.",
        )
    columns = {str(c["column_name"]): c for c in table.get("columns") or []}
    bad = [
        (c, why)
        for c in key.columns
        if c in columns
        and (why := key_type_problem(str(columns[c].get("data_type") or ""),
                                     columns[c].get("max_length"), dialect))  # fmt: skip
    ]
    return KeyProblem("key_not_validatable", key_type_message(key.label, bad)) if bad else None
