import pytest

from src.contracts.schema_design_input import NormalizedDataType as N
from src.tools.schema.aurora_common.type_map import resolve_mysql_type, resolve_pg_type


@pytest.mark.parametrize(
    "normalized,max_length,expected_type",
    [
        (N.integer, None, "BIGINT"),
        (N.boolean, None, "BOOLEAN"),
        (N.date, None, "DATE"),
        (N.datetime, None, "TIMESTAMP"),
        (N.timestamp, None, "TIMESTAMPTZ"),
        (N.binary, None, "BYTEA"),
        (N.blob, None, "BYTEA"),
        (N.json, None, "JSONB"),
        (N.xml, None, "XML"),
        (N.uuid, None, "UUID"),
        (N.text, None, "TEXT"),
        (N.string, 255, "VARCHAR(255)"),
        (N.string, 40, "VARCHAR(40)"),
        (N.floating_point, None, "DOUBLE PRECISION"),
    ],
)
def test_resolve_pg_type_deterministic(normalized, max_length, expected_type):
    res = resolve_pg_type(normalized, max_length=max_length)
    assert res.aurora_type == expected_type
    assert res.needs_judgment is False


def test_floating_point_never_resolves_to_a_constrained_numeric():
    """SQL Server float/real and Oracle FLOAT/BINARY_FLOAT/BINARY_DOUBLE are
    approximate, not exact: even with a (meaningless) precision/scale given,
    the resolver must still answer DOUBLE PRECISION / DOUBLE, never a
    NUMERIC(n,0)/DECIMAL(n,0) that would truncate every fractional value."""
    pg = resolve_pg_type(N.floating_point, numeric_precision=53, numeric_scale=0)
    assert pg.aurora_type == "DOUBLE PRECISION"
    assert pg.needs_judgment is False

    my = resolve_mysql_type(N.floating_point, numeric_precision=53, numeric_scale=0)
    assert my.aurora_type == "DOUBLE"
    assert my.needs_judgment is False


def test_oracle_float_over_double_precision_is_exact_on_pg_flagged_on_mysql():
    """Oracle FLOAT(p) with p over a double's 53-bit mantissa needs more
    precision than DOUBLE PRECISION/DOUBLE can hold exactly.

    PostgreSQL NUMERIC is arbitrary-precision and exact, so it is a confident
    carry-over there; Aurora MySQL has no equivalent, so DOUBLE is flagged
    rather than silently losing precision (#156, suggestion).
    """
    pg = resolve_pg_type(N.floating_point, numeric_precision=126, numeric_scale=0)
    assert pg.aurora_type == "NUMERIC"
    assert pg.needs_judgment is False

    my = resolve_mysql_type(N.floating_point, numeric_precision=126, numeric_scale=0)
    assert my.aurora_type == "DOUBLE"
    assert my.needs_judgment is True
    assert "126" in my.reason


def test_oracle_binary_double_has_no_precision_and_is_unaffected():
    """BINARY_FLOAT/BINARY_DOUBLE report no numeric_precision at all, so the
    over-53-bits special case never triggers for them - just plain DOUBLE."""
    pg = resolve_pg_type(N.floating_point, numeric_precision=None, numeric_scale=None)
    assert pg.aurora_type == "DOUBLE PRECISION"
    assert pg.needs_judgment is False


@pytest.mark.parametrize(
    ("precision", "scale", "pg_expected", "mysql_expected"),
    [
        (10, 2, "NUMERIC(10,2)", "DECIMAL(10,2)"),
        (38, 0, "NUMERIC(38,0)", "DECIMAL(38,0)"),
        # Precision with no scale is a bare DECIMAL(p), which both engines read
        # as scale 0. A translation, not a guess.
        (10, None, "NUMERIC(10,0)", "DECIMAL(10,0)"),
    ],
)
def test_known_precision_resolves_deterministically(precision, scale, pg_expected, mysql_expected):
    pg = resolve_pg_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert pg.aurora_type == pg_expected
    assert pg.needs_judgment is False

    my = resolve_mysql_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert my.aurora_type == mysql_expected
    assert my.needs_judgment is False


def test_unknown_precision_is_flagged_on_both_engines():
    """Both engines lose something without p/s, but not the same thing.

    Unconstrained PG ``NUMERIC`` is arbitrary-precision and exact, so it loses
    the *constraint*, not the data. Bare MySQL ``DECIMAL`` is ``(10,0)`` and
    truncates, so the fallback is a wide ``DECIMAL(38,10)``. Either way the
    designer is asked, rather than a silent choice being made.
    """
    pg = resolve_pg_type(N.decimal)
    assert pg.aurora_type == "NUMERIC"
    assert pg.needs_judgment is True
    assert "precision" in pg.reason.lower()

    my = resolve_mysql_type(N.decimal)
    assert my.aurora_type == "DECIMAL(38,10)"
    assert my.needs_judgment is True


