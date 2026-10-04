"""Source data_type -> Aurora type for PostgreSQL and MySQL sources (issue #274)."""

from __future__ import annotations

import pytest

from src.contracts.aurora_design_delta import validate_aurora_type
from src.tools.schema.aurora_common.source_types import resolve_source_type

PG, MY = "aurora_postgresql", "aurora_mysql"


def _type(raw, family, target, **kw):
    res = resolve_source_type(raw, source_family=family, target=target, **kw)
    assert res is not None, raw
    assert not res.needs_judgment, (raw, res.reason)
    validate_aurora_type(res.aurora_type, target)  # every result is allowlisted
    return res.aurora_type


# --- PostgreSQL -> Aurora PostgreSQL: exact, aliases normalised ---------------


@pytest.mark.parametrize(
    "raw,max_length,expected",
    [
        ("integer", None, "INTEGER"),
        ("int4", None, "INTEGER"),
        ("int", None, "INTEGER"),
        ("bigint", None, "BIGINT"),
        ("int8", None, "BIGINT"),
        ("smallint", None, "SMALLINT"),
        ("serial", None, "INTEGER"),
        ("bigserial", None, "BIGINT"),
        ("real", None, "REAL"),
        ("double precision", None, "DOUBLE PRECISION"),
        ("float8", None, "DOUBLE PRECISION"),
        ("numeric", None, "NUMERIC"),
        ("numeric(10,2)", None, "NUMERIC(10,2)"),
        ("decimal(12, 4)", None, "NUMERIC(12,4)"),
        ("boolean", None, "BOOLEAN"),
        ("character varying", None, "VARCHAR"),
        ("character varying", 255, "VARCHAR(255)"),
        ("character varying(100)", None, "VARCHAR(100)"),
        ("varchar", 40, "VARCHAR(40)"),
        ("character", 2, "CHAR(2)"),
        ("bpchar", 3, "CHAR(3)"),
        ("text", None, "TEXT"),
        ("timestamp without time zone", None, "TIMESTAMP"),
        ("timestamp(3) without time zone", None, "TIMESTAMP(3)"),
        ("timestamp with time zone", None, "TIMESTAMPTZ"),
        ("timestamptz", None, "TIMESTAMPTZ"),
        ("time without time zone", None, "TIME"),
        ("time with time zone", None, "TIMETZ"),
        ("interval", None, "INTERVAL"),
        ("interval day to second", None, "INTERVAL"),
        ("date", None, "DATE"),
        ("json", None, "JSON"),
        ("jsonb", None, "JSONB"),
        ("uuid", None, "UUID"),
        ("inet", None, "INET"),
        ("cidr", None, "CIDR"),
        ("tsvector", None, "TSVECTOR"),
        ("int4range", None, "INT4RANGE"),
        ("bytea", None, "BYTEA"),
        ("money", None, "MONEY"),
        ("bit varying(8)", None, "VARBIT(8)"),
        ("citext", None, "CITEXT"),
        ("halfvec", None, "HALFVEC"),
        ("vector(1536)", None, "VECTOR(1536)"),
        # arrays: format_type spelling and the udt_name spelling
        ("integer[]", None, "INTEGER[]"),
        ("character varying[]", None, "VARCHAR[]"),
        ("_int4", None, "INTEGER[]"),
        ("_varchar", None, "VARCHAR[]"),
        ("_text", None, "TEXT[]"),
        ("_inet", None, "INET[]"),
        ("INTEGER", None, "INTEGER"),  # case-insensitive
    ],
)
def test_pg_carry_over(raw, max_length, expected):
    assert _type(raw, "postgresql", PG, max_length=max_length) == expected


@pytest.mark.parametrize("raw", ["ARRAY", "USER-DEFINED", "hotlinked_media_status", "pg_sleep(1)"])
def test_pg_unknown_types_have_no_source_mapping(raw):
    assert resolve_source_type(raw, source_family="postgresql", target=PG) is None


# --- MySQL -> Aurora MySQL: exact, display widths dropped ---------------------


