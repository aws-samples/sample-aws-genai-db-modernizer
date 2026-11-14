# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two analysis advisor skills follow their structured outputs and the designer.

The DynamoDB and DocumentDB advisors answer through Strands structured output with
strict tools, so the schema already holds the answer to its shape: a "valid JSON only"
paragraph adds nothing and wrongly suggests a bare-text answer. What the schema cannot
say, the skill must: which input value each id copies (the pipeline matches a key
design to its aggregate by ``aggregate_id`` and drops one that matches none), every
field the model fills, and the embed bounds the DocumentDB schema designer enforces
(it turns any other ``embed`` into ``reference``). The strict schema also offers null
for each defaulted field, so those fields read null as their default. Nothing here
calls a model.
"""

from __future__ import annotations

import asyncio
import json
import re
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from src.contracts.dynamodb_decision_trace import AggregateKeyDesign, DenormStrategy
from src.tools.analysis import documentdb_analysis_tools as docdb_tools
from src.tools.analysis import dynamodb_analysis_tools as ddb_tools

REPO = Path(__file__).resolve().parents[4]
DESIGNER_SKILL = REPO / "src" / "skills" / "documentdb-data-modeling.md"


def _ddb_skill() -> str:
    return ddb_tools.load_advisor_prompt()


def _docdb_skill() -> str:
    return docdb_tools.load_docdb_advisor_prompt()


SKILLS = {"dynamodb": _ddb_skill, "documentdb": _docdb_skill}
#: Each advisor's structured output and the item model of each of its lists.
OUTPUTS: dict[str, type[BaseModel]] = {
    "dynamodb": ddb_tools.LlmAdvisorOutput,
    "documentdb": docdb_tools.LlmDocumentDBOutput,
}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = re.search(r"\n## ", text[start + len(heading) :])
    return text[start : start + len(heading) + following.start()] if following else text[start:]


def _item_models(model: type[BaseModel]) -> list[type[BaseModel]]:
    out = []
    for field in model.model_fields.values():
        for arg in typing.get_args(field.annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                out.append(arg)
    return out


def _literal_values(annotation: Any) -> set[str]:
    return {a for a in typing.get_args(annotation) if isinstance(a, str)}


@pytest.mark.parametrize("engine", sorted(SKILLS))
def test_no_dated_scaffolding(engine: str) -> None:
    text = SKILLS[engine]()
    assert "valid JSON" not in text and "## Constraint" not in text
    assert "Critical Rules" not in text
    shouted = re.findall(r"\b(?:MUST|SHOULD|NOT|NEVER|ALWAYS|ONLY)\b", text)
    assert shouted == [], shouted


@pytest.mark.parametrize("engine", sorted(SKILLS))
def test_the_output_section_names_every_field_of_the_structured_output(engine: str) -> None:
    """Every field the model fills, at both levels, and none the output lacks."""
    output = OUTPUTS[engine]
    section = _section(SKILLS[engine](), "## Output")
    top = set(re.findall(r"^`(\w+)`:", section, re.M))
    items = {
        name
        for line in re.findall(r"^- (.*?):", section, re.M)
        for name in re.findall(r"`(\w+)`", line)
    }
    assert top == set(output.model_fields)
    assert items == {name for model in _item_models(output) for name in model.model_fields}


def test_the_dynamodb_ids_are_copied_from_the_input() -> None:
    section = _flat(_section(_ddb_skill(), "## Output"))
    assert "`aggregate_id`: the aggregate's own `aggregate_id`, copied exactly" in section
    assert "an empty list when the input lists none" in section


class _Aggregate(BaseModel):
    """The two members of an aggregate recommendation the merge reads and writes."""

    aggregate_id: str
    partition_key: str | None = None
    sort_key: str | None = None
    key_design_rationale: str | None = None
    supporting_access_patterns: list[str] = []


class _Analysis(BaseModel):
    aggregate_recommendations: list[_Aggregate] | None = None


@pytest.mark.passes_on_main  # a guard: the pipeline already matches by id
def test_a_key_design_applies_only_to_the_aggregate_with_its_id() -> None:
    """Why the skill says to copy the id: the merge is keyed on it."""
    from src.agents.analysis.dynamodb_analysis_agent import apply_dynamodb_llm_output

    analysis = _Analysis(aggregate_recommendations=[_Aggregate(aggregate_id="agg-orders")])
    advice = ddb_tools.LlmAdvisorOutput(
        aggregate_recommendations=[
            AggregateKeyDesign(
                aggregate_id="agg-orders", partition_key="customer_id", rationale="r"
            ),
            AggregateKeyDesign(aggregate_id="orders", partition_key="id", rationale="r"),
        ],
        denormalization_strategies=[],
    )
    merged = apply_dynamodb_llm_output(analysis, advice)  # type: ignore[arg-type]
    assert [a.partition_key for a in merged.aggregate_recommendations] == ["customer_id"]


def test_the_documentdb_values_are_the_contracts() -> None:
    lines = _section(_docdb_skill(), "## Output").splitlines()
    model = docdb_tools.DenormalizationStrategy
    for field in ("strategy", "risk_level"):
        line = next(ln for ln in lines if ln.startswith(f"- `{field}`:"))
        named = set(re.findall(r"`(\w+)`", line.split(":", 1)[1]))
        assert named == _literal_values(model.model_fields[field].annotation), field


def test_the_documentdb_embed_bounds_are_the_designers() -> None:
    """The designer turns an ``embed`` outside its bounds into ``reference``; the advisor
    states the same bounds, so its advice survives."""
    designer = _flat(DESIGNER_SKILL.read_text(encoding="utf-8"))
    bounds = re.search(
        r"verify `avg_children_per_parent <= (\d+)`, `co_access_ratio >= ([\d.]+)`, "
        r"`exceeds_16mb == false`",
        designer,
    )
    assert bounds, "the designer skill states its embed bounds"
    advisor = _flat(_docdb_skill())
    assert (
        f"`avg_children_per_parent` is at most {bounds.group(1)}, its `co_access_ratio` is "
        f"at least {bounds.group(2)} and `exceeds_16mb` is false"
    ) in advisor


@pytest.mark.parametrize(
    ("model", "payload", "field", "empty"),
    [
        (
            AggregateKeyDesign,
            {"aggregate_id": "a", "partition_key": "p", "rationale": "r"},
            "supporting_access_patterns",
            [],
        ),
        (
            DenormStrategy,
            {"opportunity_id": "o", "strategy": "s", "rationale": "r"},
            "supporting_access_patterns",
            [],
        ),
        (
            docdb_tools.LlmDocumentDBOutput,
            {"denormalization_strategies": []},
            "collection_design_notes",
            "",
        ),
    ],
    ids=["AggregateKeyDesign", "DenormStrategy", "LlmDocumentDBOutput"],
)
def test_a_defaulted_field_reads_null_as_its_default(
    model: type[BaseModel], payload: dict, field: str, empty: Any
) -> None:
    assert getattr(model.model_validate({**payload, field: None}), field) == empty


@pytest.mark.passes_on_main  # a guard: the strict schema the advisor sees offers null
def test_the_strict_schema_offers_null_for_those_fields() -> None:
    from strands.tools.structured_output.structured_output_utils import (
        convert_pydantic_to_tool_spec,
    )

    schema = json.dumps(convert_pydantic_to_tool_spec(ddb_tools.LlmAdvisorOutput)["inputSchema"])
    assert '"supporting_access_patterns": {' in schema and '"null"' in schema


def test_strands_accepts_an_answer_with_null_lists_on_the_first_try() -> None:
    from strands.tools.structured_output._structured_output_context import (
        StructuredOutputContext,
    )
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    output = ddb_tools.LlmAdvisorOutput
    tool = StructuredOutputTool(output)
    context = StructuredOutputContext(output)
    answer = {
        "aggregate_recommendations": [
            {
                "aggregate_id": "agg-orders",
                "partition_key": "customer_id",
                "sort_key": None,
                "rationale": "r",
                "supporting_access_patterns": None,
            }
        ],
        "denormalization_strategies": [],
    }
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": answer}

    async def run() -> list[Any]:
        return [e async for e in tool.stream(use, {}, structured_output_context=context)]  # type: ignore[arg-type]

    result = asyncio.run(run())[-1].tool_result
    assert result["status"] == "success", result
