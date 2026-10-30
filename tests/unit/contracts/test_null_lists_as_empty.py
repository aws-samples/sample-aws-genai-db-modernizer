# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An explicit ``null`` for an optional list reads as empty in the ElastiCache contract (#1675).

The ElastiCache designer's first answer was rejected, and the whole design asked for
again (one Opus turn, about 25K output tokens), because two TTL-only
``cache_invalidation`` entries had ``"source_write_query_ids": null``: they have no
write query, and the same objects carry nullable fields (``pipeline_commands``,
``lua_script``), so the model wrote ``null`` for "none" (diagnostic Bedrock run on the
wordpress sample, 2026-10-29). A list that defaults to empty means exactly that, so it
now reads as empty. Required lists and nullable lists are unchanged, and so is the
JSON schema the model is given (``scripts/render_contract_schemas.py --check``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from src.contracts import elasticache_model_output as ec
from src.contracts.schema_design_output import TradeOff

MODELS: list[type[BaseModel]] = [
    ec.KeyDesign,
    ec.AccessPattern,
    ec.UnsupportedPattern,
    ec.MigrationNote,
    ec.CacheInvalidationStrategy,
    ec.ElastiCacheModelOutputContract,
    TradeOff,
]


def _design(**invalidation: Any) -> dict:
    """A minimal valid design, with one cache-invalidation entry."""
    return {
        "job_id": "j",
        "source_database": "wordpress",
        "key_designs": [
            {
                "key_pattern": "wp_options:{option_name}",
                "data_type": "string",
                "use_case": "caching",
                "source_tables": ["wordpress.wp_options"],
                "rationale": "r",
                "example_key": "wp_options:siteurl",
                "example_value": "v",
            }
        ],
        "access_patterns": [
            {
                "pattern_id": "EC-AP-1",
                "description": "d",
                "operation": "GET",
                "key_pattern": "wp_options:{option_name}",
                "command_example": "GET wp_options:siteurl",
                "source_tables": ["wordpress.wp_options"],
                "source_query_ids": ["q1"],
            }
        ],
        "cache_invalidation": [
            {
                "key_pattern": "wp_options:{option_name}",
                "strategy": "ttl",
                "description": "expires after an hour",
                **invalidation,
            }
        ],
        "trade_offs": [{"description": "d", "impact": "i", "engine": "elasticache"}],
        "validation_passed": True,
    }


def test_the_rejected_answer_now_validates():
    design = ec.ElastiCacheModelOutputContract.model_validate(_design(source_write_query_ids=None))
    assert design.cache_invalidation[0].source_write_query_ids == []


def test_strands_accepts_it_on_the_first_try():
    """Through Strands' own structured-output tool, as the designer's answer arrives."""
    from strands.tools.structured_output._structured_output_context import (
        StructuredOutputContext,
    )
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    tool = StructuredOutputTool(ec.ElastiCacheModelOutputContract)
    context = StructuredOutputContext(ec.ElastiCacheModelOutputContract)
    use = {
        "toolUseId": "t1",
        "name": tool.tool_name,
        "input": json.loads(json.dumps(_design(source_write_query_ids=None))),
    }

    async def run() -> list[Any]:
        return [e async for e in tool.stream(use, {}, structured_output_context=context)]

    result = asyncio.run(run())[-1].tool_result
    assert result["status"] == "success", result


def _optional_lists(model: type[BaseModel]) -> list[str]:
    return [n for n, f in model.model_fields.items() if f.default_factory is list]


def _valid(model: type[BaseModel]) -> dict:
    """A valid payload for ``model``, from the minimal design."""
    design = _design()
    payloads: dict[type[BaseModel], dict] = {
        ec.KeyDesign: design["key_designs"][0],
        ec.AccessPattern: design["access_patterns"][0],
        ec.UnsupportedPattern: {"source_query_ids": ["q1"], "reason": "r"},
        ec.MigrationNote: {
            "object_name": "o",
            "object_type": "trigger",
            "application_logic_required": "a",
        },
        ec.CacheInvalidationStrategy: design["cache_invalidation"][0],
        ec.ElastiCacheModelOutputContract: design,
        TradeOff: design["trade_offs"][0],
    }
    return payloads[model]


@pytest.mark.parametrize(
    "model,field",
    [(m, f) for m in MODELS for f in _optional_lists(m)],
    ids=lambda v: v if isinstance(v, str) else v.__name__,
)
def test_every_optional_list_reads_null_as_empty(model, field):
    assert getattr(model.model_validate({**_valid(model), field: None}), field) == []


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_required_list_still_rejects_null():
    with pytest.raises(ValidationError) as exc:
        ec.ElastiCacheModelOutputContract.model_validate({**_design(), "key_designs": None})
    assert [e["loc"] for e in exc.value.errors()] == [("key_designs",)]


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_nullable_list_keeps_its_null():
    pattern = _design()["access_patterns"][0]
    assert (
        ec.AccessPattern.model_validate({**pattern, "pipeline_commands": None}).pipeline_commands
        is None
    )


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_wrong_type_is_still_rejected():
    with pytest.raises(ValidationError):
        ec.ElastiCacheModelOutputContract.model_validate(
            _design(source_write_query_ids="not-a-list")
        )


def test_the_trade_off_list_reads_null_as_empty():
    trade_off = TradeOff.model_validate(
        {"description": "d", "impact": "i", "source_tables": None, "query_ids": None}
    )
    assert trade_off.source_tables == [] and trade_off.query_ids == []
