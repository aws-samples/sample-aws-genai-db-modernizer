# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Byte-string and enum columns are modelled, so they are no longer unchecked (#1830)."""

from __future__ import annotations

import pytest

from src.tools.synthetic_data.generator import column_kind, edge_values, generate_rows


def _col(name, data_type, normalized=None, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "normalized_data_type": normalized,
            "nullable": nullable, **kw}  # fmt: skip


def _table(columns, pk=(), rows=8):
    return {"table_id": "s.t", "row_count": rows, "primary_key": list(pk), "columns": columns}


@pytest.mark.parametrize("data_type,normalized", [("blob", "blob"), ("longblob", "blob"),
                                                  ("tinyblob", None), ("varbinary(8)", "binary"),
                                                  ("binary(4)", "binary")])  # fmt: skip
def test_mysql_byte_strings_are_modelled(data_type, normalized):
    col = _col("b", data_type, normalized, max_length=4 if "binary(" in data_type else None)
    assert column_kind(col) == "bytes"
    t = generate_rows(_table([col]), dialect="mysql", seed=1)
    assert t.unchecked_source_columns == ()
    assert all(isinstance(r["b"], str) and r["b"] for r in t.rows)


def test_a_postgresql_bytea_is_modelled_in_its_hex_input_form():
    col = _col("b", "bytea", "blob", nullable=True)
    t = generate_rows(_table([col]), dialect="postgresql", seed=1)
    assert t.unchecked_source_columns == ()
    values = [r["b"] for r in t.rows]
    assert None in values
    assert all(v.startswith("\\x") and len(v) == 4 for v in values if v is not None)


def test_a_fixed_length_binary_gets_its_full_length_as_an_edge_value():
    assert edge_values(_col("b", "binary(4)", "binary", max_length=4), dialect="mysql") == ["zzzz"]
    assert edge_values(_col("b", "blob", "blob"), dialect="mysql") == []
    assert edge_values(_col("b", "bytea", "blob"), dialect="postgresql") == []


@pytest.mark.parametrize("normalized", [None, "other", "blob"])
def test_an_enum_with_values_is_modelled_from_its_own_values(normalized):
    col = _col("e", "enum('a','it''s','c')", normalized, nullable=True)
    assert column_kind(col) == "enum"
    t = generate_rows(_table([col], rows=12), dialect="mysql", seed=3)
    assert t.unchecked_source_columns == ()
    values = {r["e"] for r in t.rows}
    assert None in values
    assert values - {None} <= {"a", "it's", "c"}


def test_a_set_with_values_is_modelled_from_its_own_values():
    col = _col("s", "set('x','y')")
    t = generate_rows(_table([col]), dialect="mysql", seed=1)
    assert t.unchecked_source_columns == ()
    assert {r["s"] for r in t.rows} <= {"x", "y"}


@pytest.mark.passes_on_main
@pytest.mark.parametrize("data_type", ["enum", "enum()", "set"])
def test_an_enum_with_no_values_listed_stays_unchecked(data_type):
    col = _col("e", data_type)
    assert column_kind(col) == "other"
    assert generate_rows(_table([col]), dialect="mysql", seed=1).unchecked_source_columns == ("e",)


@pytest.mark.passes_on_main
def test_an_enum_normalized_as_string_keeps_its_string_values():
    # The rows #1942 recorded (generate_rows_golden.json) model it as a string.
    col = _col("e", "enum('a','b')", "string")
    assert column_kind(col) == "string"


def test_a_byte_or_enum_primary_key_is_unique():
    b = generate_rows(_table([_col("b", "varbinary(8)", "binary")], pk=["b"]), dialect="mysql")
    assert len({r["b"] for r in b.rows}) == len(b.rows)
    e = generate_rows(_table([_col("e", "enum('a','b','c')")], pk=["e"], rows=9), dialect="mysql")
    assert len({r["e"] for r in e.rows}) == len(e.rows) <= 3
    assert {r["e"] for r in e.rows} <= {"a", "b", "c"}


def test_a_mysql_binary_value_is_stored_at_its_full_length():
    # MySQL keeps binary(4) 'ab' as 'ab\0\0': every value is exactly 4 bytes.
    col = _col("b", "binary(4)", "binary", True, max_length=4)
    t = generate_rows(_table([col]), dialect="mysql", seed=1)
    values = [r["b"] for r in t.rows if r["b"] is not None]
    assert values and all(len(v.encode()) == 4 for v in values)
    assert "ab\x00\x00" in values
    bare = generate_rows(_table([_col("b", "binary", "binary")]), dialect="mysql", seed=1)
    assert all(len(r["b"].encode()) == 1 for r in bare.rows)


def test_a_mysql_varbinary_value_is_not_padded():
    col = _col("b", "varbinary(4)", "binary", max_length=4)
    t = generate_rows(_table([col]), dialect="mysql", seed=1)
    assert {r["b"] for r in t.rows} == {"zzzz", "ab"}
