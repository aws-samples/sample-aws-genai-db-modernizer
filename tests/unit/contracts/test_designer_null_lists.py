# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every optional list a designer's structured output offers as null reads null as empty.

Strands builds the output tool's schema with every field that has a default typed
``[..., "null"]``, so the schema tells the model a list it has nothing for may be
``null``. A contract list declared ``Field(default_factory=list)`` rejected that, and the
whole design was asked for again: on the 2026-11-07 wordpress Bedrock smoke a DynamoDB
group wrote ``"gsis": null`` on all nine of its tables, none of which had a GSI, and
the same class of re-ask cost the ElastiCache designer a turn before #1675. A list that
defaults to empty means the same thing by ``null``, so the DocumentDB, OpenSearch,
ElastiCache and Aurora delta contracts read it as empty (``null_lists_as_empty``), on
every model a design reaches. Required lists and lists typed ``list | None`` are
unchanged, and so is the JSON schema.
"""

from __future__ import annotations

import asyncio
import json
import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
from src.contracts.opensearch_model_output import AccessPattern as OpenSearchAccessPattern
from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

#: The structured outputs these designers answer with on Bedrock.
OUTPUTS: tuple[type[BaseModel], ...] = (
    DocumentDBModelOutputContract,
    OpenSearchModelOutputContract,
    ElastiCacheModelOutputContract,
    AuroraDesignDeltaContract,
)


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


#: Models that already read null as empty before this change (#1675): their cases pass on main.
ALREADY_LENIENT = ("src.contracts.elasticache_model_output", "src.contracts.schema_design_output")


def _optional_lists() -> list[Any]:
    cases = []
    for output in OUTPUTS:
        for model in _reachable(output):
            for name, field in model.model_fields.items():
                if field.default_factory is list:
                    case_id = f"{output.__name__}.{model.__name__}.{name}"
                    marks = (
                        [pytest.mark.passes_on_main] if model.__module__ in ALREADY_LENIENT else []
                    )
                    cases.append(pytest.param(model, name, id=case_id, marks=marks))
    return cases


def _null_errors(model: type[BaseModel], field: str) -> list[str]:
    """The messages of the errors a null ``field`` raises on ``model`` itself."""
    try:
        model.model_validate({field: None})
    except ValidationError as exc:
        return [e["msg"] for e in exc.errors() if e["loc"] == (field,)]
    return []


def _tool_schema(output: type[BaseModel]) -> dict[str, Any]:
    from strands.tools.structured_output.structured_output_utils import (
        convert_pydantic_to_tool_spec,
    )

    schema: dict[str, Any] = convert_pydantic_to_tool_spec(output)["inputSchema"]["json"]
    return schema


@pytest.mark.passes_on_main  # a guard: the schema the model sees already offers null
def test_the_tool_schema_offers_null_for_an_optional_list() -> None:
    """Why null arrives: the schema says it may."""
    tables = _tool_schema(AuroraDesignDeltaContract)["properties"]["tables"]
    assert "null" in tables["type"]


@pytest.mark.parametrize(("model", "field"), _optional_lists())
def test_every_optional_list_reads_null_as_empty(model: type[BaseModel], field: str) -> None:
    assert _null_errors(model, field) == []


def test_strands_accepts_a_delta_with_null_lists_on_the_first_try() -> None:
    """Through Strands' own structured-output tool, as the Aurora designer's answer arrives."""
    from strands.tools.structured_output._structured_output_context import (
        StructuredOutputContext,
    )
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    tool = StructuredOutputTool(AuroraDesignDeltaContract)
    context = StructuredOutputContext(AuroraDesignDeltaContract)
    answer = {
        "delta_version": "1.0",
        "tables": [
            {
                "table_name": "wp_posts",
                "add_indexes": [{"index_name": "idx_status", "columns": ["post_status"]}],
                "modify_indexes": None,
                "remove_indexes": None,
                "column_types": None,
            }
        ],
        "type_rules": None,
        "optimizations": None,
        "app_layer_notes": None,
        "trade_offs": [{"description": "d", "impact": "i", "source_tables": None}],
    }
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": json.loads(json.dumps(answer))}

    async def run() -> list[Any]:
        return [e async for e in tool.stream(use, {}, structured_output_context=context)]  # type: ignore[arg-type]

    result = asyncio.run(run())[-1].tool_result
    assert result["status"] == "success", result


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_required_list_still_rejects_null() -> None:
    assert _null_errors(DocumentDBModelOutputContract, "collections")
    assert _null_errors(OpenSearchAccessPattern, "query_ids")


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_nullable_list_keeps_its_null() -> None:
    from src.contracts.documentdb_model_output import EmbeddedEntity

    entity = EmbeddedEntity.model_validate(
        {
            "source_table": "t",
            "embed_path": "tags",
            "strategy": "hybrid",
            "embedded_fields": None,
            "avg_array_length": 1,
            "max_array_length_estimate": 2,
            "rationale": "r",
        }
    )
    assert entity.embedded_fields is None


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_wrong_type_is_still_rejected() -> None:
    from src.contracts.aurora_design_delta import TableDelta

    with pytest.raises(ValidationError):
        TableDelta.model_validate({"table_name": "t", "remove_indexes": "idx_a"})
