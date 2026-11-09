# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cast check's gaps from #1763, closed (#1830): MySQL unsigned decimals and floats,
character and byte lengths, ``FLOAT(M,D)``, and PostgreSQL booleans into Aurora MySQL."""

from __future__ import annotations

import dataclasses
import struct

import pytest

from src.tools.synthetic_data.generator import SyntheticTable, generate_rows
from src.tools.transformation_contract.duckdb_backend import (
    UnknownTargetType,
    duckdb_type,
    run_rule,
)
from src.tools.transformation_contract.plan_ir import OutputColumn, RulePlan

MYSQL, PG = "aurora_mysql", "aurora_postgresql"


def _run(target_type, values, engine=MYSQL, *, copy=False):
    """One column ``c`` cast to ``target_type`` (a transform, or a copy) over ``values``."""
    column = OutputColumn("c", "c", None if copy else target_type, target_type)
    table = SyntheticTable("s.t", [{"c": v} for v in values], 0, "collector")
    return run_rule(RulePlan("s.t", "t", (column,), ()), table, source_columns=["c"],
                    engine=engine)  # fmt: skip


def _out(run):
    return [r["c"] for r in run.rows]


def _failed(run):
    return [f.value for f in run.cast_failures]


def _f32(x: float) -> float:
    return float(struct.unpack("<f", struct.pack("<f", x))[0])


# --------------------------------------------------------------- unsigned decimals and floats


def test_an_unsigned_decimal_rejects_negatives_and_keeps_zero():
    run = _run("decimal(5,2) unsigned", ["0", "-0", "0.00", "-0.01", "999.99", "1000.00", None])
    assert run.unchecked_columns == []
    assert [None if v is None else str(v) for v in _out(run)] == [
        "0.00", "0.00", "0.00", None, "999.99", None, None,
    ]  # fmt: skip
    assert _failed(run) == ["-0.01", "1000.00"]


def test_an_unsigned_decimal_copy_is_checked_too():
    run = _run("DECIMAL(10,2) UNSIGNED", ["-5", "5"], copy=True)
    assert run.unchecked_columns == [] and _failed(run) == ["-5"]


def test_an_unsigned_decimal_at_max_precision():
    run = _run("decimal(38,0) unsigned", ["9" * 38, "9" * 39, "-1", "0"])
    assert _failed(run) == ["9" * 39, "-1"]
    assert str(_out(run)[0]) == "9" * 38


@pytest.mark.passes_on_main  # a guard on what must not change
def test_an_unsigned_decimal_above_duckdb_precision_stays_unchecked():
    # MySQL allows DECIMAL(65,30); DuckDB stops at 38 digits.
    assert _run("decimal(65,30) unsigned", ["-1"]).unchecked_columns == ["c"]


def test_zerofill_implies_unsigned():
    assert _failed(_run("decimal(10,2) unsigned zerofill", ["-1", "1"])) == ["-1"]
    assert _failed(_run("int zerofill", ["-1", "1"])) == ["-1"]


@pytest.mark.parametrize("t", ["float unsigned", "double unsigned", "real unsigned",
                               "float(7,2) unsigned", "double(10,3) unsigned"])  # fmt: skip
def test_unsigned_floats_reject_negatives(t):
    run = _run(t, ["-1.5", "0", "-0", "1.5", None])
    assert run.unchecked_columns == []
    assert _failed(run) == ["-1.5"]


@pytest.mark.passes_on_main  # a guard on what must not change
def test_unsigned_stays_mysql_only():
    with pytest.raises(UnknownTargetType):
        duckdb_type("numeric(5,2) unsigned", PG)


def test_an_empty_table_reports_nothing_unchecked():
    run = _run("decimal(5,2) unsigned", [])
    assert run.rows == [] and run.cast_failures == [] and run.unchecked_columns == []


# --------------------------------------------------------------- character lengths


@pytest.mark.parametrize("engine,t", [(MYSQL, "varchar(3)"), (PG, "character varying(3)"),
                                      (PG, "varchar(3)"), (MYSQL, "nvarchar(3)")])  # fmt: skip
def test_varchar_length_is_in_characters(engine, t):
    four_byte = "\U0001d11e"
    run = _run(t, ["abc", "abcd", four_byte * 3, four_byte * 4, "", None], engine)
    assert run.unchecked_columns == []
    assert _failed(run) == ["abcd", four_byte * 4]
    assert _out(run) == ["abc", None, four_byte * 3, None, "", None]


@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_excess_trailing_spaces_are_cut_not_rejected(engine):
    run = _run("varchar(3)", ["ab   ", "abc  ", "a b c", "ab"], engine)
    assert _failed(run) == ["a b c"]
    assert _out(run) == ["ab ", "abc", None, "ab"]


def test_a_postgresql_char_is_padded_to_its_length():
    run = _run("character(4)", ["ab", "abcd  ", "abcde", "", None], PG)
    assert _failed(run) == ["abcde"]
    assert _out(run) == ["ab  ", "abcd", None, "    ", None]


def test_a_mysql_char_comes_back_without_trailing_spaces():
    run = _run("char(4)", ["ab  ", "ab", " a", "abcd  ", "abcde"], MYSQL)
    assert _failed(run) == ["abcde"]
    assert _out(run) == ["ab", "ab", " a", "abcd", None]


@pytest.mark.parametrize("engine", [MYSQL, PG])
def test_a_bare_char_holds_one_character(engine):
    run = _run("char", ["a", "a ", "ab"], engine)
    assert _failed(run) == ["ab"]


@pytest.mark.passes_on_main  # a guard on what must not change
def test_text_types_have_no_length_check():
    assert _failed(_run("text", ["x" * 100_000])) == []
    assert _failed(_run("varchar", ["x" * 100_000], PG)) == []


# --------------------------------------------------------------- byte lengths


def test_binary_length_is_in_bytes_and_padded_with_zero_bytes():
    e_acute = "é"  # two bytes in UTF-8
    run = _run("binary(4)", ["ab", "abcd", "abcde", e_acute * 2, e_acute * 3, None])
    assert run.unchecked_columns == []
    assert _failed(run) == ["abcde", e_acute * 3]
    assert _out(run) == [b"ab\x00\x00", b"abcd", None, "éé".encode(), None, None]


def test_varbinary_length_is_in_bytes_and_not_padded():
    e_acute = "é"
    run = _run("varbinary(4)", ["ab", e_acute * 2, e_acute * 3])
    assert _failed(run) == [e_acute * 3]
    assert _out(run) == [b"ab", "éé".encode(), None]


def test_a_bare_binary_holds_one_byte():
    assert _failed(_run("binary", ["a", "ab"])) == ["ab"]


# --------------------------------------------------------------- FLOAT(M,D)


def test_mysql_float_m_d_is_single_precision_whatever_m():
    assert duckdb_type("float(7,2)", MYSQL) == "FLOAT"
    assert duckdb_type("float(30,2)", MYSQL) == "FLOAT"
    assert duckdb_type("double(10,3)", MYSQL) == "DOUBLE"
    assert duckdb_type("float(25)", MYSQL) == "DOUBLE"  # FLOAT(p): p above 24 is DOUBLE


def test_float_m_d_rounds_to_d_places_and_rejects_overflow_at_the_boundary():
    run = _run("float(7,2)", ["12.346", "99999.994", "99999.996", "-99999.996", "100000",
                              "0.004", None])  # fmt: skip
    assert run.unchecked_columns == []
    assert _failed(run) == ["99999.996", "-99999.996", "100000"]
    assert _out(run) == [_f32(12.35), _f32(99999.99), None, None, None, 0.0, None]


def test_float_m_d_with_m_above_24_is_still_single_precision():
    run = _run("float(30,2)", ["12.346"])
    assert _out(run) == [_f32(12.35)]


def test_double_m_d_rounds_to_d_places():
    run = _run("double(6,3)", ["1.23456", "999.9994", "999.9996"])
    assert _failed(run) == ["999.9996"]
    assert _out(run)[:2] == [1 + 0.235, 999 + 0.999]  # floor + fraction, in double


@pytest.mark.parametrize("t", ["float(7,2)", "double precision(7,2)"])
def test_postgresql_has_no_float_m_d(t):
    with pytest.raises(UnknownTargetType):
        duckdb_type(t, PG)


@pytest.mark.parametrize("t", ["float(5,7)", "float(0,0)", "double(256,2)"])
def test_impossible_float_m_d_is_unchecked(t):
    with pytest.raises(UnknownTargetType):
        duckdb_type(t, MYSQL)


# --------------------------------------------------------------- booleans into Aurora MySQL


@pytest.mark.parametrize("target", ["tinyint(1)", "boolean", "tinyint"])
def test_a_postgresql_boolean_reaches_mysql_as_zero_or_one(target):
    table = {"table_id": "public.flags", "row_count": 6, "primary_key": [],
             "columns": [{"column_name": "ok", "data_type": "boolean", "nullable": True}]}  # fmt: skip
    synthetic = generate_rows(table, dialect="postgresql", seed=1)
    assert {r["ok"] for r in synthetic.rows} >= {"true", "false"}
    plan = RulePlan("public.flags", "flags", (OutputColumn("ok", "ok", target, target),), ())
    run = run_rule(plan, synthetic, source_columns=["ok"], engine=MYSQL)
    assert run.cast_failures == []
    want = {"true": 1, "false": 0, None: None}
    assert [r["ok"] for r in run.rows] == [want[r["ok"]] for r in synthetic.rows]


@pytest.mark.passes_on_main  # a guard on what must not change
def test_a_postgresql_boolean_into_postgresql_is_unchanged():
    table = {"table_id": "public.flags", "row_count": 4, "primary_key": [],
             "columns": [{"column_name": "ok", "data_type": "boolean", "nullable": False}]}  # fmt: skip
    synthetic = generate_rows(table, dialect="postgresql", seed=1)
    plan = RulePlan("public.flags", "flags", (OutputColumn("ok", "ok", None, "boolean"),), ())
    run = run_rule(plan, synthetic, source_columns=["ok"], engine=PG)
    assert [r["ok"] for r in run.rows] == [r["ok"] == "true" for r in synthetic.rows]


@pytest.mark.passes_on_main
def test_text_true_is_not_a_boolean_and_still_fails_into_mysql_tinyint():
    # Only a boolean source column is fed as 0/1; a text 'true' still fails.
    assert _failed(_run("tinyint(1)", ["true", "1"])) == ["true"]


# --------------------------------------------------------------- review round (#2058)


@pytest.mark.parametrize("t,value", [("decimal(6,2) unsigned", "-0.001"),
                                     ("decimal(6,2) unsigned", "-0.004"),
                                     ("float(7,2) unsigned", "-0.001"),
                                     ("float unsigned", "-1e-50"),
                                     ("double unsigned", "-1e-300")])  # fmt: skip
def test_unsigned_tests_the_sign_before_rounding(t, value):
    # MySQL rejects a negative value even when it rounds to zero.
    assert _failed(_run(t, [value, "0", "-0"])) == [value]


def test_float_m_d_rounds_the_fraction_half_to_even_like_mysql():
    # MySQL: floor(x) + rint(frac * 10^D) / 10^D. -0.005 -> -1 + rint(99.5)/100 = 0.00.
    run = _run("float(7,2)", ["-0.005", "0.125", "0.375", "2.5"])
    assert _failed(run) == []
    assert _out(run) == [0.0, _f32(0.12), _f32(0.38), 2.5]


def _bytea_table(values):
    """A PostgreSQL ``bytea`` column's rows, as the generator names its hex text."""
    table = {"table_id": "s.t", "row_count": 1, "primary_key": [],
             "columns": [{"column_name": "c", "data_type": "bytea", "nullable": True}]}  # fmt: skip
    synthetic = generate_rows(table, dialect="postgresql", seed=1)
    return dataclasses.replace(synthetic, rows=[{"c": v} for v in values])


