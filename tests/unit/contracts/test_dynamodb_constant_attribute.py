# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A DynamoDB design can declare a constant attribute (#1531).

An entity-type label or a sparse-index flag has no source column: before the
``constant`` calculation a designer could only borrow an unrelated column (for
example ``IdCounters.entity_name <- comment_ID``), which made the lineage wrong.
Designs written before the change still load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

# Only names main already has, so ci/main_tests.py runs these tests against main's code.
from src.contracts.dynamodb_model_output import AttributeDefinition, DynamoDBModelOutputContract

REPO = Path(__file__).resolve().parents[3]

#: Designs an earlier release wrote, before the constant calculation existed.
OLD_DESIGNS = [
    "tests/fixtures/real_runs/wordpress-both/job/schema-dynamodb/v2/schema_output.json",
    "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json",
]


def constant(name: str, type_: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": type_,
        "source_table": "wp_comments",
        "calculation": {"type": "constant", "value": value},
        **extra,
    }


def test_an_entity_type_label_needs_no_source_column() -> None:
    """The source table stays: it names the rows whose items carry the label."""
    attr = AttributeDefinition.model_validate(constant("entity_name", "S", "wp_comments"))
    assert attr.calculation is not None and attr.calculation.type == "constant"
    assert attr.calculation.value == "wp_comments"
    assert attr.source_column is None and attr.source_table == "wp_comments"


@pytest.mark.parametrize(
    ("type_", "value"),
    [("S", "1"), ("N", 1), ("N", 2.5), ("BOOL", True), ("BOOL", False)],
)
def test_a_constant_value_matches_its_attribute_type(type_: str, value: Any) -> None:
    attr = AttributeDefinition.model_validate(constant("flag", type_, value))
    assert attr.calculation is not None and attr.calculation.type == "constant"
    assert attr.calculation.value == value and type(attr.calculation.value) is type(value)


def test_a_constant_round_trips_through_json() -> None:
    attr = AttributeDefinition.model_validate(constant("is_pending", "BOOL", True))
    again = AttributeDefinition.model_validate_json(attr.model_dump_json())
    assert again == attr
    assert again.model_dump(mode="json")["calculation"] == {"type": "constant", "value": True}


@pytest.mark.parametrize(
    ("type_", "value"),
    [("S", 1), ("S", True), ("N", "1"), ("N", True), ("BOOL", "true"), ("BOOL", 1), ("M", "x")],
)
def test_a_constant_whose_value_does_not_match_its_type_is_refused(type_: str, value: Any) -> None:
    with pytest.raises(ValidationError, match="a constant of type"):
        AttributeDefinition.model_validate(constant("flag", type_, value))


def test_a_constant_that_also_names_a_source_column_is_refused() -> None:
    """Borrowing a column next to the literal is the wrong lineage the constant replaces."""
    with pytest.raises(ValidationError, match="a constant names no source_column"):
        AttributeDefinition.model_validate(
            constant("entity_name", "S", "wp_comments", source_column="comment_ID")
        )


def test_a_constant_with_a_join_is_refused() -> None:
    join = {
        "type": "foreign-key",
        "target_table": "t",
        "join_condition": "a=b",
        "select_column": "c",
    }
    with pytest.raises(ValidationError, match="a constant takes no join"):
        AttributeDefinition.model_validate(constant("entity_name", "S", "x", join=join))


def test_an_attribute_that_is_not_a_constant_still_needs_its_source_column() -> None:
    attr = {"name": "email", "type": "S", "source_table": "users"}
    for given in ({}, {"source_column": None}):
        with pytest.raises(ValidationError, match="source_column is required unless"):
            AttributeDefinition.model_validate({**attr, **given})


@pytest.mark.passes_on_main  # main already required it, for every attribute
def test_a_constant_still_names_its_source_table() -> None:
    attr = constant("entity_name", "S", "wp_comments")
    del attr["source_table"]
    with pytest.raises(ValidationError, match="source_table"):
        AttributeDefinition.model_validate(attr)


@pytest.mark.passes_on_main  # main loaded it too; the finalize check, not the contract, fails it
@pytest.mark.parametrize("empty", ["", []])
def test_a_legacy_attribute_with_an_empty_source_column_still_loads(empty: Any) -> None:
    """Before constants existed a design wrote ``""`` for a literal: it loads, and only the
    finalize check (``check_columns``) fails it, so saved designs keep loading."""
    attr = AttributeDefinition.model_validate(
        {"name": "entity_name", "type": "S", "source_table": "wp_comments", "source_column": empty}
    )
    assert attr.source_column == empty and attr.calculation is None


@pytest.mark.passes_on_main  # guards behaviour main already has: old designs load
@pytest.mark.parametrize("path", OLD_DESIGNS)
def test_a_design_written_before_constants_still_loads(path: str) -> None:
    payload = json.loads((REPO / path).read_text())
    loaded = DynamoDBModelOutputContract.model_validate(payload)
    attrs = [a for t in loaded.table_definitions for a in t.attributes or []] + [
        a for t in loaded.table_definitions for e in t.entities or [] for a in e.attributes
    ]
    assert attrs
    assert not any(getattr(a.calculation, "type", None) == "constant" for a in attrs)
    assert all(a.source_column is not None for a in attrs)
