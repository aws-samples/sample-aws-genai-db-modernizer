"""Bedrock prompt caching on the schema-design path (#626).

Schema design is the only role with ``prompt_cache``: every designer and PE
turn resends the same output-contract tool schema and skill text, and parallel
design groups share them too. These tests pin the request shape Bedrock
receives (cache points after the tool definitions and after the system prompt,
none in the conversation), that no other role changes, and the per-call token
accounting the design trace records.

Tests never call a real model: the Bedrock client's ``converse_stream`` is
patched.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from src.shared import models
from src.shared.models import (
    MODEL_IDS,
    Role,
    bedrock_model_kwargs,
    build_bedrock_model,
    min_cacheable_tokens,
)

_ALL_ROLE_ENV = sorted({name for role in Role for name in models.ROLE_SPECS[role].env_vars})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ALL_ROLE_ENV + ["SCHEMA_AGENT_MAX_TOKENS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


SCHEMA_MODELS = [models.ROLE_SPECS[Role.SCHEMA_DESIGN].default_model, MODEL_IDS["opus-5.5"]]


# ---------------------------------------------------------------------------
# Which roles cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_schema_design_turns_caching_on(model_id):
    config = bedrock_model_kwargs(Role.SCHEMA_DESIGN, model_id=model_id)["cache_config"]
    assert config.strategy == "auto"
    assert config.tools_ttl is True
    assert config.system_prompt_ttl is True
    assert config.ttl is None  # Bedrock's default 5-minute TTL, refreshed by each read


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.SCHEMA_DESIGN])
def test_other_roles_do_not_cache(role):
    assert "cache_config" not in bedrock_model_kwargs(role)
    assert models.ROLE_SPECS[role].prompt_cache is False


def test_unrecognised_model_id_gets_no_cache_points():
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc"
    assert "cache_config" not in bedrock_model_kwargs(Role.SCHEMA_DESIGN, model_id=arn)


@pytest.mark.parametrize(
    "model_id,minimum",
    [
        (MODEL_IDS["opus-4.6"], 4096),
        (MODEL_IDS["haiku-4.5"], 4096),
        (MODEL_IDS["sonnet-4.6"], 1024),
        ("anthropic.claude-opus-4-7", 2048),
        ("anthropic.claude-opus-4-8", 1024),
        ("anthropic.claude-sonnet-5", 1024),
        ("anthropic.claude-opus-5", 512),
        (MODEL_IDS["opus-5.5"], 512),
        (MODEL_IDS["sonnet-5.5"], 512),
        (MODEL_IDS["haiku-5.5"], 512),
        ("anthropic.claude-3-5-haiku-20241022-v1:0", 2048),
        ("anthropic.claude-3-5-sonnet-20241022-v2:0", 1024),
        ("arn:aws:bedrock:us-east-1:1:application-inference-profile/x", None),
    ],
)
def test_min_cacheable_tokens(model_id, minimum):
    assert min_cacheable_tokens(model_id) == minimum


# ---------------------------------------------------------------------------
# Request shape through a real Strands agent
# ---------------------------------------------------------------------------


class _Design(BaseModel):
    tables: list[str]


def _stream(blocks: list[dict], stop: str, usage: dict[str, int]) -> dict:
    events: list[dict] = [{"messageStart": {"role": "assistant"}}]
    for index, block in enumerate(blocks):
        if "toolUse" in block:
            name, payload = block["toolUse"]
            events.append(
                {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": f"t{index}", "name": name}},
                        "contentBlockIndex": index,
                    }
                }
            )
            events.append(
                {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps(payload)}},
                        "contentBlockIndex": index,
                    }
                }
            )
        else:
            events.append(
                {
                    "contentBlockDelta": {
                        "delta": {"text": block["text"]},
                        "contentBlockIndex": index,
                    }
                }
            )
        events.append({"contentBlockStop": {"contentBlockIndex": index}})
    events.append({"messageStop": {"stopReason": stop}})
    events.append({"metadata": {"usage": {"totalTokens": 0, **usage}, "metrics": {"latencyMs": 1}}})
    return {"stream": events}


SKILL_TEXT = "# Designer skill\n" + "Rule.\n" * 50


def _run_designer(model_id: str) -> tuple[list[dict], Any]:
    """A designer-shaped agent: a load-input tool turn, then the structured output."""
    from strands import Agent, tool

    @tool
    def load_agent_input() -> dict:
        """Load the projected input."""
        return {"tables": ["a", "b"]}

    model = build_bedrock_model(Role.SCHEMA_DESIGN, model_id=model_id)
    requests: list[dict] = []
    responses = [
        _stream(
            [{"toolUse": ("load_agent_input", {})}],
            "tool_use",
            {"inputTokens": 900, "outputTokens": 10, "cacheWriteInputTokens": 5000},
        ),
        _stream(
            [{"toolUse": ("_Design", {"tables": ["a", "b"]})}],
            "tool_use",
            {"inputTokens": 950, "outputTokens": 20, "cacheReadInputTokens": 5000},
        ),
    ]

    def fake_converse_stream(**request):
        requests.append(json.loads(json.dumps(request)))  # snapshot: Strands mutates in place
        return responses.pop(0) if responses else _stream([{"text": "done"}], "end_turn", {})

    model.client = MagicMock()
    model.client.converse_stream.side_effect = fake_converse_stream
    agent = Agent(
        model=model,
        system_prompt=SKILL_TEXT,
        tools=[load_agent_input],
        structured_output_model=_Design,
        callback_handler=None,
    )
    result = agent("Use load_agent_input, then design.")
    assert result.structured_output == _Design(tables=["a", "b"])
    return requests, model


def _cache_points(value: Any) -> int:
    if isinstance(value, dict):
        return ("cachePoint" in value) + sum(_cache_points(v) for v in value.values())
    if isinstance(value, list):
        return sum(_cache_points(v) for v in value)
    return 0


@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_designer_requests_cache_tools_and_system_only(model_id):
    requests, _ = _run_designer(model_id)
    assert len(requests) >= 2, "the tool-loop turn was not exercised"
    for request in requests:
        assert request["system"] == [{"text": SKILL_TEXT}, {"cachePoint": {"type": "default"}}]
        tools = request["toolConfig"]["tools"]
        assert tools[-1] == {"cachePoint": {"type": "default"}}
        assert all("toolSpec" in t for t in tools[:-1])
        assert {t["toolSpec"]["name"] for t in tools[:-1]} == {"load_agent_input", "_Design"}
        assert _cache_points(request["messages"]) == 0
        assert _cache_points(request) == 2  # Bedrock allows at most four


@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_cached_prefix_is_identical_across_turns(model_id):
    """A cache read needs a byte-identical prefix: tools, then system."""
    requests, _ = _run_designer(model_id)
    prefixes = {json.dumps([r["toolConfig"]["tools"], r["system"]]) for r in requests}
    assert len(prefixes) == 1


def test_model_adds_up_token_usage_per_call():
    _, model = _run_designer(SCHEMA_MODELS[0])
    assert model.usage == {
        "calls": 2,
        "inputTokens": 1850,
        "outputTokens": 30,
        "cacheReadInputTokens": 5000,
        "cacheWriteInputTokens": 5000,
    }


def test_design_trace_records_the_token_usage():
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    model = MagicMock()
    model.usage = {"calls": 3, "inputTokens": 10}
    designer = MagicMock(return_value=MagicMock(structured_output=_Design(tables=["a"])))
    runner = SchemaDesignRunner(
        target_type="dynamodb", output_model=_Design, model=model, designer_agent=designer
    )
    _, trace = runner.run("design")
    assert trace["token_usage"] == {"calls": 3, "inputTokens": 10}


def test_role_model_fails_at_import_if_strands_renames_an_overridden_hook(monkeypatch):
    """A renamed private hook would silently undo the override; import must fail."""
    import importlib

    from strands.models.bedrock import BedrockModel

    import src.shared.role_bedrock_model as module

    monkeypatch.delattr(BedrockModel, "_inject_cache_point")
    with pytest.raises(ImportError, match="_inject_cache_point"):
        importlib.reload(module)
    monkeypatch.undo()
    importlib.reload(module)


def test_no_cache_config_class_means_no_caching(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "strands.models.model":
            raise ImportError("old strands")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert "cache_config" not in bedrock_model_kwargs(Role.SCHEMA_DESIGN)


def test_atx_image_bounds_strands():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[3] / "src/atx_orchestrator/requirements.txt"
    ).read_text()
    assert "strands-agents>=1.56.0,<2" in text
