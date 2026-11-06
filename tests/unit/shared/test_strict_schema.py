# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Strict-mode tool schemas stay inside Bedrock's supported subset (#744).

The analysis advisor sends ``strict: true`` on its output tool, so a schema
change that leaves the subset would turn every advisor call into a 400. These
tests check the checker itself, then every advisor output schema as Strands
sends it, and pin why schema design stays off strict mode.
"""

from __future__ import annotations

import pytest

from src.shared.models import ROLE_SPECS, Role
from src.shared.strict_schema import (
    MAX_OPTIONAL_PROPERTIES,
    MAX_UNION_PROPERTIES,
    strict_schema_violations,
)


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def test_a_schema_inside_the_subset_has_no_violations():
    schema = _obj(
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 300, "pattern": "^[a-z]+$"},
            "kind": {"type": "string", "enum": ["a", "b"]},
            "when": {"type": "string", "format": "date-time"},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "child": {"$ref": "#/$defs/Child"},
        }
    )
    schema["$defs"] = {"Child": _obj({"x": {"type": "integer"}})}
    assert strict_schema_violations(schema) == []


@pytest.mark.parametrize(
    "prop,reason",
    [
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "'oneOf' is not supported"),
        ({"type": "integer", "minimum": 0}, "'minimum' is not supported"),
        ({"type": "number", "maximum": 100}, "'maximum' is not supported"),
        ({"type": "integer", "multipleOf": 2}, "'multipleOf' is not supported"),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 3}, "'maxItems'"),
        ({"type": "array", "items": {"type": "string"}, "minItems": 2}, "minItems 2"),
        (
            {"type": "object", "additionalProperties": {"type": "string"}},
            "additionalProperties must be false",
        ),
        ({"type": "object", "properties": {"x": {"type": "string"}}}, "additionalProperties"),
        ({"type": "string", "format": "credit-card"}, "format 'credit-card'"),
        ({"type": "string", "pattern": "^(?!x).*$"}, "lookaround"),
        ({"type": "string", "pattern": "(a)\\1"}, "backreference"),
        ({"$ref": "https://example.com/schema.json"}, "external $ref"),
    ],
)
def test_each_unsupported_feature_is_reported(prop, reason):
    found = strict_schema_violations(_obj({"field": prop}))
    assert any(reason in item and "$.field" in item for item in found), found


def test_a_recursive_ref_is_reported():
    schema = _obj({"root": {"$ref": "#/$defs/Node"}})
    schema["$defs"] = {"Node": _obj({"next": {"$ref": "#/$defs/Node"}})}
    assert any("recursive $ref" in item for item in strict_schema_violations(schema))


def test_a_nullable_object_without_additional_properties_false_is_reported():
    """Strands marks only ``type: "object"``, so a nullable object keeps no marker."""
    nullable = {"type": ["object", "null"], "properties": {"x": {"type": "string"}}}
    found = strict_schema_violations(_obj({"key": nullable}))
    assert any("$.key: additionalProperties must be false" in item for item in found)


def test_optional_properties_are_capped_across_the_whole_schema():
    at_cap = _obj({f"p{i}": {"type": "string"} for i in range(MAX_OPTIONAL_PROPERTIES)}, [])
    assert strict_schema_violations(at_cap) == []
    nested = _obj(
        {
            "a": _obj({f"p{i}": {"type": "string"} for i in range(MAX_OPTIONAL_PROPERTIES)}, []),
            "b": {"type": "string"},
        },
        ["a"],
    )
    assert any("optional properties" in item for item in strict_schema_violations(nested))


def test_union_typed_properties_are_capped():
    at_cap = _obj({f"p{i}": {"type": ["string", "null"]} for i in range(MAX_UNION_PROPERTIES)})
    assert strict_schema_violations(at_cap) == []
    over = _obj({f"p{i}": {"type": ["string", "null"]} for i in range(MAX_UNION_PROPERTIES + 1)})
    assert any("union-typed" in item for item in strict_schema_violations(over))


def test_a_ref_is_counted_at_every_use():
    """The grammar is compiled from the expanded schema, so a shared def counts twice."""
    half = MAX_OPTIONAL_PROPERTIES // 2 + 1
    schema = _obj({"a": {"$ref": "#/$defs/D"}, "b": {"$ref": "#/$defs/D"}})
    schema["$defs"] = {"D": _obj({f"p{i}": {"type": "string"} for i in range(half)}, [])}
    assert any("optional properties" in item for item in strict_schema_violations(schema))


# ---------------------------------------------------------------------------
# The schemas a strict role sends
# ---------------------------------------------------------------------------


def _sent_schema(output_model: type) -> dict:
    """The output tool's input schema in the advisor role's first Bedrock request.

    Built by Strands itself (tool spec, registry normalisation and, with
    ``strict_tools`` on, ``additionalProperties: false``), so the check runs on the
    exact schema Bedrock compiles. Also checks the tool goes out strict.
    """
    from unittest.mock import MagicMock

    from strands import Agent

    from src.shared.models import build_bedrock_model

    model = build_bedrock_model(Role.ANALYSIS_ADVISOR)
    requests: list[dict] = []

    def fake_converse_stream(**request):
        requests.append(request)
        raise RuntimeError("stop after the first request")

    model.client = MagicMock()
    model.client.converse_stream.side_effect = fake_converse_stream
    agent = Agent(
        model=model, tools=[], structured_output_model=output_model, callback_handler=None
    )
    with pytest.raises(Exception):  # noqa: B017 -- the fake client stops the call
        agent("advise")
    [tool] = requests[0]["toolConfig"]["tools"]
    assert tool["toolSpec"]["strict"] is True
    schema: dict = tool["toolSpec"]["inputSchema"]["json"]
    return schema


def _advisor_classes() -> list[type]:
    # Import every advisor so each subclass is registered.
    import src.tools.analysis.documentdb_analysis_tools  # noqa: F401
    import src.tools.analysis.dynamodb_analysis_tools  # noqa: F401
    from src.tools.analysis.llm_advisor_base import LlmAdvisorBase

    found: list[type] = []
    pending = list(LlmAdvisorBase.__subclasses__())
    while pending:
        cls = pending.pop()
        found.append(cls)
        pending.extend(cls.__subclasses__())
    return sorted(found, key=lambda cls: cls.__name__)


_ADVISORS = _advisor_classes()


def test_every_advisor_is_checked():
    assert {cls.__name__ for cls in _ADVISORS} >= {"LlmAdvisor", "LlmDocumentDBAdvisor"}


@pytest.mark.parametrize("advisor", _ADVISORS, ids=lambda cls: cls.__name__)
def test_advisor_output_schemas_compile_under_strict_mode(advisor):
    assert ROLE_SPECS[Role.ANALYSIS_ADVISOR].strict_tools
    output_model = advisor(system_prompt="x", enabled=False)._output_model()
    assert strict_schema_violations(_sent_schema(output_model)) == []


def test_the_dynamodb_design_contract_is_outside_the_subset():
    """Why ``Role.SCHEMA_DESIGN`` keeps ``strict_tools`` off: its contract would 400."""
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract

    found = strict_schema_violations(_sent_schema(DynamoDBModelOutputContract))
    for reason in (
        "'oneOf'",
        "'minimum'",
        "'maxItems'",
        "additionalProperties must be false",
        "optional properties",
        "union-typed",
    ):
        assert any(reason in item for item in found), reason
    assert not ROLE_SPECS[Role.SCHEMA_DESIGN].strict_tools
