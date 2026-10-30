# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A structured-output call that asks again is logged with its reason (#1675).

Strands asks the model again for its structured output in two cases: the answer
ended without calling the output tool (Strands appends its "format the previous
response" prompt and forces the tool, which Opus 5.5 gets as ``auto``), or the
tool's input failed the output model (Strands returns the validation errors as the
tool result). Each costs a full extra turn. ``RoleBedrockModel`` logs every such
request at INFO with the case and the failing field paths (never the validation
messages, which can quote model or customer text: DEBUG only, #143), keeps the
reasons, and the design trace counts them.

Real Strands agents run here, so a Strands release that rewords either request
fails these tests instead of silently hiding the re-asks. No real model is called.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel
from strands.tools.structured_output import DEFAULT_STRUCTURED_OUTPUT_PROMPT

from src.shared.role_bedrock_model import structured_output_reask
from tests.unit.shared.test_prompt_cache import (
    SCHEMA_MODELS,
    _Design,
    _designer_turns,
    _run_designer,
    _stream,
)

LOGGER = "src.shared.role_bedrock_model"


def _invalid_output() -> dict:
    return _stream(
        [{"toolUse": ("_Design", {"tables": "customer_secret_table"})}],
        "tool_use",
        {"inputTokens": 40, "outputTokens": 15},
    )


def _text_answer() -> dict:
    return _stream([{"text": '{"tables": ["a", "b"]}'}], "end_turn", {"inputTokens": 30})


def test_an_output_that_fails_validation_is_logged_with_its_field(caplog):
    load, valid = _designer_turns()
    caplog.set_level(logging.INFO, logger=LOGGER)
    _, model = _run_designer(
        SCHEMA_MODELS[0], conversation=True, responses=[load, _invalid_output(), valid]
    )
    assert model.structured_output_reasks == [
        "_Design input failed validation, 1 error(s) at: tables"
    ]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    reask = [m for m in infos if "structured output asked again" in m]
    assert len(reask) == 1
    assert "phase=<schema_design>" in reask[0] and "at: tables" in reask[0]
    # The model's (customer-derived) text never reaches INFO.
    assert not any("customer_secret_table" in m for m in infos)


def test_an_answer_without_the_output_tool_is_logged(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    load, valid = _designer_turns()
    _, model = _run_designer(
        SCHEMA_MODELS[0], conversation=True, responses=[load, _text_answer(), valid]
    )
    assert model.structured_output_reasks == ["the answer ended without calling the output tool"]
    assert any(
        "structured output asked again: the answer ended without calling the output tool"
        in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


def test_a_first_try_answer_records_no_reask(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _, model = _run_designer(SCHEMA_MODELS[0], conversation=True)
    assert model.structured_output_reasks == []
    assert not any("asked again" in r.getMessage() for r in caplog.records)


def test_a_failing_ordinary_tool_is_not_a_reask():
    messages = [
        {"role": "user", "content": [{"text": "design"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x"}}]},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t",
                        "status": "error",
                        "content": [{"text": "Error: input file not found"}],
                    }
                }
            ],
        },
    ]
    assert structured_output_reask(messages, None) is None


def test_a_forced_choice_on_a_first_prompt_is_not_a_reask():
    messages = [{"role": "user", "content": [{"text": "design"}]}]
    assert structured_output_reask(messages, {"tool": {"name": "_Design"}}) is None
    assert structured_output_reask([], None) is None


def test_the_reask_prompt_alone_without_a_forced_choice_is_not_a_reask():
    messages = [{"role": "user", "content": [{"text": DEFAULT_STRUCTURED_OUTPUT_PROMPT}]}]
    assert structured_output_reask(messages, None) is None
    assert structured_output_reask(messages, {"any": {}}) is not None


def test_the_detail_keeps_the_validation_messages_for_debug():
    load, valid = _designer_turns()
    requests, _ = _run_designer(
        SCHEMA_MODELS[0], conversation=True, responses=[load, _invalid_output(), valid]
    )
    specs = [t["toolSpec"] for t in requests[2]["toolConfig"]["tools"] if "toolSpec" in t]
    for spec in specs:  # the request's Bedrock shape back to Strands' ToolSpec
        spec["inputSchema"] = {"json": spec["inputSchema"]["json"]}
    reask = structured_output_reask(requests[2]["messages"], None, specs)
    assert reask is not None
    reason, detail = reask
    assert "tables" in reason and "valid list" not in reason
    assert "valid list" in detail  # pydantic's message, DEBUG only


def test_the_design_trace_counts_the_reasks():
    from src.tools.schema.base_schema_agent import SchemaDesignRunner, model_token_usage

    model = MagicMock()
    model.usage = {"calls": 3, "inputTokens": 10}
    model.structured_output_reasks = ["the answer ended without calling the output tool"]
    designer = MagicMock(return_value=MagicMock(structured_output=_Design(tables=["a"])))
    runner = SchemaDesignRunner(
        target_type="elasticache", output_model=_Design, model=model, designer_agent=designer
    )
    _, trace = runner.run("design")
    assert trace["token_usage"] == {"calls": 3, "inputTokens": 10, "structured_output_reasks": 1}
    model.structured_output_reasks = []
    assert model_token_usage(model) == {"calls": 3, "inputTokens": 10}
    assert model_token_usage(object()) is None


class _Mapped(BaseModel):
    """An output model with a dict keyed by source-derived names (like DynamoDB's
    ``select_columns``)."""

    columns: dict[str, int]


def test_a_dict_key_in_the_error_is_never_logged(caplog):
    """pydantic puts the dict key in the error location; INFO shows ``<key>`` instead."""
    from strands import Agent

    from src.shared.models import Role, build_bedrock_model

    model = build_bedrock_model(Role.SCHEMA_DESIGN, model_id=SCHEMA_MODELS[0])
    responses = [
        _stream(
            [{"toolUse": ("_Mapped", {"columns": {"wp_customer_col": "not a number"}})}],
            "tool_use",
            {"inputTokens": 10},
        ),
        _stream([{"toolUse": ("_Mapped", {"columns": {"wp_customer_col": 1}})}], "tool_use", {}),
    ]
    model.client = MagicMock()
    model.client.converse_stream.side_effect = lambda **_: responses.pop(0)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    agent = Agent(model=model, structured_output_model=_Mapped, callback_handler=None)
    assert agent("map").structured_output == _Mapped(columns={"wp_customer_col": 1})
    assert model.structured_output_reasks == [
        "_Mapped input failed validation, 1 error(s) at: columns -> <key>"
    ]
    infos = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert any("columns -> <key>" in m for m in infos)
    assert not any("wp_customer_col" in m for m in infos)


def test_the_dynamodb_select_columns_path_keeps_only_declared_names():
    """The path the review named: a source-derived column under ``select_columns``."""
    from pydantic import ValidationError
    from strands.tools.structured_output import convert_pydantic_to_tool_spec

    from src.contracts.dynamodb_model_output import (
        AttributeDefinition,
        DynamoDBModelOutputContract,
    )
    from src.shared.role_bedrock_model import _output_schema, declared_path

    spec = convert_pydantic_to_tool_spec(DynamoDBModelOutputContract)
    schema = _output_schema([spec], spec["name"])
    with pytest.raises(ValidationError) as exc:
        AttributeDefinition.model_validate(
            {
                "name": "x",
                "type": "S",
                "source_table": "t",
                "source_column": "c",
                "join": {
                    "type": "json-construction",
                    "target_table": "t",
                    "join_condition": "c",
                    "construction": {"type": "object", "select_columns": {"cust_ssn": 5}},
                },
            }
        )
    (loc,) = [" -> ".join(str(p) for p in e["loc"]) for e in exc.value.errors()]
    assert "cust_ssn" in loc  # pydantic's own location carries the key
    path = declared_path(f"table_definitions -> 0 -> attributes -> 2 -> {loc}", schema)
    assert path == (
        "table_definitions -> 0 -> attributes -> 2 -> join -> json-construction -> "
        "construction -> select_columns -> <key>"
    )


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tables", "tables"),
        ("tables -> 3", "tables -> 3"),
        ("customer_text", "<?>"),
        ("tables -> customer_text", "tables -> <?>"),
    ],
)
def test_undeclared_segments_are_replaced(path, expected):
    from src.shared.role_bedrock_model import declared_path

    assert declared_path(path, _Design.model_json_schema()) == expected


def test_without_the_output_schema_only_indexes_survive():
    from src.shared.role_bedrock_model import declared_path

    assert declared_path("table_name -> 2 -> cust_col", None) == "<?> -> 2 -> <?>"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t",
                        "status": "error",
                        "content": [
                            {
                                "text": "Validation failed for Secret_Name. Please fix the "
                                "following errors:\n- Field 'cust_col': Field required"
                            }
                        ],
                    }
                }
            ],
        }
    ]
    reask = structured_output_reask(messages, None, tool_specs=None)
    assert reask is not None
    assert reask[0] == "the output tool input failed validation, 1 error(s) at: <?>"
