"""Map normalized source column types to Aurora PostgreSQL native types.

The collector already normalizes every source column into a 14-value
``NormalizedDataType`` enum, so this map is source-agnostic: an Oracle NUMBER,
a SQL Server INT, and a MySQL BIGINT all arrive as ``integer``. Anything the map
cannot resolve confidently (e.g. a string with no known length) is returned with
``needs_judgment=True`` and a reason string, never silently guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contracts.aurora_design_delta import (
    MYSQL_DECIMAL_PRECISION_MAX,
    MYSQL_DECIMAL_SCALE_MAX,
    mysql_decimal_limit_message,
)
from src.contracts.schema_design_input import NormalizedDataType

# IEEE-754 double has a 53-bit mantissa. Oracle FLOAT(p) is the only source
# that reports a precision for a floating type at all (SQL Server float(n)
# is capped at 53; BINARY_FLOAT/BINARY_DOUBLE report none), and Oracle allows
# p above 53 - at that point DOUBLE PRECISION/DOUBLE would hold less
# precision than the source actually has (#156, suggestion).
_IEEE_DOUBLE_PRECISION_BITS = 53


@dataclass(frozen=True)
class TypeResolution:
    """Resolved Aurora type for one source column.

    ``aurora_type`` always holds a usable type (a safe fallback when judgment is
    needed). ``needs_judgment`` flags that the LLM should confirm or refine it,
    and ``reason`` explains why. ``note`` is an application-layer remark on a
    resolved type (e.g. time zones dropped by a cross-engine mapping).
    """

    aurora_type: str
    needs_judgment: bool
    reason: str = ""
    note: str = ""


def _apply_negative_scale(precision: int, scale: int) -> tuple[int, int]:
    """Fold a negative scale into the precision, or pass through unchanged.

    Oracle's ``NUMBER(p, s)`` with ``s < 0`` rounds to the left of the decimal
    point: ``NUMBER(5,-2)`` holds multiples of 100, up to 9,999,900 - a real,
    wider range than ``NUMBER(5,0)``, not a misparse to discard (#156). Neither
    engine's ``NUMERIC``/``DECIMAL`` grammar takes a negative scale, so this
    expresses the same magnitude as an all-digits exact type: ``p - s`` digits
    (``p`` plus the ``s`` digits the rounding drops), scale 0.
    """
    if scale < 0:
        return precision - scale, 0
    return precision, scale


def normalize_precision_scale(
    precision: int | None,
    scale: int | None,
    *,
    max_precision: int | None = None,
    max_scale: int | None = None,
) -> tuple[int, int] | None:
    """Validate and default (precision, scale), or ``None`` when unusable.

    Scale defaults to 0 when absent but precision is present: that is what both
    engines mean by a bare ``DECIMAL(p)``, so it is a translation rather than a
    guess. A negative scale is folded into the precision (``_apply_negative_scale``)
    rather than discarded. Precision without any digits is meaningless, so 0 is
    rejected, as is a scale above precision (DDL the target would reject at
    ``CREATE TABLE`` time). ``max_precision``/``max_scale`` enforce an engine's
    own limits (e.g. MySQL's ``DECIMAL`` caps at precision 65, scale 30, #156)
    the same way: out of range is unusable, not a guess.
    """
    if not precision or precision <= 0:
        return None
    p, s = _apply_negative_scale(precision, scale if scale is not None else 0)
    if s > p:
        return None
    if max_precision is not None and p > max_precision:
        return None
    if max_scale is not None and s > max_scale:
        return None
    return (p, s)


def _exact_numeric(
    base: str,
    precision: int | None,
    scale: int | None,
    *,
    max_precision: int | None = None,
    max_scale: int | None = None,
) -> str | None:
    """Render ``BASE(p,s)`` when precision is known and usable, else ``None``."""
    resolved = normalize_precision_scale(
        precision, scale, max_precision=max_precision, max_scale=max_scale
    )
    if resolved is None:
        return None
    return f"{base}({resolved[0]},{resolved[1]})"


# Direct, unambiguous mappings that ignore length. `decimal` is deliberately
# absent: unconstrained PG NUMERIC is exact, so it never loses data, but it
# also enforces nothing. When precision is known we emit NUMERIC(p,s) so the
# target rejects what the source rejected.
_DIRECT: dict[NormalizedDataType, str] = {
    NormalizedDataType.integer: "BIGINT",
    # A floating source type (FLOAT/REAL/DOUBLE PRECISION) is approximate, not
    # exact: DOUBLE PRECISION holds a single- or double-precision source
    # (REAL/FLOAT, BINARY_FLOAT/BINARY_DOUBLE) exactly, unlike resolving it as
    # decimal and reaching an unconstrained-precision NUMERIC(n,0) (#156).
    # Oracle FLOAT(p) above a double's 53-bit mantissa is the one case this
    # table does not cover - resolve_pg_type/resolve_mysql_type special-case
    # it before ever reaching this map.
    NormalizedDataType.floating_point: "DOUBLE PRECISION",
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
    numeric_precision: int | None = None,
    numeric_scale: int | None = None,
) -> TypeResolution:
    """Resolve one column's Aurora PostgreSQL type from its normalized type."""
    if normalized is None:
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="Source type was not normalized; confirm the intended column type.",
        )

    if normalized is NormalizedDataType.decimal:
        exact = _exact_numeric("NUMERIC", numeric_precision, numeric_scale)
        if exact:
            return TypeResolution(aurora_type=exact, needs_judgment=False)
        if numeric_precision and numeric_precision > 0:
            # Precision is known but scale exceeds it even after folding any
            # negative scale (e.g. NUMBER(2,5)): a specific, accurate reason,
            # not the generic "unknown" one below.
            eff_precision, eff_scale = _apply_negative_scale(numeric_precision, numeric_scale or 0)
            return TypeResolution(
                aurora_type="NUMERIC",
                needs_judgment=True,
                reason=(
                    f"NUMERIC({eff_precision},{eff_scale}): scale cannot exceed precision; "
                    "confirm the correct precision/scale."
                ),
            )
        # Unconstrained NUMERIC is exact in PostgreSQL, so this is a loss of
        # constraint rather than of data - flagged, not silent.
        return TypeResolution(
            aurora_type="NUMERIC",
            needs_judgment=True,
            reason=(
                "Decimal precision/scale unknown; unconstrained NUMERIC stores the "
                "values exactly but enforces no range. Confirm NUMERIC(p,s)."
            ),
        )

    if (
        normalized is NormalizedDataType.floating_point
        and numeric_precision
        and numeric_precision > _IEEE_DOUBLE_PRECISION_BITS
    ):
        # Oracle FLOAT(p) with p over a double's 53-bit mantissa: unconstrained
        # NUMERIC is exact and arbitrary-precision on PostgreSQL, so this is a
        # confident carry-over, not a residual (same reasoning as unconstrained
        # decimal above - just reached from the opposite direction).
        return TypeResolution(aurora_type="NUMERIC", needs_judgment=False)

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
    # Approximate, not exact (#156): DOUBLE holds a single- or
    # double-precision source exactly, unlike resolving it as decimal and
    # reaching a lossy DECIMAL(10,0). Oracle FLOAT(p) above a double's 53-bit
    # mantissa is the one case this table does not cover - resolve_mysql_type
    # special-cases it before ever reaching this map.
    NormalizedDataType.floating_point: "DOUBLE",
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
    numeric_precision: int | None = None,
    numeric_scale: int | None = None,
) -> TypeResolution:
    """Resolve one column's Aurora MySQL type from its normalized type."""
    if normalized is None:
        return TypeResolution(
            aurora_type="TEXT",
            needs_judgment=True,
            reason="Source type was not normalized; confirm the intended column type.",
        )
    if normalized is NormalizedDataType.decimal:
        exact = _exact_numeric(
            "DECIMAL",
            numeric_precision,
            numeric_scale,
            max_precision=MYSQL_DECIMAL_PRECISION_MAX,
            max_scale=MYSQL_DECIMAL_SCALE_MAX,
        )
        if exact:
            return TypeResolution(aurora_type=exact, needs_judgment=False)
        if numeric_precision and numeric_precision > 0:
            # Collected precision/scale is known but unusable on Aurora MySQL
            # (over its digit caps, or scale over precision, after folding any
            # negative scale into the precision): a flagged residual, never
            # DDL CREATE TABLE would reject.
            eff_precision, eff_scale = _apply_negative_scale(numeric_precision, numeric_scale or 0)
            return TypeResolution(
                aurora_type="DECIMAL(38,10)",
                needs_judgment=True,
                reason=(
                    f"{mysql_decimal_limit_message(eff_precision, eff_scale)}; "
                    "confirm a DECIMAL(p,s) that fits."
                ),
            )
        return TypeResolution(
            aurora_type="DECIMAL(38,10)",
            needs_judgment=True,
            reason="Decimal precision/scale unknown; confirm DECIMAL(p,s) (bare DECIMAL is lossy).",
        )
    if (
        normalized is NormalizedDataType.floating_point
        and numeric_precision
        and numeric_precision > _IEEE_DOUBLE_PRECISION_BITS
    ):
        # Oracle FLOAT(p) with p over a double's 53-bit mantissa: Aurora
        # MySQL has no arbitrary-precision floating type, and there is no
        # fractional scale here to form an exact DECIMAL(p,s) from, so DOUBLE
        # is the fallback - but flagged, since it would hold less precision
        # than the source actually has.
        return TypeResolution(
            aurora_type="DOUBLE",
            needs_judgment=True,
            reason=(
                f"FLOAT({numeric_precision}) carries more precision than Aurora MySQL's "
                "DOUBLE can hold exactly; confirm DOUBLE is acceptable or an exact DECIMAL(p,s)."
            ),
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
