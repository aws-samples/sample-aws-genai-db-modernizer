"""The Aurora design delta contract is small, versioned and strict (issue #273)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.aurora_design_delta import (
    DELTA_VERSION,
    AuroraDesignDeltaContract,
    IndexSpec,
    is_design_delta,
    validate_aurora_type,
)


def test_minimal_delta_is_just_the_version():
    delta = AuroraDesignDeltaContract.model_validate({"delta_version": "1.0"})
    assert delta.tables == [] and delta.type_rules == [] and delta.trade_offs == []
    assert DELTA_VERSION == "1.0"


@pytest.mark.parametrize("payload", [{}, {"delta_version": "2.0"}, {"delta_version": 1}])
def test_version_is_required_and_pinned(payload):
    with pytest.raises(ValidationError):
        AuroraDesignDeltaContract.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"delta_version": "1.0", "table_definitions": []},  # full-contract field
        {"delta_version": "1.0", "tables": [{"table_name": "t", "add_index": ["x"]}]},
        {"delta_version": "1.0", "type_rules": [{"source_data_type": "int"}]},
    ],
)
def test_unknown_or_incomplete_fields_are_rejected(payload):
    with pytest.raises(ValidationError):
        AuroraDesignDeltaContract.model_validate(payload)


def test_is_design_delta_tells_a_delta_from_a_full_contract():
    assert is_design_delta({"delta_version": "1.0"})
    assert not is_design_delta({"table_definitions": [], "generated_ddl": ""})
    assert not is_design_delta(["delta_version"])


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("BIGINT", "BIGINT"),
        (" varchar(255) ", "varchar(255)"),
        ("NUMERIC( 10 , 2 )", "NUMERIC( 10 , 2 )"),
        ("DOUBLE   PRECISION", "DOUBLE PRECISION"),
        ("TIMESTAMP(3) WITH TIME ZONE", "TIMESTAMP(3) WITH TIME ZONE"),
        ("INT UNSIGNED", "INT UNSIGNED"),
        ("TEXT[]", "TEXT[]"),
        ("ENUM('a','it''s')", "ENUM('a','it''s')"),
        ("set('x', 'y')", "set('x', 'y')"),
    ],
)
def test_type_grammar_accepts_plain_types(raw, normalized):
    assert validate_aurora_type(raw) == normalized


@pytest.mark.parametrize(
    "attack",
    [
        "BIGINT); DROP TABLE users; --",
        "INT, `evil` TEXT",
        "INT /* x */",
        "INT -- x",
        "VARCHAR(10)\n; DROP TABLE x",
        "INT NOT NULL",
        "INT DEFAULT 0",
        "INT PRIMARY KEY",
        "INT GENERATED ALWAYS AS (1) STORED",
        "ENUM('a\\'); DROP TABLE x; --')",
        "ENUM('a','b'), evil TEXT",
        "VARCHAR(-1)",
        "1INT",
        "",
        "   ",
        "A" * 65,
        "pg_sleep(10)",
        "lower(1)",
        "VARCHAR(10) CHARACTER SET utf8mb4",
        "evil_type",
        "ENUM(" + ",".join(["'v'"] * 101) + ")",
        "ENUM('" + "x" * 65 + "')",
    ],
)
def test_type_grammar_rejects_everything_else(attack):
    with pytest.raises(ValueError):
        validate_aurora_type(attack)


@pytest.mark.parametrize("name", ['a"; DROP TABLE x; --', "has space", "1abc", "", "x" * 64])
def test_index_names_are_plain_identifiers(name):
    with pytest.raises(ValidationError):
        IndexSpec(index_name=name, columns=["id"])


def test_index_entries_accept_structured_and_legacy_string_forms():
    delta = AuroraDesignDeltaContract.model_validate(
        {
            "delta_version": "1.0",
            "tables": [
                {
                    "table_name": "t",
                    "add_indexes": [
                        {"index_name": "i1", "columns": ["a"]},
                        "CREATE INDEX i2 ON t (a)",
                    ],
                }
            ],
        }
    )
    assert isinstance(delta.tables[0].add_indexes[0], IndexSpec)
    assert isinstance(delta.tables[0].add_indexes[1], str)


@pytest.mark.parametrize(
    ("value", "engine"),
    [
        ("INT UNSIGNED", "aurora_postgresql"),
        ("TIMESTAMP WITH TIME ZONE", "aurora_mysql"),
        ("TEXT[]", "aurora_mysql"),
        ("ENUM('a')", "aurora_postgresql"),
        ("TINYTEXT", "aurora_postgresql"),
        ("JSONB", "aurora_mysql"),
    ],
)
def test_type_grammar_is_per_engine(value, engine):
    validate_aurora_type(value)  # valid for one of the engines
    with pytest.raises(ValueError):
        validate_aurora_type(value, engine)


@pytest.mark.parametrize("value", ["DECIMAL(66,0)", "NUMERIC(38,35)"])
def test_mysql_decimal_limits_are_rejected_only_for_mysql(value):
    """Aurora MySQL's DECIMAL/NUMERIC caps at precision 65, scale 30 - a model
    delta naming a wider type must be rejected before it reaches DDL (I1,
    #156). PostgreSQL NUMERIC has no equivalent cap.
    """
    validate_aurora_type(value, "aurora_postgresql")  # PostgreSQL has no cap
    with pytest.raises(ValueError, match="exceeds Aurora MySQL's limits"):
        validate_aurora_type(value, "aurora_mysql")


@pytest.mark.parametrize("value", ["DEC(70,2)", "FIXED(126,0)"])
def test_mysql_only_decimal_aliases_also_enforce_the_limit(value):
    """``DEC``/``FIXED`` are MySQL-only aliases (not valid PostgreSQL types at
    all), so the limit check must still fire for them specifically."""
    with pytest.raises(ValueError, match="exceeds Aurora MySQL's limits"):
        validate_aurora_type(value, "aurora_mysql")


def test_mysql_decimal_scale_over_precision_is_rejected():
    """``DECIMAL(10,20)`` is well within both digit caps but still invalid
    DDL on Aurora MySQL (scale cannot exceed precision) - the limit check
    must reject it too, not just the two hard caps."""
    with pytest.raises(ValueError, match="scale cannot exceed precision"):
        validate_aurora_type("DECIMAL(10,20)", "aurora_mysql")


def test_mysql_decimal_limit_message_does_not_repeat_the_type():
    """``validate_aurora_type`` already names the type once (``aurora_type
    'DECIMAL(66,0)' ...``); the limit reason must not restate it, or the
    message reads ``aurora_type 'DECIMAL(66,0)' DECIMAL(66,0) exceeds ...``.
    """
    with pytest.raises(ValueError) as exc_info:
        validate_aurora_type("DECIMAL(66,0)", "aurora_mysql")
    message = str(exc_info.value)
    assert message.count("DECIMAL(66,0)") == 1, message


@pytest.mark.parametrize("value", ["DECIMAL(65,30)", "DECIMAL(10,2)", "NUMERIC(1,0)"])
def test_mysql_decimal_at_or_under_limit_is_accepted(value):
    assert validate_aurora_type(value, "aurora_mysql") == value
