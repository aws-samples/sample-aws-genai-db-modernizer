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
    ],
)
def test_resolve_pg_type_deterministic(normalized, max_length, expected_type):
    res = resolve_pg_type(normalized, max_length=max_length)
    assert res.aurora_type == expected_type
    assert res.needs_judgment is False


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
