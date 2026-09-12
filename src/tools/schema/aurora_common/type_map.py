"""Map normalized source column types to Aurora PostgreSQL native types.

The collector already normalizes every source column into a 13-value
``NormalizedDataType`` enum, so this map is source-agnostic: an Oracle NUMBER,
a SQL Server INT, and a MySQL BIGINT all arrive as ``integer``. Anything the map
cannot resolve confidently (e.g. a string with no known length) is returned with
``needs_judgment=True`` and a reason string, never silently guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contracts.schema_design_input import NormalizedDataType


@dataclass(frozen=True)
class TypeResolution:
    """Resolved Aurora type for one source column.

    ``aurora_type`` always holds a usable type (a safe fallback when judgment is
    needed). ``needs_judgment`` flags that the LLM should confirm or refine it,
    and ``reason`` explains why.
    """

    aurora_type: str
    needs_judgment: bool
    reason: str = ""


# Direct, unambiguous mappings that ignore length.
_DIRECT: dict[NormalizedDataType, str] = {
    NormalizedDataType.integer: "BIGINT",
    NormalizedDataType.decimal: "NUMERIC",
    NormalizedDataType.boolean: "BOOLEAN",
    NormalizedDataType.date: "DATE",
    NormalizedDataType.datetime: "TIMESTAMP",
    NormalizedDataType.timestamp: "TIMESTAMPTZ",
    NormalizedDataType.binary: "BYTEA",
    NormalizedDataType.blob: "BYTEA",
    NormalizedDataType.json: "JSONB",
    NormalizedDataType.xml: "XML",
    NormalizedDataType.uuid: "UUID",
    NormalizedDataType.text: "TEXT",
}


def resolve_pg_type(
    normalized: NormalizedDataType | None,
    *,
    max_length: int | None = None,
) -> TypeResolution:
    """Resolve one column's Aurora PostgreSQL type from its normalized type."""
    if normalized is None:
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="Source type was not normalized; confirm the intended column type.",
        )

    if normalized in _DIRECT:
        return TypeResolution(aurora_type=_DIRECT[normalized], needs_judgment=False)

    # Only `string` remains: length-dependent.
    if normalized is NormalizedDataType.string:
        if max_length and max_length > 0:
            return TypeResolution(aurora_type=f"VARCHAR({max_length})", needs_judgment=False)
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="String column has no known max length; confirm VARCHAR(n) vs TEXT.",
        )

    # Defensive: any future enum value we have not mapped.
    return TypeResolution(
        aurora_type="TEXT",
        needs_judgment=True,
        reason=f"No deterministic Aurora mapping for normalized type '{normalized}'.",
    )


# Aurora MySQL direct mappings (length-independent). decimal is intentionally
# absent: bare MySQL DECIMAL is (10,0) and lossy, so it needs judgment.
_MYSQL_DIRECT: dict[NormalizedDataType, str] = {
    NormalizedDataType.integer: "BIGINT",
    NormalizedDataType.boolean: "BOOLEAN",
    NormalizedDataType.date: "DATE",
    NormalizedDataType.datetime: "DATETIME",
    NormalizedDataType.timestamp: "TIMESTAMP",
    NormalizedDataType.binary: "BLOB",
    NormalizedDataType.blob: "LONGBLOB",
    NormalizedDataType.json: "JSON",
    NormalizedDataType.xml: "LONGTEXT",
    NormalizedDataType.uuid: "CHAR(36)",
    NormalizedDataType.text: "TEXT",
}


def resolve_mysql_type(
    normalized: NormalizedDataType | None,
    *,
    max_length: int | None = None,
) -> TypeResolution:
    """Resolve one column's Aurora MySQL type from its normalized type."""
    if normalized is None:
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="Source type was not normalized; confirm the intended column type.",
        )
    if normalized is NormalizedDataType.decimal:
        return TypeResolution(
            aurora_type="DECIMAL(38,10)",
            needs_judgment=True,
            reason="Decimal precision/scale unknown; confirm DECIMAL(p,s) (bare DECIMAL is lossy).",
        )
    if normalized in _MYSQL_DIRECT:
        return TypeResolution(aurora_type=_MYSQL_DIRECT[normalized], needs_judgment=False)
    if normalized is NormalizedDataType.string:
        if max_length and max_length > 0:
            return TypeResolution(aurora_type=f"VARCHAR({max_length})", needs_judgment=False)
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="String column has no known max length; confirm VARCHAR(n) vs TEXT.",
        )
    return TypeResolution(
        aurora_type="TEXT",
        needs_judgment=True,
        reason=f"No deterministic Aurora MySQL mapping for normalized type '{normalized}'.",
    )