@pytest.mark.parametrize(
    "raw,max_length,expected",
    [
        ("bigint unsigned", None, "BIGINT UNSIGNED"),
        ("bigint(20) unsigned", None, "BIGINT UNSIGNED"),
        ("int(11)", None, "INT"),
        ("int unsigned zerofill", None, "INT UNSIGNED ZEROFILL"),
        ("integer", None, "INT"),
        ("tinyint(1)", None, "TINYINT(1)"),
        ("tinyint(4)", None, "TINYINT"),
        ("tinyint unsigned", None, "TINYINT UNSIGNED"),
        ("smallint", None, "SMALLINT"),
        ("mediumint", None, "MEDIUMINT"),
        ("double", None, "DOUBLE"),
        ("float", None, "FLOAT"),
        ("decimal(26,8)", None, "DECIMAL(26,8)"),
        ("decimal", None, "DECIMAL"),
        ("varchar(200)", None, "VARCHAR(200)"),
        ("varchar", 64, "VARCHAR(64)"),
        ("char(2)", None, "CHAR(2)"),
        ("text", None, "TEXT"),
        ("longtext", None, "LONGTEXT"),
        ("mediumtext", None, "MEDIUMTEXT"),
        ("tinytext", None, "TINYTEXT"),
        ("longblob", None, "LONGBLOB"),
        ("varbinary(16)", None, "VARBINARY(16)"),
        ("datetime", None, "DATETIME"),
        ("datetime(6)", None, "DATETIME(6)"),
        ("timestamp", None, "TIMESTAMP"),
        ("year", None, "YEAR"),
        ("json", None, "JSON"),
        ("bit(1)", None, "BIT(1)"),
        ("point", None, "POINT"),
        ("enum('draft','publish')", None, "ENUM('draft','publish')"),
        ("set('a','b''c')", None, "SET('a','b''c')"),
    ],
)
def test_mysql_carry_over(raw, max_length, expected):
    assert _type(raw, "mysql", MY, max_length=max_length) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "varchar",  # no length known
        "enum('a'); DROP TABLE users; --')",
        "bigint); DROP TABLE users; --",
        "int, `evil` text",
    ],
)
def test_mysql_unmappable_or_hostile_types_have_no_source_mapping(raw):
    assert resolve_source_type(raw, source_family="mysql", target=MY) is None


# --- Cross engine -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,max_length,expected",
    [
        ("tinyint(1)", None, "SMALLINT"),
        ("tinyint unsigned", None, "SMALLINT"),
        ("smallint unsigned", None, "INTEGER"),
        ("mediumint", None, "INTEGER"),
        ("int", None, "INTEGER"),
        ("int unsigned", None, "BIGINT"),
        ("bigint unsigned", None, "NUMERIC(20,0)"),  # not a key: full unsigned range
        ("decimal(19,4)", None, "NUMERIC(19,4)"),
        ("decimal", None, "NUMERIC(10,0)"),
        ("double", None, "DOUBLE PRECISION"),
        ("float", None, "REAL"),
        ("varchar(191)", None, "VARCHAR(191)"),
        ("char(32)", None, "CHAR(32)"),
        ("longtext", None, "TEXT"),
        ("mediumblob", None, "BYTEA"),
        ("datetime", None, "TIMESTAMP"),
        ("datetime(3)", None, "TIMESTAMP(3)"),
        ("timestamp", None, "TIMESTAMPTZ"),
        ("time", None, "TIME"),
        ("year", None, "SMALLINT"),
        ("json", None, "JSONB"),
        ("enum('draft','publish')", None, "VARCHAR(7)"),
        ("set('a','b')", None, "TEXT"),
        ("bit(1)", None, "BIT(1)"),
    ],
)
def test_mysql_to_pg(raw, max_length, expected):
    assert _type(raw, "mysql", PG, max_length=max_length) == expected


