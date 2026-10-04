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
