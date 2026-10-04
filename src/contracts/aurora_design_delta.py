"""Aurora schema-design delta contract (issue #273).

An Aurora PostgreSQL / MySQL design is a 1:1 carry-over of the deterministic
draft (``src/tools/schema/aurora_common/draft_builder.py``) plus a few
Aurora-level decisions. The model writes only those decisions, as a delta
against the draft, so its output size does not depend on the schema size.
``merge_design_delta`` (``src/tools/schema/aurora_common/delta_merge.py``)
rebuilds the draft, applies the delta, regenerates the DDL and returns the
full ``Aurora*ModelOutputContract``.

Everything the delta contributes to DDL is LLM output derived from untrusted
collector text, so it is never pasted into DDL as written: column types must
match a strict type grammar (:func:`validate_aurora_type`), and indexes are
structured (name, columns, flags) and rendered by the DDL generator with
quoted identifiers. A legacy ``CREATE INDEX`` string is accepted only when it
parses completely under a strict grammar, and is then re-rendered the same way.

Shared by both Aurora engines: the merged output is validated against the
engine's own contract afterwards.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .aurora_postgresql_model_output import AppLayerNote, Optimization
from .schema_design_output import TradeOff

DELTA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Strict grammars for anything that reaches DDL
# ---------------------------------------------------------------------------

MAX_TYPE_LENGTH = 64

# One or more words, optional (n) / (p, s), optional trailing words
# (UNSIGNED, WITH TIME ZONE), optional [] array suffixes.
_TYPE_GRAMMAR = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)*"
    r"(?:\(\s*\d+\s*(?:,\s*\d+\s*)?\))?"
    r"(?: [A-Za-z][A-Za-z0-9_]*)*"
    r"(?:\[\])*"
)
# MySQL ENUM('a','b') / SET('a','b'): single-quoted literals, '' escapes only.
_LITERAL = r"'(?:[^'\\\n\r]|'')*'"
_ENUM_GRAMMAR = re.compile(
    rf"(?:ENUM|SET)\s*\(\s*{_LITERAL}(?:\s*,\s*{_LITERAL})*\s*\)", re.IGNORECASE
)
# Words that turn a type into a column constraint, a default or a statement.
_FORBIDDEN_TYPE_WORDS = frozenset(
    {
        "ALTER",
        "AS",
        "AUTO_INCREMENT",
        "CHECK",
        "COLLATE",
        "CONSTRAINT",
        "CREATE",
        "DEFAULT",
        "DELETE",
        "DROP",
        "GENERATED",
        "GRANT",
        "IDENTITY",
        "INSERT",
        "KEY",
        "NOT",
        "NULL",
        "ON",
        "PRIMARY",
        "REFERENCES",
        "REVOKE",
        "SELECT",
        "TRUNCATE",
        "UNIQUE",
        "UPDATE",
    }
)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")


def is_enum_type(aurora_type: str) -> bool:
    return bool(_ENUM_GRAMMAR.fullmatch(aurora_type))


def validate_aurora_type(value: str) -> str:
    """Return ``value`` normalized (trimmed, single spaces) or raise ``ValueError``.

    Accepts ``BIGINT``, ``VARCHAR(255)``, ``NUMERIC(10, 2)``, ``DOUBLE PRECISION``,
    ``TIMESTAMP(3) WITH TIME ZONE``, ``INT UNSIGNED``, ``TEXT[]`` and MySQL
    ``ENUM('a','b')`` / ``SET(...)``. Rejects anything else, including
    ``BIGINT); DROP TABLE users; --`` and ``INT, `evil` TEXT``.
    """
    if not isinstance(value, str):
        raise ValueError("aurora_type must be a string")
    if is_enum_type(value.strip()):
        return value.strip()
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("aurora_type must not be blank")
    if len(normalized) > MAX_TYPE_LENGTH:
        raise ValueError(f"aurora_type longer than {MAX_TYPE_LENGTH} characters")
    if not _TYPE_GRAMMAR.fullmatch(normalized):
        raise ValueError(
            f"aurora_type {value!r} is not a plain SQL type (e.g. BIGINT, VARCHAR(255), "
            "NUMERIC(10,2), TIMESTAMP WITH TIME ZONE, ENUM('a','b'))"
        )
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", normalized.upper()))
    bad = sorted(words & _FORBIDDEN_TYPE_WORDS)
    if bad:
        raise ValueError(f"aurora_type {value!r} contains {', '.join(bad)}; give only the type")
    return normalized


def _identifier(value: str, what: str) -> str:
    value = value.strip()
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{what} {value!r} must be a plain identifier (letters, digits, _ or $, "
            "at most 63 characters, not starting with a digit)"
        )
    return value


# ---------------------------------------------------------------------------
# Delta entries
# ---------------------------------------------------------------------------


class ColumnTypeChange(BaseModel):
    """Set one column's Aurora type (resolve a residual, or change a draft type)."""

    column: str = Field(..., description="Column name, exactly as in the design view")
    aurora_type: str = Field(..., description="Target Aurora type, e.g. BIGINT or VARCHAR(255)")
    reason: str = Field(default="", description="Why this type")

    model_config = ConfigDict(extra="forbid")

    @field_validator("aurora_type")
    @classmethod
    def _type(cls, v: str) -> str:
        return validate_aurora_type(v)