@pytest.mark.parametrize("target,want", [("varbinary(1)", [b"\x0a", None, None]),
                                         ("blob", [b"\x0a", b"\x0a\x0b", None]),
                                         ("binary(2)", [b"\x0a\x00", b"\x0a\x0b", None])])  # fmt: skip
def test_a_postgresql_bytea_into_mysql_is_decoded_from_its_hex_text(target, want):
    plan = RulePlan("s.t", "t", (OutputColumn("c", "c", target, target),), ())
    run = run_rule(plan, _bytea_table(["\\x0a", "\\x0a0b", None]), source_columns=["c"],
                   engine=MYSQL)  # fmt: skip
    assert _out(run) == want
    assert _failed(run) == (["\\x0a0b"] if target == "varbinary(1)" else [])


def test_a_postgresql_bytea_into_bytea_keeps_every_byte():
    plan = RulePlan("s.t", "t", (OutputColumn("c", "c", None, "bytea"),), ())
    run = run_rule(plan, _bytea_table(["\\x0a0b", "\\xzz"]), source_columns=["c"], engine=PG)
    assert _out(run) == [b"\x0a\x0b", None]
    assert _failed(run) == ["\\xzz"]


def test_float_m_d_above_single_precision_fails_the_row_not_the_run():
    # FLOAT(60,0) allows 60 digits, but a single-precision float stops near 3.4e38.
    run = _run("float(60,0)", ["1e50", "12"])
    assert _failed(run) == ["1e50"]
    assert _out(run) == [None, 12.0]