@pytest.mark.parametrize(("precision", "scale"), [(2, 5), (0, 0), (-1, 0)])
def test_unusable_precision_falls_back_rather_than_emitting_bad_ddl(precision, scale):
    """Scale above precision, or no precision at all, is not usable.

    Emitting ``NUMERIC(2,5)`` would produce DDL the target rejects at CREATE
    TABLE time, which is worse than falling back and asking.
    """
    pg = resolve_pg_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert pg.aurora_type == "NUMERIC"
    assert pg.needs_judgment is True


def test_scale_over_precision_reason_names_the_actual_values():
    """NUMBER(2,5): precision is known, so the reason should say exactly what
    is wrong with it, not fall back to the generic "unknown" message that
    applies when there is no precision at all."""
    pg = resolve_pg_type(N.decimal, numeric_precision=2, numeric_scale=5)
    assert "NUMERIC(2,5)" in pg.reason
    assert "scale" in pg.reason.lower()
    assert "unknown" not in pg.reason.lower()

    my = resolve_mysql_type(N.decimal, numeric_precision=2, numeric_scale=5)
    assert "DECIMAL(2,5)" in my.reason
    assert "scale" in my.reason.lower()
    assert "unknown" not in my.reason.lower()


@pytest.mark.parametrize(
    ("precision", "scale", "pg_expected", "mysql_expected"),
    [
        # NUMBER(5,-2): rounds to the nearest 100, holding up to 9,999,900 -
        # a wider range than NUMBER(5,0), expressed as 5 - (-2) = 7 digits,
        # scale 0 (#156, Critical).
        (5, -2, "NUMERIC(7,0)", "DECIMAL(7,0)"),
        (1, -1, "NUMERIC(2,0)", "DECIMAL(2,0)"),
    ],
)
def test_negative_scale_folds_into_precision(precision, scale, pg_expected, mysql_expected):
    pg = resolve_pg_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert pg.aurora_type == pg_expected
    assert pg.needs_judgment is False

    my = resolve_mysql_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert my.aurora_type == mysql_expected
    assert my.needs_judgment is False


def test_negative_scale_still_respects_mysql_limits():
    """NUMBER(60,-10) folds to 70 digits, over Aurora MySQL's 65-digit cap -
    a flagged residual there, but an exact NUMERIC(70,0) on PostgreSQL, which
    has no equivalent cap."""
    my = resolve_mysql_type(N.decimal, numeric_precision=60, numeric_scale=-10)
    assert my.needs_judgment is True
    assert "70" in my.reason

    pg = resolve_pg_type(N.decimal, numeric_precision=60, numeric_scale=-10)
    assert pg.aurora_type == "NUMERIC(70,0)"
    assert pg.needs_judgment is False


@pytest.mark.parametrize(
    ("precision", "scale"),
    [
        (66, 0),  # precision over Aurora MySQL's 65-digit cap
        (38, 35),  # scale over Aurora MySQL's 30-digit cap
        (126, 0),  # Oracle NUMBER's max precision, far over the cap
    ],
)
def test_mysql_decimal_over_limit_falls_back_rather_than_emitting_bad_ddl(precision, scale):
    """Aurora MySQL's DECIMAL/NUMERIC caps at precision 65, scale 30; CREATE
    TABLE rejects anything wider, so a known-but-out-of-range precision/scale
    must still flag a residual rather than emit invalid DDL (I1, #156).

    PostgreSQL has no equivalent cap, so the same precision/scale resolves
    exactly on that engine - this is MySQL-only.
    """
    my = resolve_mysql_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert my.aurora_type == "DECIMAL(38,10)"
    assert my.needs_judgment is True

    pg = resolve_pg_type(N.decimal, numeric_precision=precision, numeric_scale=scale)
    assert pg.needs_judgment is False
    assert pg.aurora_type == f"NUMERIC({precision},{scale})"


def test_mysql_decimal_at_limit_boundary_is_deterministic():
    """Precision 65 / scale 30 are themselves valid - only strictly over fails."""
    my = resolve_mysql_type(N.decimal, numeric_precision=65, numeric_scale=30)
    assert my.aurora_type == "DECIMAL(65,30)"
    assert my.needs_judgment is False


def test_precision_does_not_leak_into_string_resolution():
    """``max_length`` and ``numeric_precision`` are different fields.

    Oracle historically smuggled NUMBER precision through ``max_length``, so
    this guards the separation.
    """
    res = resolve_pg_type(N.string, max_length=64, numeric_precision=10, numeric_scale=2)
    assert res.aurora_type == "VARCHAR(64)"
    assert res.needs_judgment is False


def test_string_without_length_needs_judgment():
    res = resolve_pg_type(N.string, max_length=None)
    assert res.needs_judgment is True
    assert res.aurora_type == "TEXT"  # safe fallback, but flagged
    assert "length" in res.reason.lower()


def test_missing_normalized_type_needs_judgment():
    res = resolve_pg_type(None, max_length=None)
    assert res.needs_judgment is True
    assert "not normalized" in res.reason.lower()