class IndexSpec(BaseModel):
    """A secondary index, rendered by the DDL generator with quoted identifiers."""

    index_name: str = Field(..., description="Index name (plain identifier)")
    columns: list[str] = Field(..., min_length=1, description="Indexed columns of this table")
    unique: bool = Field(default=False, description="UNIQUE index")
    method: Literal["btree", "hash", "gin", "gist", "brin"] | None = Field(
        default=None, description="Index access method (Aurora PostgreSQL only)"
    )
    include: list[str] = Field(
        default_factory=list, description="Covering INCLUDE columns (Aurora PostgreSQL only)"
    )
    where: str | None = Field(
        default=None,
        description=(
            "Partial-index predicate (Aurora PostgreSQL only): columns, comparisons, "
            "IS [NOT] NULL, AND/OR/NOT, numbers, 'strings', TRUE/FALSE"
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("index_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _identifier(v, "index_name")


class TableDelta(BaseModel):
    """Changes to one draft table. Tables not listed carry over unchanged."""

    table_name: str = Field(..., description="A table from the design view")
    add_indexes: list[IndexSpec | str] = Field(
        default_factory=list,
        description=(
            "New indexes on this table. Prefer the structured form; a plain "
            "'CREATE [UNIQUE] INDEX name ON table (col, ...)' string is also accepted"
        ),
    )
    modify_indexes: list[IndexSpec | str] = Field(
        default_factory=list,
        description="Replacement definitions for draft indexes, matched by index_name",
    )
    remove_indexes: list[str] = Field(
        default_factory=list, description="Names of draft indexes to drop"
    )
    column_types: list[ColumnTypeChange] = Field(
        default_factory=list, description="Per-column type overrides"
    )

    model_config = ConfigDict(extra="forbid")


class TypeRule(BaseModel):
    """Resolve every residual column of one source data type at once.

    Applies only to residual columns (``needs_judgment`` in the draft) whose
    source ``data_type`` matches, case-insensitively, and that have no
    per-column override in ``tables[].column_types``.
    """

    source_data_type: str = Field(
        ..., min_length=1, description="Source data_type, as listed in residual_types"
    )
    aurora_type: str = Field(..., description="Target Aurora type")
    reason: str = Field(default="", description="Why this type")

    model_config = ConfigDict(extra="forbid")

    @field_validator("aurora_type")
    @classmethod
    def _type(cls, v: str) -> str:
        return validate_aurora_type(v)


class AuroraDesignDeltaContract(BaseModel):
    """What the model decides on top of the deterministic Aurora draft."""

    delta_version: Literal["1.0"] = Field(..., description='Delta contract version; must be "1.0"')
    tables: list[TableDelta] = Field(
        default_factory=list, description="Per-table index and type changes"
    )
    type_rules: list[TypeRule] = Field(
        default_factory=list, description="Residual resolutions by source data type"
    )
    optimizations: list[Optimization] = Field(
        default_factory=list,
        description="Aurora optimizations (partitioning, index, read_replica, io_optimized)",
    )
    app_layer_notes: list[AppLayerNote] = Field(
        default_factory=list, description="Source features that need application-layer handling"
    )
    trade_offs: list[TradeOff] = Field(
        default_factory=list, description="Significant design decisions, written for a CTO"
    )

    model_config = ConfigDict(extra="forbid")


def is_design_delta(response: object) -> bool:
    """True when an LLM response is a delta rather than a full output contract."""
    return isinstance(response, dict) and "delta_version" in response
