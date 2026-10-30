# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock prompt caching on the schema-design path (#626).

Schema design is the only role with ``prompt_cache``: every designer and PE
turn resends the same output-contract tool schema and skill text, and parallel
design groups share them too. These tests pin the request shape Bedrock
receives (cache points after the tool definitions and after the system prompt;
one more on the latest user turn for a multi-turn designer, #1675, none for a
one-shot call such as the PE reviewer), that no other role changes, and the
per-call token accounting the design trace records.

The Bedrock limits checked here are from the Amazon Bedrock prompt-caching guide
(docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html, "Supported
models, Regions, and explicit caching limits"): Claude Opus 5.5 takes at most 4
cache checkpoints per request, in ``tools``, ``system`` and ``messages``, each
needing a 512-token prefix; a hit is looked up about 20 content blocks back from
a checkpoint.

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


# The default (Opus 5.5) and a 4.x override: both cache.
SCHEMA_MODELS = [models.ROLE_SPECS[Role.SCHEMA_DESIGN].default_model, MODEL_IDS["opus-4.6"]]


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
        ("anthropic.claude-opus-4-7", 4096),  # the Bedrock table; was 2048 before #1675
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


def _designer_turns() -> list[dict]:
    """A load-input tool turn, then the structured output."""
    return [
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


def _run_designer(
    model_id: str, *, conversation: bool = False, responses: list[dict] | None = None
) -> tuple[list[dict], Any]:
    """A designer-shaped agent on the role's model (``conversation``: the designer's
    conversation-cached model, as the six designers build it)."""
    from strands import Agent, tool

    @tool
    def load_agent_input() -> dict:
        """Load the projected input."""
        return {"tables": ["a", "b"]}

    model = build_bedrock_model(Role.SCHEMA_DESIGN, model_id=model_id)
    agent_model = model.with_conversation_cache() if conversation else model
    requests: list[dict] = []
    responses = _designer_turns() if responses is None else responses

    def fake_converse_stream(**request):
        requests.append(json.loads(json.dumps(request)))  # snapshot: Strands mutates in place
        return responses.pop(0) if responses else _stream([{"text": "done"}], "end_turn", {})

    model.client = MagicMock()
    model.client.converse_stream.side_effect = fake_converse_stream
    agent_model.client = model.client
    agent = Agent(
        model=agent_model,
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


@pytest.mark.passes_on_main  # a guard: main already behaves this way
@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_a_one_shot_model_caches_tools_and_system_only(model_id):
    """The role's own model (what the PE reviewer is given) never marks the messages."""
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


# ---------------------------------------------------------------------------
# The designer's conversation (#1675)
# ---------------------------------------------------------------------------


def _three_turns() -> list[dict]:
    """Load the input, send an output that fails validation, then a valid one."""
    load, valid = _designer_turns()
    invalid = _stream(
        [{"toolUse": ("_Design", {"tables": "not a list"})}],
        "tool_use",
        {"inputTokens": 40, "outputTokens": 15, "cacheReadInputTokens": 5900},
    )
    return [load, invalid, valid]


def _message_points(request: dict) -> list[tuple[int, int]]:
    """(message index, block index) of every cache point in the messages."""
    return [
        (m, b)
        for m, message in enumerate(request["messages"])
        for b, block in enumerate(message["content"])
        if "cachePoint" in block
    ]


def _without_points(messages: list[dict]) -> list[dict]:
    return [{**m, "content": [b for b in m["content"] if "cachePoint" not in b]} for m in messages]


@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_the_designer_caches_its_conversation(model_id):
    requests, _ = _run_designer(model_id, conversation=True, responses=_three_turns())
    assert len(requests) == 3, "the three-turn conversation was not exercised"
    for request in requests:
        # The stable prefix keeps its two points ...
        assert request["system"] == [{"text": SKILL_TEXT}, {"cachePoint": {"type": "default"}}]
        assert request["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
        # ... and the latest user turn carries one more, as its last block.
        points = _message_points(request)
        assert len(points) == 1
        msg, block = points[0]
        last_user = max(i for i, m in enumerate(request["messages"]) if m["role"] == "user")
        assert msg == last_user
        assert block == len(request["messages"][msg]["content"]) - 1
        # Bedrock's limit for Claude models: four cache checkpoints per request.
        assert _cache_points(request) == 3 <= 4


@pytest.mark.parametrize("model_id", SCHEMA_MODELS)
def test_each_designer_turn_can_read_the_previous_turns_entry(model_id):
    """The next turn resends the cached prefix byte for byte, and its own point is
    within the ~20 blocks Bedrock looks back from a checkpoint for a hit."""
    requests, _ = _run_designer(model_id, conversation=True, responses=_three_turns())
    for before, after in zip(requests, requests[1:], strict=False):
        msg, block = _message_points(before)[0]
        cached = _without_points(before["messages"][: msg + 1])
        resent = _without_points(after["messages"])
        assert resent[: msg + 1] == cached
        next_msg, next_block = _message_points(after)[0]
        between = sum(len(m["content"]) for m in resent[msg + 1 : next_msg]) + next_block
        assert between <= 20


@pytest.mark.parametrize(
    "skill",
    [
        "aurora_mysql-data-modeling.md",
        "aurora_postgresql-data-modeling.md",
        "documentdb-data-modeling.md",
        "dynamodb-data-modeling.md",
        "elasticache-data-modeling.md",
        "opensearch-index-modeling.md",
    ],
)
@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_every_designers_cached_prefix_meets_the_models_minimum(skill):
    """A checkpoint on a prefix under the minimum is silently not cached. The system
    point alone covers the skill text, already over the default model's minimum
    (at a generous 4 characters a token), so every later point is too."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[3] / "src/skills" / skill).read_text("utf-8")
    default = models.ROLE_SPECS[Role.SCHEMA_DESIGN].default_model
    assert len(text) // 4 >= min_cacheable_tokens(default)


def test_the_designers_model_shares_the_role_models_totals():
    model = build_bedrock_model(Role.SCHEMA_DESIGN)
    designer = model.with_conversation_cache()
    assert designer is not model and designer.cache_conversation and not model.cache_conversation
    assert designer.usage is model.usage
    assert designer.structured_output_reasks is model.structured_output_reasks
    assert designer.client is model.client and designer.config is model.config
    assert designer.with_conversation_cache() is designer


def test_a_designer_turn_and_a_pe_review_add_up_on_one_model():
    requests, model = _run_designer(SCHEMA_MODELS[0], conversation=True)
    assert model.usage["calls"] == 2 == len(requests)


def _pe_request(model: Any) -> dict:
    """The request the ElastiCache PE reviewer sends on ``model``."""
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.tools.schema import elasticache_schema_agent as agent

    requests: list[dict] = []

    def fake_converse_stream(**request):
        requests.append(json.loads(json.dumps(request)))
        return _stream(
            [{"toolUse": ("PEReviewResult", {"verdict": "APPROVED", "summary": "ok"})}],
            "tool_use",
            {"inputTokens": 100, "outputTokens": 10},
        )

    model.client = MagicMock()
    model.client.converse_stream.side_effect = fake_converse_stream
    fields: dict[str, Any] = {"key_designs": [], "access_patterns": []}
    design = ElastiCacheModelOutputContract.model_construct(**fields)
    review = agent._invoke_pe_reviewer(model, design, {"table_count": 1})
    assert review.verdict.value == "APPROVED"
    assert len(requests) == 1
    return requests[0]


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_the_pe_reviewer_puts_no_cache_point_on_its_input():
    request = _pe_request(build_bedrock_model(Role.SCHEMA_DESIGN))
    assert _message_points(request) == []
    assert _cache_points(request) == 2  # tools and system only


def test_the_runner_hands_the_pe_reviewer_the_one_shot_model():
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    model = build_bedrock_model(Role.SCHEMA_DESIGN)
    given: list[Any] = []

    def reviewer(m, design, summary, skill):
        given.append(m)
        return MagicMock(verdict="APPROVED", pe_notes=[], routing_flags=[])

    designer = MagicMock(return_value=MagicMock(structured_output=_Design(tables=["a"])))
    runner = SchemaDesignRunner(
        target_type="elasticache",
        output_model=_Design,
        model=model,
        designer_agent=designer,
        pe_reviewer_fn=reviewer,
    )
    runner.run("design")
    assert given == [model] and not given[0].cache_conversation


def _agent_models(module_path: str) -> dict[str, str]:
    """``{variable: source of its model= argument}`` for each ``Agent(...)`` assigned in a module."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if getattr(call.func, "id", None) != "Agent":
                continue
            target = node.targets[0]
            name = target.id if isinstance(target, ast.Name) else ast.unparse(target)
            model = next(k.value for k in call.keywords if k.arg == "model")
            found[name] = ast.unparse(model)
    return found


@pytest.mark.parametrize(
    "engine",
    [
        "aurora_mysql",
        "aurora_postgresql",
        "documentdb",
        "dynamodb",
        "elasticache",
        "opensearch",
    ],
)
def test_every_designer_caches_its_conversation_and_no_reviewer_does(engine):
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / f"src/tools/schema/{engine}_schema_agent.py"
    agents = _agent_models(str(path))
    assert agents == {"designer": "model.with_conversation_cache()", "pe_agent": "model"}


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
    import re
    from pathlib import Path

    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    root = Path(__file__).resolve().parents[3] / "src/atx_orchestrator"
    assert "strands-agents>=1.56.0,<2" in (root / "requirements.in").read_text()
    # The image installs the locked version, which must satisfy that bound (#1725).
    pin = re.search(r"^strands-agents==(\S+)$", (root / "requirements.txt").read_text(), re.M)
    assert pin and Version(pin[1]) in SpecifierSet(">=1.56.0,<2")
