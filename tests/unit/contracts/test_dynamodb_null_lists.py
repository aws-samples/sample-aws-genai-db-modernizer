# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every optional list of the DynamoDB designer's output reads ``null`` as empty (#2038).

Strands builds the structured-output tool's schema with every field that has a default
typed ``[..., "null"]``, so the model is told a list it has nothing for may be ``null``.
``TableDefinition.gsis`` (``Field(default_factory=list)``) refused it, and the whole
design was asked for again: in the saved Bedrock smokes since 2026-11-06 the DynamoDB
designer wrote ``"gsis": null`` on tables with no GSI (nine of nine in one run), one to
three Opus re-asks per run. A list that defaults to empty means the same thing by
``null``, so every such list in the DynamoDB contract reads it as empty
(``null_lists_as_empty``, as ElastiCache does since #1675). Required lists and lists
typed ``list | None`` are unchanged, and so is the JSON schema.
"""

from __future__ import annotations

import asyncio
import json
import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from scripts.contract_schema_snapshot import SNAPSHOT_PATH, build_snapshot
from src.contracts.dynamodb_model_output import (
    AccessPattern,
    DynamoDBModelOutputContract,
    TableDefinition,
)

SNAPSHOT_KEY = "dynamodb_model_output.DynamoDBModelOutputContract"

#: Models whose optional lists already read null as empty before #2038 (#1675).
ALREADY_LENIENT = ("src.contracts.schema_design_output",)


def _models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _models(arg)]


def _reachable(root: type[BaseModel]) -> list[type[BaseModel]]:
    seen: list[type[BaseModel]] = []
    todo = [root]
    while todo:
        model = todo.pop()
        if model in seen:
            continue
        seen.append(model)
        for field in model.model_fields.values():
            todo += _models(field.annotation)
    return seen


def _owner(model: type[BaseModel], name: str) -> str:
    """The module of the class that declares ``name`` (a subclass inherits its validator)."""
    for klass in model.__mro__:
        if name in getattr(klass, "__annotations__", {}):
            return klass.__module__
    return model.__module__


def _optional_lists() -> list[Any]:
    cases = []
    for model in _reachable(DynamoDBModelOutputContract):
        for name, field in model.model_fields.items():
            if field.default_factory is list:
                marks = (
                    [pytest.mark.passes_on_main] if _owner(model, name) in ALREADY_LENIENT else []
                )
                cases.append(pytest.param(model, name, id=f"{model.__name__}.{name}", marks=marks))
    return cases


OPTIONAL_LISTS = _optional_lists()


def _null_errors(model: type[BaseModel], field: str) -> list[str]:
    """The messages of the errors a null ``field`` raises on ``model`` itself."""
    try:
        model.model_validate({field: None})
    except ValidationError as exc:
        return [e["msg"] for e in exc.errors() if e["loc"] == (field,)]
    return []


def _design(**overrides: Any) -> dict[str, Any]:
    """A minimal design the contract accepts, with ``overrides`` applied at the root."""
    design: dict[str, Any] = {
        "job_id": "job-1",
        "source_database": "wordpress",
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "pattern_group": "Option reads",
                "query_ids": ["q1"],
                "source_tables": ["wp_options"],
                "description": "Read one option by name",
                "operation": "GetItem",
                "table_name": "options",
                "key_condition": "PK=option_name",
                "design_rps": 1.0,
                "item_size_bytes": 100,
            }
        ],
        "table_definitions": [
            {
                "table_name": "options",
                "aggregate_pattern": "separate",
                "source_tables": ["wp_options"],
                "partition_key": {"attribute_name": "option_name", "attribute_type": "S"},
                "attributes": [
                    {
                        "name": "option_name",
                        "type": "S",
                        "source_table": "wp_options",
                        "source_column": "option_name",
                    }
                ],
                "item_count": 10,
                "item_size_bytes": 100,
            }
        ],
        "hot_partition_analysis": [],
        "trade_offs": [{"description": "d", "impact": "i"}],
        "validation_passed": True,
    }
    design.update(overrides)
    return design


@pytest.mark.passes_on_main  # a guard: the parametrized cases are not vacuous
def test_the_contract_has_the_optional_lists_this_file_covers() -> None:
    """The parametrized cases below are not vacuous: the lists the smokes hit are there."""
    ids = {case.id for case in OPTIONAL_LISTS}
    assert {
        "TableDefinition.gsis",
        "AccessPattern.analysis_pattern_ids",
        "DynamoDBModelOutputContract.unsupported_patterns",
        "DynamoDBModelOutputContract.migration_notes",
        "DynamoDBModelOutputContract.validation_failures",
        "DynamoDBModelOutputContract.unresolved_merge_conflicts",
        "UnresolvedMergeConflict.pattern_ids",
        "UnresolvedMergeConflict.query_ids",
        "DynamoDBTradeOff.source_tables",
        "DynamoDBTradeOff.target_tables",
        "DynamoDBTradeOff.query_ids",
    } <= ids


@pytest.mark.parametrize(("model", "field"), OPTIONAL_LISTS)
def test_every_optional_list_reads_null_as_empty(model: type[BaseModel], field: str) -> None:
    assert _null_errors(model, field) == []


def test_a_design_with_every_optional_list_null_loads_them_as_empty() -> None:
    design = _design(
        unsupported_patterns=None,
        migration_notes=None,
        validation_failures=None,
        unresolved_merge_conflicts=None,
        trade_offs=[
            {
                "description": "d",
                "impact": "i",
                "source_tables": None,
                "target_tables": None,
                "query_ids": None,
            }
        ],
    )
    design["access_patterns"][0]["analysis_pattern_ids"] = None
    design["table_definitions"][0]["gsis"] = None

    out = DynamoDBModelOutputContract.model_validate(design)

    assert out.unsupported_patterns == []
    assert out.migration_notes == []
    assert out.validation_failures == []
    assert out.unresolved_merge_conflicts == []
    assert out.access_patterns[0].analysis_pattern_ids == []
    assert out.table_definitions[0].gsis == []
    trade_off = out.trade_offs[0]
    assert (trade_off.source_tables, trade_off.target_tables, trade_off.query_ids) == ([], [], [])


def test_an_unresolved_merge_conflict_reads_null_lists_as_empty() -> None:
    conflict = {"failure": "f", "status": "none_named", "pattern_ids": None, "query_ids": None}
    out = DynamoDBModelOutputContract.model_validate(_design(unresolved_merge_conflicts=[conflict]))
    assert out.unresolved_merge_conflicts[0].pattern_ids == []
    assert out.unresolved_merge_conflicts[0].query_ids == []


@pytest.mark.passes_on_main  # a guard: the JSON schema must not move with the reader
def test_the_json_schema_is_unchanged() -> None:
    committed = json.loads(SNAPSHOT_PATH.read_text())[SNAPSHOT_KEY]
    assert build_snapshot()[SNAPSHOT_KEY] == committed


def _tool_schema() -> dict[str, Any]:
    from strands.tools.structured_output.structured_output_utils import (
        convert_pydantic_to_tool_spec,
    )

    schema: dict[str, Any] = convert_pydantic_to_tool_spec(DynamoDBModelOutputContract)[
        "inputSchema"
    ]["json"]
    return schema


@pytest.mark.passes_on_main  # a guard: why null arrives, the schema the model sees offers it
def test_the_tool_schema_offers_null_for_gsis() -> None:
    schema = _tool_schema()
    table = schema["properties"]["table_definitions"]["items"]
    assert "null" in table["properties"]["gsis"]["type"]


def test_strands_accepts_a_design_with_null_gsis_on_the_first_try() -> None:
    """Through Strands' own structured-output tool, as the designer's answer arrives."""
    from strands.tools.structured_output._structured_output_context import (
        StructuredOutputContext,
    )
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    tool = StructuredOutputTool(DynamoDBModelOutputContract)
    context = StructuredOutputContext(DynamoDBModelOutputContract)
    answer = _design(unsupported_patterns=None, migration_notes=None)
    answer["table_definitions"][0]["gsis"] = None
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": json.loads(json.dumps(answer))}

    async def run() -> list[Any]:
        return [e async for e in tool.stream(use, {}, structured_output_context=context)]  # type: ignore[arg-type]

    result = asyncio.run(run())[-1].tool_result
    assert result["status"] == "success", result


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_required_list_still_rejects_null() -> None:
    assert _null_errors(DynamoDBModelOutputContract, "access_patterns")
    assert _null_errors(TableDefinition, "source_tables")
    assert _null_errors(AccessPattern, "query_ids")


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_nullable_list_keeps_its_null() -> None:
    design = _design()
    design["access_patterns"][0]["returned_attributes"] = None
    out = DynamoDBModelOutputContract.model_validate(design)
    assert out.access_patterns[0].returned_attributes is None
    assert out.table_definitions[0].entities is None


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_wrong_type_is_still_rejected() -> None:
    design = _design()
    design["table_definitions"][0]["gsis"] = "ByAuthor"
    with pytest.raises(ValidationError):
        DynamoDBModelOutputContract.model_validate(design)