@pytest.mark.parametrize(
    "raw,max_length,expected",
    [
        ("integer", None, "INT"),
        ("bigint", None, "BIGINT"),
        ("smallint", None, "SMALLINT"),
        ("double precision", None, "DOUBLE"),
        ("real", None, "FLOAT"),
        ("numeric(10,2)", None, "DECIMAL(10,2)"),
        ("money", None, "DECIMAL(19,2)"),
        ("boolean", None, "BOOLEAN"),
        ("character varying", 255, "VARCHAR(255)"),
        ("character varying", None, "LONGTEXT"),
        ("character varying", 100000, "LONGTEXT"),
        ("character", 2, "CHAR(2)"),
        ("text", None, "LONGTEXT"),
        ("bytea", None, "LONGBLOB"),
        ("timestamp without time zone", None, "DATETIME(6)"),
        ("timestamp(3) with time zone", None, "DATETIME(3)"),
        ("time without time zone", None, "TIME(6)"),
        ("date", None, "DATE"),
        ("jsonb", None, "JSON"),
        ("json", None, "JSON"),
        ("_int4", None, "JSON"),
        ("integer[]", None, "JSON"),
        ("uuid", None, "CHAR(36)"),
        ("inet", None, "VARCHAR(43)"),
        ("macaddr", None, "VARCHAR(17)"),
        ("xml", None, "LONGTEXT"),
    ],
)
def test_pg_to_mysql(raw, max_length, expected):
    assert _type(raw, "postgresql", MY, max_length=max_length) == expected


@pytest.mark.parametrize(
    "raw,max_length,fallback",
    [
        ("character varying", None, "VARCHAR(255)"),  # unbounded and indexed
        ("text", None, "VARCHAR(255)"),
        ("character varying", 1000, "VARCHAR(768)"),  # over the index key length
    ],
)
def test_pg_to_mysql_indexed_unbounded_text_is_a_residual(raw, max_length, fallback):
    res = resolve_source_type(
        raw, source_family="postgresql", target=MY, max_length=max_length, indexed=True
    )
    assert res is not None and res.needs_judgment
    assert res.aurora_type == fallback
    assert res.reason


def test_pg_bare_numeric_to_mysql_is_a_residual():
    res = resolve_source_type("numeric", source_family="postgresql", target=MY)
    assert res is not None and res.needs_judgment and res.aurora_type == "DECIMAL(38,10)"


@pytest.mark.parametrize("raw", ["tsvector", "interval", "int4range", "point", "halfvec"])
def test_pg_types_without_a_mysql_equivalent_have_no_mapping(raw):
    assert resolve_source_type(raw, source_family="postgresql", target=MY) is None


def test_other_source_families_use_the_normalized_type():
    assert resolve_source_type("NUMBER(10)", source_family="other", target=PG) is None
    assert resolve_source_type(None, source_family="postgresql", target=PG) is None


def test_mysql_bigint_unsigned_key_stays_bigint_on_pg():
    # identity columns and foreign keys need an integer type
    assert _type("bigint unsigned", "mysql", PG, indexed=True) == "BIGINT"


@pytest.mark.parametrize(
    "raw,max_length,expected",
    [
        ("_bpchar", None, "TEXT[]"),  # no element length: CHAR[] would be char(1)[]
        ("_bit", None, "VARBIT[]"),
        ("_varchar", 50, "VARCHAR[]"),  # max_length is not the element length
        ("character(3)[]", None, "CHAR(3)[]"),
    ],
)
def test_pg_arrays_never_truncate_elements(raw, max_length, expected):
    assert _type(raw, "postgresql", PG, max_length=max_length) == expected


@pytest.mark.parametrize("raw", ["jsonb", "json", "hstore", "_int4", "geometry", "text"])
def test_pg_to_mysql_unindexable_types_in_a_key_are_residuals(raw):
    res = resolve_source_type(raw, source_family="postgresql", target=MY, indexed=True)
    assert res is not None and res.needs_judgment
    validate_aurora_type(res.aurora_type, MY)


@pytest.mark.parametrize(
    "raw,target_type",
    [("jsonb", "JSON"), ("text", "LONGTEXT"), ("bytea", "LONGBLOB"), ("geography", "GEOMETRY")],
)
def test_pg_to_mysql_defaults_on_text_blob_json_geometry_are_residuals(raw, target_type):
    res = resolve_source_type(raw, source_family="postgresql", target=MY, has_default=True)
    assert res is not None and res.needs_judgment
    assert res.aurora_type == target_type and "DEFAULT" in res.reason


@pytest.mark.parametrize("raw", ["timestamp with time zone", "time with time zone"])
def test_pg_time_zone_types_on_mysql_carry_a_utc_note(raw):
    res = resolve_source_type(raw, source_family="postgresql", target=MY)
    assert res is not None and not res.needs_judgment and "UTC" in res.note
    assert resolve_source_type("timestamp", source_family="postgresql", target=MY).note == ""
