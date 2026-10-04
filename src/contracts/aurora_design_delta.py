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
MAX_ENUM_VALUES = 100
MAX_ENUM_VALUE_LENGTH = 64
MAX_ENUM_LENGTH = 2000

# Base type names each engine accepts (lower case, single spaces). A type is
# a base name, an optional (n) / (p, s), an optional engine-specific suffix and,
# on PostgreSQL only, [] array suffixes. Anything else, including a function
# call such as pg_sleep(10), is rejected.
_PG_BASES = (
    frozenset("""smallint integer int int2 int4 int8 bigint decimal numeric real float float4 float8
    smallserial serial serial2 serial4 serial8 bigserial money char character varchar text citext
    bytea timestamp timestamptz date time timetz interval boolean bool uuid json jsonb xml
    inet cidr macaddr macaddr8 tsvector tsquery point line lseg box path polygon circle bit
    varbit int4range int8range numrange tsrange tstzrange daterange oid hstore geometry
    geography vector""".split()) | {"double precision", "character varying", "bit varying"}
)
_MYSQL_BASES = (
    frozenset(
        """tinyint smallint mediumint int integer bigint decimal dec numeric fixed float double
    real bit bool boolean date datetime timestamp time year char varchar binary varbinary
    tinyblob blob mediumblob longblob tinytext text mediumtext longtext json geometry point
    linestring polygon multipoint multilinestring multipolygon geometrycollection nchar
    nvarchar""".split()
    )
    | {"double precision", "national char", "national varchar"}
)
_PG_SUFFIXES = frozenset({"", "with time zone", "without time zone"})
_MYSQL_SUFFIXES = frozenset({"", "unsigned", "signed", "zerofill", "unsigned zerofill"})
ENGINE_TYPE_RULES = {
    "aurora_postgresql": (_PG_BASES, _PG_SUFFIXES, True),
    "aurora_mysql": (_MYSQL_BASES, _MYSQL_SUFFIXES, False),
}

_TYPE_SHAPE = re.compile(
    r"(?P<pre>[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)*)"
    r"(?P<params>\(\s*\d+\s*(?:,\s*\d+\s*)?\))?"
    r"(?P<post>(?: [A-Za-z][A-Za-z0-9_]*)*)"
    r"(?P<arrays>(?:\[\])*)"
)
# MySQL ENUM('a','b') / SET('a','b'): single-quoted literals, '' escapes only.
_LITERAL = r"'(?:[^'\\\n\r]|'')*'"
_ENUM_GRAMMAR = re.compile(
    rf"(?:ENUM|SET)\s*\(\s*{_LITERAL}(?:\s*,\s*{_LITERAL})*\s*\)", re.IGNORECASE
)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")


def is_enum_type(aurora_type: str) -> bool:
    return bool(_ENUM_GRAMMAR.fullmatch(aurora_type))


def _check_enum(value: str) -> str:
    literals = re.findall(_LITERAL, value)
    if len(value) > MAX_ENUM_LENGTH:
        raise ValueError(f"ENUM/SET type longer than {MAX_ENUM_LENGTH} characters")
    if len(literals) > MAX_ENUM_VALUES:
        raise ValueError(f"ENUM/SET has more than {MAX_ENUM_VALUES} values")
    if any(len(lit) - 2 > MAX_ENUM_VALUE_LENGTH for lit in literals):
        raise ValueError(f"ENUM/SET value longer than {MAX_ENUM_VALUE_LENGTH} characters")
    return value


def _type_error(engine: str | None, normalized: str) -> str | None:
    """Why ``normalized`` is not a valid type for ``engine`` (None = either engine)."""
    shape = _TYPE_SHAPE.fullmatch(normalized)
    if shape is None:
        return "is not a plain SQL type"
    words = shape.group("pre").lower().split()
    post = shape.group("post").lower().split()
    engines = [engine] if engine else list(ENGINE_TYPE_RULES)
    reasons = []
    for name in engines:
        bases, suffixes, arrays_ok = ENGINE_TYPE_RULES[name]
        base_len = next((n for n in (3, 2, 1) if " ".join(words[:n]) in bases), 0)
        if not base_len:
            reasons.append(f"'{' '.join(words)}' is not an {name} type")
            continue
        suffix = " ".join([*words[base_len:], *post])
        if suffix not in suffixes:
            reasons.append(f"'{suffix}' is not a valid {name} type modifier")
            continue
        if shape.group("arrays") and not arrays_ok:
            reasons.append(f"array types are not {name} types")
            continue
        return None
    return "; ".join(reasons)


def validate_aurora_type(value: str, engine: str | None = None) -> str:
    """Return ``value`` normalized (trimmed, single spaces) or raise ``ValueError``.

    A type is an allowlisted base type name for the engine (``BIGINT``,
    ``VARCHAR``, ``DOUBLE PRECISION``, ...), an optional ``(n)`` / ``(p, s)``,
    an engine-specific suffix (``WITH TIME ZONE`` on PostgreSQL, ``UNSIGNED``
    on MySQL) and, on PostgreSQL, ``[]`` arrays; or, on MySQL, ``ENUM`` /
    ``SET`` with quoted literals (capped in count and length). ``engine=None``
    accepts a type valid for either engine; the merge re-checks per engine.
    Rejects anything else, including ``BIGINT); DROP TABLE users; --``,
    ``INT, `evil` TEXT``, ``INT NOT NULL`` and ``pg_sleep(10)``.
    """
    if not isinstance(value, str):
        raise ValueError("aurora_type must be a string")
    stripped = value.strip()
    if is_enum_type(stripped):
        if engine not in (None, "aurora_mysql"):
            raise ValueError(
                f"aurora_type {value!r}: ENUM/SET literal types are Aurora MySQL only; on "
                "Aurora PostgreSQL use TEXT (or a lookup table)"
            )
        return _check_enum(stripped)
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("aurora_type must not be blank")
    if len(normalized) > MAX_TYPE_LENGTH:
        raise ValueError(f"aurora_type longer than {MAX_TYPE_LENGTH} characters")
    error = _type_error(engine, normalized)
    if error:
        raise ValueError(
            f"aurora_type {value!r} {error} (give only the type, e.g. BIGINT, "
            "VARCHAR(255), NUMERIC(10,2))"
        )
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
