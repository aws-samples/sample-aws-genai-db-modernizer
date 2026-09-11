import pytest

from src.contracts.schema_design_input import NormalizedDataType as N
from src.tools.schema.aurora_common.type_map import resolve_pg_type


@pytest.mark.parametrize(
    "normalized,max_length,expected_type",
    [
        (N.integer, None, "BIGINT"),
        (N.decimal, None, "NUMERIC"),
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


def test_string_without_length_needs_judgment():
    res = resolve_pg_type(N.string, max_length=None)
    assert res.needs_judgment is True
    assert res.aurora_type == "TEXT"  # safe fallback, but flagged
    assert "length" in res.reason.lower()


def test_missing_normalized_type_needs_judgment():
    res = resolve_pg_type(None, max_length=None)
    assert res.needs_judgment is True
    assert "not normalized" in res.reason.lower()
