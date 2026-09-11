import pytest

from src.contracts.schema_design_input import NormalizedDataType as N
from src.tools.schema.aurora_common.type_map import resolve_mysql_type


@pytest.mark.parametrize(
    "normalized,max_length,expected",
    [
        (N.integer, None, "BIGINT"),
        (N.boolean, None, "BOOLEAN"),
        (N.date, None, "DATE"),
        (N.datetime, None, "DATETIME"),
        (N.timestamp, None, "TIMESTAMP"),
        (N.binary, None, "BLOB"),
        (N.blob, None, "LONGBLOB"),
        (N.json, None, "JSON"),
        (N.xml, None, "LONGTEXT"),
        (N.uuid, None, "CHAR(36)"),
        (N.text, None, "TEXT"),
        (N.string, 255, "VARCHAR(255)"),
    ],
)
def test_resolve_mysql_type_deterministic(normalized, max_length, expected):
    res = resolve_mysql_type(normalized, max_length=max_length)
    assert res.aurora_type == expected
    assert res.needs_judgment is False


def test_mysql_decimal_needs_judgment():
    res = resolve_mysql_type(N.decimal, max_length=None)
    assert res.aurora_type == "DECIMAL(38,10)"
    assert res.needs_judgment is True
    assert "precision" in res.reason.lower()


def test_mysql_string_without_length_needs_judgment():
    res = resolve_mysql_type(N.string, max_length=None)
    assert res.needs_judgment is True
    assert res.aurora_type == "TEXT"


def test_mysql_missing_type_needs_judgment():
    res = resolve_mysql_type(None, max_length=None)
    assert res.needs_judgment is True
    assert "not normalized" in res.reason.lower()
