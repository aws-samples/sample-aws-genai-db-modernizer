# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest

from src.tools.synthetic_data.generator import SyntheticTable
from src.tools.transformation_contract.duckdb_backend import (
    UnknownTargetType,
    duckdb_type,
    quote_ident,
    run_rule,
    to_sql,
)
from src.tools.transformation_contract.plan_ir import OutputColumn, RulePlan


def test_types_map_and_unknown_types_raise():
    assert duckdb_type("bigint unsigned", "aurora_mysql") == "UBIGINT"
    assert duckdb_type("INT(11)", "aurora_mysql") == "INTEGER"
    assert duckdb_type("decimal(5,2)", "aurora_postgresql") == "DECIMAL(5,2)"
    assert duckdb_type("TINYINT(1)", "aurora_mysql") == "TINYINT"
    assert duckdb_type("BOOLEAN", "aurora_mysql") == "TINYINT"
    assert duckdb_type("BOOLEAN", "aurora_postgresql") == "BOOLEAN"
    with pytest.raises(UnknownTargetType):
        duckdb_type("GEOMETRY", "aurora_mysql")


def test_more_types_and_edges():
    assert duckdb_type("VARCHAR(255)", "aurora_mysql") == "VARCHAR"
    assert duckdb_type("character varying(10)", "aurora_postgresql") == "VARCHAR"
    assert duckdb_type("ENUM('a','b')", "aurora_mysql") == "VARCHAR"
    assert duckdb_type("mediumint unsigned", "aurora_mysql") == "UINTEGER"
    assert duckdb_type("DOUBLE PRECISION", "aurora_postgresql") == "DOUBLE"
    assert duckdb_type("timestamp with time zone", "aurora_postgresql") == "TIMESTAMPTZ"
    assert duckdb_type("VARBINARY(16)", "aurora_mysql") == "BLOB"
    assert duckdb_type("tinyint", "aurora_mysql") == "TINYINT"
    with pytest.raises(UnknownTargetType):
        duckdb_type("decimal(39,2)", "aurora_mysql")
    with pytest.raises(UnknownTargetType):
        duckdb_type("textual", "aurora_mysql")
    with pytest.raises(ValueError, match="unknown target engine") as info:
        duckdb_type("INT", "oracle")
    assert not isinstance(info.value, UnknownTargetType)


def test_a_mysql_boolean_accepts_two():
    plan = RulePlan("s.t", "t", (OutputColumn("f", "f", "BOOLEAN", "BOOLEAN"),), ())
    table = SyntheticTable("s.t", [{"f": "2"}], 1, "collector")
    assert run_rule(plan, table, source_columns=["f"], engine="aurora_mysql").cast_failures == []


def test_a_postgres_boolean_rejects_two():
    plan = RulePlan("s.t", "t", (OutputColumn("f", "f", "BOOLEAN", "BOOLEAN"),), ())
    table = SyntheticTable("s.t", [{"f": "2"}], 1, "collector")
    run = run_rule(plan, table, source_columns=["f"], engine="aurora_postgresql")
    assert [f.value for f in run.cast_failures] == ["2"]


def test_hostile_identifiers_are_quoted():
    assert quote_ident('we"ird') == '"we""ird"'
    plan = RulePlan(
        "s.t",
        "t",
        (
            OutputColumn("order", "order", None, "TEXT"),
            OutputColumn("my col", 'we"ird', None, "TEXT"),
        ),
        (),
    )
    table = SyntheticTable("s.t", [{"order": "1", 'we"ird': "x"}], 0, "collector")
    run = run_rule(plan, table, source_columns=["order", 'we"ird'], engine="aurora_mysql")
    assert run.rows == [{"order": "1", "my col": "x"}]
    assert '"we""ird"' in to_sql(plan, "src", "aurora_mysql")


def test_a_failed_cast_is_reported_per_value():
    plan = RulePlan("s.t", "t", (OutputColumn("d", "d", "DATE", "DATE"),), ())
    table = SyntheticTable(
        "s.t", [{"d": "0000-00-00"}, {"d": "2024-01-02"}, {"d": None}], 1, "collector"
    )
    run = run_rule(plan, table, source_columns=["d"], engine="aurora_mysql")
    assert [f.value for f in run.cast_failures] == ["0000-00-00"]
    assert run.cast_failures[0].target_column == "d"
    assert run.cast_failures[0].to_type == "DATE"
    assert run.rows[2]["d"] is None


