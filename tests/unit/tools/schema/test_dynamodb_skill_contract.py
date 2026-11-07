# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB designer skill names only fields its output contract has (#2038).

A designer that follows its skill writes the fields the skill names. The skill called a
GSI's name ``index_name`` (the contract's field is ``gsi_name``), left out a GSI's
required ``item_count`` and ``item_size_bytes``, and named ``calculation.aggregate``
without the four fields an aggregate needs; each one failed validation and cost a whole
structured-output re-ask on Bedrock. The contract is the source of truth; these tests read
the skill against it. Nothing here calls a model.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.contracts import dynamodb_model_output as ddb
from src.tools.schema import dynamodb_schema_agent

SKILL = Path(dynamodb_schema_agent.DEFAULT_SKILL_PATH).read_text(encoding="utf-8")
FLAT = " ".join(SKILL.split())


def _required(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.is_required()}


def _spans(text: str) -> set[str]:
    """The identifier-shaped code spans in ``text`` (field names, not values or SQL)."""
    return {s for s in re.findall(r"`([^`]+)`", text) if re.fullmatch(r"[a-z][a-z0-9_]*", s)}


def _step_6() -> str:
    start = FLAT.index("**Step 6")
    return FLAT[start : FLAT.index("---", start)]


def _gsi_clause() -> str:
    """The parenthesis after ``gsis`` in step 6: what each GSI carries."""
    text = _step_6()
    start = text.index("`gsis` (") + len("`gsis` (")
    depth, end = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        end += 1
    return text[start : end - 1]


def _bullets(heading: str) -> set[str]:
    """The ``- `field`:`` bullets of the Phase 2b paragraph that starts with ``heading``."""
    start = SKILL.index(f"\n{heading} — ")
    block = SKILL[start : SKILL.index("\n\n`", start + 1)]
    return set(re.findall(r"^- `([a-z_]+)`:", block, flags=re.M))


def test_step_6_names_a_gsi_by_the_contract_field() -> None:
    clause = _gsi_clause()
    assert "`index_name`" not in clause
    assert "`gsi_name`" in clause


def test_step_6_names_every_required_gsi_field() -> None:
    assert _required(ddb.GSIDefinition) <= _spans(_gsi_clause())


def test_every_gsi_field_step_6_names_is_a_contract_field() -> None:
    assert _spans(_gsi_clause()) - {"null"} <= set(ddb.GSIDefinition.model_fields)


@pytest.mark.passes_on_main  # a guard: main's skill already names these fields right
def test_every_table_field_step_6_names_is_a_contract_field() -> None:
    text = _step_6()
    table_part = text.replace(_gsi_clause(), "")
    patterns = set(typing.get_args(ddb.AGGREGATE_PATTERNS))
    named = _spans(table_part) - patterns
    assert named <= set(ddb.TableDefinition.model_fields), named - set(
        ddb.TableDefinition.model_fields
    )
    assert _required(ddb.TableDefinition) <= named


@pytest.mark.passes_on_main  # a guard: main's skill already names these fields right
@pytest.mark.parametrize(
    ("heading", "model"),
    [("`access_patterns`", ddb.AccessPattern), ("`table_definitions`", ddb.TableDefinition)],
)
def test_every_phase_2b_field_bullet_is_a_contract_field(
    heading: str, model: type[BaseModel]
) -> None:
    bullets = _bullets(heading)
    assert bullets, heading
    assert bullets <= set(model.model_fields), bullets - set(model.model_fields)


@pytest.mark.passes_on_main  # a guard: main's skill already names these fields right
def test_every_phase_2b_root_paragraph_is_a_contract_field() -> None:
    roots = set(re.findall(r"^`([a-z_]+)` — ", SKILL, flags=re.M))
    assert roots
    assert roots <= set(ddb.DynamoDBModelOutputContract.model_fields)


def _aggregate_rule() -> str:
    return next(line for line in SKILL.splitlines() if "`calculation.aggregate`" in line)


def test_the_aggregate_rule_names_every_field_an_aggregate_needs() -> None:
    rule = _aggregate_rule()
    for field in _required(ddb.AggregateCalculation):
        assert f'"{field}"' in rule, field


def test_the_aggregate_rule_names_exactly_the_contract_operations() -> None:
    operations = set(typing.get_args(ddb.AggregateCalculation.model_fields["operation"].annotation))
    named = set(re.findall(r"`([A-Z]+)`", _aggregate_rule()))
    assert named == operations


@pytest.mark.passes_on_main  # a guard: the contract already accepts this shape
def test_the_aggregate_rule_example_validates() -> None:
    """The shape the rule spells out is one the contract accepts."""
    ddb.AggregateCalculation.model_validate(
        {
            "type": "aggregate",
            "target_table": "order_items",
            "join_condition": "order_items.order_id = orders.id",
            "operation": "SUM",
            "select_column": "order_items.price * order_items.quantity",
        }
    )
