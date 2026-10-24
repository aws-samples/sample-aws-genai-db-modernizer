"""Model token usage by phase and model id (#1494). No test calls a model: the Strands
stream and the boto3 responses are fakes that carry a ``usage`` block."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from src.shared import model_usage
from src.shared.model_usage import LEDGER, phase, record_invoke_model_usage, record_usage
from src.shared.models import Role, build_bedrock_model

OPUS = "us.anthropic.claude-opus-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def _clean_ledger():
    LEDGER.reset()
    yield
    LEDGER.reset()


def test_calls_add_up_per_phase_and_model() -> None:
    with phase("schema_design"):
        record_usage(OPUS, {"inputTokens": 100, "outputTokens": 10, "cacheWriteInputTokens": 50})
        record_usage(OPUS, {"inputTokens": 20, "outputTokens": 5, "cacheReadInputTokens": 50})
    with phase("synthesis"):
        record_usage(SONNET, {"inputTokens": 7, "outputTokens": 3})
    record = LEDGER.snapshot()
    assert record["by_phase"]["schema_design"][OPUS] == {
        "calls": 2,
        "inputTokens": 120,
        "outputTokens": 15,
        "cacheReadInputTokens": 50,
        "cacheWriteInputTokens": 50,
    }
    assert record["by_phase"]["synthesis"][SONNET]["calls"] == 1
    assert record["by_model"][OPUS]["inputTokens"] == 120
    assert record["totals"] == {
        "calls": 3,
        "inputTokens": 127,
        "outputTokens": 18,
        "cacheReadInputTokens": 50,
        "cacheWriteInputTokens": 50,
    }


def test_a_call_outside_a_phase_is_kept_and_the_phase_is_restored() -> None:
    with phase("a"):
        with phase("b"):
            record_usage(OPUS, {"inputTokens": 1})
        record_usage(OPUS, {"inputTokens": 2})
    record_usage(OPUS, {"inputTokens": 4})
    by_phase = LEDGER.snapshot()["by_phase"]
    assert {p: m[OPUS]["inputTokens"] for p, m in by_phase.items()} == {
        "a": 2,
        "b": 1,
        model_usage.UNKNOWN_PHASE: 4,
    }


def test_a_missing_usage_block_counts_nothing() -> None:
    record_usage(OPUS, None)
    assert LEDGER.snapshot()["totals"]["calls"] == 0


def test_invoke_model_usage_uses_the_anthropic_names() -> None:
    record_invoke_model_usage(
        "haiku",
        {"usage": {"input_tokens": 9, "output_tokens": 2, "cache_creation_input_tokens": 4}},
    )
    assert LEDGER.snapshot()["by_model"]["haiku"] == {
        "calls": 1,
        "inputTokens": 9,
        "outputTokens": 2,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 4,
    }


def test_the_model_wrapper_reports_each_streamed_call() -> None:
    """RoleBedrockModel is the one place Strands calls land: its stream's metadata
    event feeds the ledger under the current phase and the model's own id."""
    from strands.models.bedrock import BedrockModel

    usage = {
        "inputTokens": 11,
        "outputTokens": 3,
        "totalTokens": 14,
        "cacheReadInputTokens": 5,
        "cacheWriteInputTokens": 2,
    }

    async def fake_stream(self, *args: Any, **kwargs: Any):
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": usage}}

    model = build_bedrock_model(Role.SUMMARY, region_name="us-east-1")

    async def drain() -> None:
        async for _ in model.stream([], None, None):
            pass

    with patch.object(BedrockModel, "stream", fake_stream), phase("synthesis"):
        asyncio.run(drain())
        asyncio.run(drain())
    row = LEDGER.snapshot()["by_phase"]["synthesis"][model.config["model_id"]]
    assert row == {
        "calls": 2,
        "inputTokens": 22,
        "outputTokens": 6,
        "cacheReadInputTokens": 10,
        "cacheWriteInputTokens": 4,
    }


def test_the_direct_converse_call_reports_its_usage() -> None:
    from unittest.mock import MagicMock

    from src.agents.referee import consolidation_validator as cv

    client = MagicMock()
    client.converse.return_value = {
        "stopReason": "end_turn",
        "output": {"message": {"content": []}},
        "usage": {"inputTokens": 30, "outputTokens": 4},
    }
    with patch("boto3.client", return_value=client), phase("reality_check"):
        with pytest.raises(cv.ValidationFailed):  # an empty answer; the call was still paid for
            cv._try_boto3_validator("aurora_mysql", "dynamodb", [])
    row = LEDGER.snapshot()["by_phase"]["reality_check"]
    assert next(iter(row.values()))["inputTokens"] == 30


def test_the_free_text_check_reports_its_usage() -> None:
    import io
    from unittest.mock import MagicMock

    from src.agents import answer_validation as av
    from src.contracts.agent_interaction_models import AgentQuestion

    body = {
        "content": [{"type": "text", "text": "yes"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    client = MagicMock()
    client.invoke_model.return_value = {"body": io.BytesIO(json.dumps(body).encode())}
    question = MagicMock(spec=AgentQuestion)
    question.question = "How many rows?"
    with patch("boto3.client", return_value=client), phase("analysis"):
        av._bedrock_validate(question, "about ten")
    row = LEDGER.snapshot()["by_phase"]["analysis"]
    assert next(iter(row.values()))["inputTokens"] == 40


def test_write_usage_stores_the_record_and_skips_a_run_with_no_call(tmp_path) -> None:
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    assert model_usage.write_usage(store, "db", "job") is None
    assert not (tmp_path / "db" / "job" / "_logs").exists()
    with phase("schema_design"):
        record_usage(OPUS, {"inputTokens": 5, "outputTokens": 1})
    model_usage.write_usage(store, "db", "job")
    written = json.loads((tmp_path / "db" / "job" / "_logs" / "model_usage.json").read_text())
    assert written["by_phase"]["schema_design"][OPUS]["inputTokens"] == 5
    assert written["totals"]["calls"] == 1