def test_values_are_bound_not_spliced():
    plan = RulePlan("s.t", "t", (OutputColumn("c", "c", None, "TEXT"),), ())
    table = SyntheticTable("s.t", [{"c": "x'); DROP TABLE t; --"}], 0, "collector")
    assert run_rule(plan, table, source_columns=["c"], engine="aurora_mysql").rows == [
        {"c": "x'); DROP TABLE t; --"}
    ]


def test_empty_table_runs():
    plan = RulePlan("s.t", "t", (OutputColumn("c", "c", "INT", "INT"),), ())
    table = SyntheticTable("s.t", [], 0, "default")
    run = run_rule(plan, table, source_columns=["c"], engine="aurora_mysql")
    assert run.rows == [] and run.cast_failures == []


@pytest.mark.parametrize("t", ["DECIMAL(5,9)", "DECIMAL(0,0)", "numeric(0,0)"])
def test_impossible_decimals_are_unknown(t):
    with pytest.raises(UnknownTargetType):
        duckdb_type(t, "aurora_mysql")


def test_valid_decimal_bounds():
    assert duckdb_type("DECIMAL(5,5)", "aurora_mysql") == "DECIMAL(5,5)"
    assert duckdb_type("DECIMAL(38,0)", "aurora_postgresql") == "DECIMAL(38,0)"


def test_missing_or_unlisted_source_column_raises_clearly():
    table = SyntheticTable("s.t", [{"a": "1"}], 0, "collector")
    no_src = RulePlan("s.t", "t", (OutputColumn("x", None, None, "TEXT"),), ())
    with pytest.raises(ValueError, match="'x'"):
        run_rule(no_src, table, source_columns=["a"], engine="aurora_mysql")
    with pytest.raises(ValueError, match="'x'"):
        to_sql(no_src, "src", "aurora_mysql")
    unlisted = RulePlan("s.t", "t", (OutputColumn("x", "zzz", None, "TEXT"),), ())
    with pytest.raises(ValueError, match="'zzz'"):
        run_rule(unlisted, table, source_columns=["a"], engine="aurora_mysql")


def _plan(*cols):
    return RulePlan("s.t", "t", tuple(cols), ())


def test_a_copy_is_cast_to_its_target_type():
    plan = _plan(OutputColumn("d", "d", None, "DATE"))
    table = SyntheticTable("s.t", [{"d": "0000-00-00"}, {"d": "2024-01-02"}], 1, "collector")
    run = run_rule(plan, table, source_columns=["d"], engine="aurora_mysql")
    assert [(f.target_column, f.value, f.to_type) for f in run.cast_failures] == [
        ("d", "0000-00-00", "DATE")
    ]
    assert run.unchecked_columns == []


def test_mysql_decimal_forms():
    assert duckdb_type("DECIMAL", "aurora_mysql") == "DECIMAL(10,0)"
    assert duckdb_type("decimal(7)", "aurora_mysql") == "DECIMAL(7,0)"
    assert duckdb_type("NUMERIC(7)", "aurora_postgresql") == "DECIMAL(7,0)"


def test_more_exact_mappings():
    assert duckdb_type("timetz", "aurora_postgresql") == "TIMETZ"
    assert duckdb_type("time with time zone", "aurora_postgresql") == "TIMETZ"
    assert duckdb_type("interval", "aurora_postgresql") == "INTERVAL"
    assert duckdb_type("INTERVAL DAY TO SECOND", "aurora_postgresql") == "INTERVAL"


@pytest.mark.parametrize("t", ["numeric", "NUMERIC", "money", "bit(3)", "xml", "inet", "year",
                               "geometry", "tsvector"])  # fmt: skip
def test_types_without_a_faithful_mapping_raise(t):
    with pytest.raises(UnknownTargetType):
        duckdb_type(t, "aurora_postgresql")


@pytest.mark.parametrize("t", ["tinyint(1)", "int unsigned", "bigint unsigned", "mediumint",
                               "datetime", "year", "tinyint", "datetime(3)", "longtext",
                               "tinyint unsigned"])  # fmt: skip
def test_mysql_only_spellings_are_unknown_on_postgresql(t):
    with pytest.raises(UnknownTargetType):
        duckdb_type(t, "aurora_postgresql")


def test_an_unmapped_type_is_unchecked_and_the_rest_still_runs():
    plan = _plan(
        OutputColumn("n", "n", "NUMERIC", "NUMERIC"),
        OutputColumn("i", "i", "INTEGER", "INTEGER"),
        OutputColumn("g", "g", None, "GEOMETRY"),
    )
    table = SyntheticTable("s.t", [{"n": "1.5", "i": "x", "g": "POINT(1 1)"}], 0, "collector")
    run = run_rule(plan, table, source_columns=["n", "i", "g"], engine="aurora_postgresql")
    assert run.unchecked_columns == ["n", "g"]
    assert run.rows == [{"n": "1.5", "i": None, "g": "POINT(1 1)"}]
    assert [f.target_column for f in run.cast_failures] == ["i"]
    assert "NUMERIC" not in to_sql(plan, "src", "aurora_postgresql")


def test_columns_the_generator_could_not_model_are_unchecked():
    plan = _plan(OutputColumn("a", "a", "INTEGER", "INTEGER"), OutputColumn("b", "b", None, "DATE"))
    table = SyntheticTable("s.t", [{"a": "v0", "b": "v0"}], 0, "collector",
                           unchecked_source_columns=("a",))  # fmt: skip
    run = run_rule(plan, table, source_columns=["a", "b"], engine="aurora_mysql")
    assert run.unchecked_columns == ["a"]
    assert [f.target_column for f in run.cast_failures] == ["b"]
    assert run.rows == [{"a": "v0", "b": None}]


def test_two_plan_columns_may_not_share_a_target_column():
    plan = _plan(OutputColumn("x", "a", None, "TEXT"), OutputColumn("x", "b", None, "TEXT"))
    with pytest.raises(ValueError, match="'x'"):
        to_sql(plan, "src", "aurora_mysql")


def test_run_rule_checks_the_table_id():
    plan = _plan(OutputColumn("a", "a", None, "TEXT"))
    table = SyntheticTable("other.t", [{"a": "1"}], 0, "collector")
    with pytest.raises(ValueError, match="other.t"):
        run_rule(plan, table, source_columns=["a"], engine="aurora_mysql")


def test_decimal_inputs_become_plain_text():
    from decimal import Decimal

    plan = _plan(OutputColumn("m", "m", None, "TEXT"))
    table = SyntheticTable("s.t", [{"m": Decimal("2E-7")}, {"m": True}], 0, "collector")
    run = run_rule(plan, table, source_columns=["m"], engine="aurora_mysql")
    assert [r["m"] for r in run.rows] == ["0.0000002", "true"]


def test_every_type_ddl_generation_emits_is_mapped_or_deliberately_unchecked():
    from src.contracts.aurora_design_delta import ENGINE_TYPE_RULES, validate_aurora_type

    unchecked: dict[str, set[str]] = {e: set() for e in ENGINE_TYPE_RULES}
    for engine, (bases, _suffixes, _arrays) in ENGINE_TYPE_RULES.items():
        for base in sorted(bases):
            forms = [f"{base}{size}" for size in ("", "(10)", "(10,2)")]
            forms = [f for f in forms if _allowed(validate_aurora_type, f, engine)]
            results = [_mapped(f, engine) for f in forms]
            assert forms, (engine, base)
            if not any(results):
                unchecked[engine].add(base)
    # Deliberately unchecked: no DuckDB type accepts and rejects the same values.
    assert unchecked["aurora_postgresql"] == {
        "money", "xml", "inet", "cidr", "macaddr", "macaddr8", "tsvector", "tsquery", "point",
        "line", "lseg", "box", "path", "polygon", "circle", "bit", "varbit", "bit varying",
        "int4range", "int8range", "numrange", "tsrange", "tstzrange", "daterange", "oid",
        "hstore", "geometry", "geography", "vector", "halfvec", "sparsevec",
    }  # fmt: skip
    assert unchecked["aurora_mysql"] == {
        "bit", "year", "geometry", "point", "linestring", "polygon", "multipoint",
        "multilinestring", "multipolygon", "geometrycollection",
    }  # fmt: skip
    # The suffixes DDL generation emits.
    assert _mapped("timestamp with time zone", "aurora_postgresql")
    assert _mapped("time without time zone", "aurora_postgresql")
    assert _mapped("bigint unsigned", "aurora_mysql")
    assert _mapped("decimal(10,2) unsigned zerofill", "aurora_mysql") is False
    # Bare PostgreSQL numeric is arbitrary precision: unchecked. MySQL's bare DECIMAL is (10,0).
    assert not _mapped("numeric", "aurora_postgresql") and _mapped("decimal", "aurora_mysql")


def _allowed(validate, name, engine):
    try:
        validate(name, engine)
    except ValueError:
        return False
    return True


def _mapped(name, engine):
    try:
        return bool(duckdb_type(name, engine))
    except UnknownTargetType:
        return False
