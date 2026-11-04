# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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


def test_a_second_run_is_added_not_overwritten(tmp_path) -> None:
    """``assess()`` and ``run_assessment.py`` share ``write_usage`` (#1675): a later run
    of the job is one more entry under ``runs``, and the top level is their sum."""
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    for tokens in (5, 7):
        LEDGER.reset()
        with phase("schema_design"):
            record_usage(OPUS, {"inputTokens": tokens, "outputTokens": 1})
        model_usage.write_usage(store, "db", "job")
    LEDGER.reset()
    written = json.loads((tmp_path / "db" / "job" / "_logs" / "model_usage.json").read_text())
    assert [r["run"] for r in written["runs"]] == [1, 2]
    assert [r["totals"]["inputTokens"] for r in written["runs"]] == [5, 7]
    assert written["by_phase"]["schema_design"][OPUS]["inputTokens"] == 12
    assert written["by_model"][OPUS]["calls"] == 2
    assert written["totals"]["calls"] == 2


# --------------------------------------------------------------------------- phase timings (#145)


class _Clock:
    """A clock the test moves by hand, in seconds."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(model_usage, "_clock", fake)
    LEDGER.reset()  # the run starts at the fake clock's time
    return fake


def test_each_phase_records_its_duration_and_engines(clock) -> None:
    """A multi-phase run: each phase's wall-clock seconds and the engines it processed,
    in run order, next to the run's own wall-clock time."""
    LEDGER.start_timing("analysis")
    LEDGER.record_engines(["dynamodb", "aurora_mysql"])
    clock.now += 2.5
    LEDGER.start_timing("schema_design")  # closes analysis
    LEDGER.record_engines(["dynamodb"])
    LEDGER.record_engines(["dynamodb", "elasticache"])  # a redesign: no duplicate
    with phase("schema_design"):
        record_usage(OPUS, {"inputTokens": 5})
    clock.now += 60.0
    LEDGER.stop_timing()
    clock.now += 1.0  # after the last phase: run time, no phase time
    snapshot = LEDGER.snapshot()
    assert snapshot["phases"] == {
        "analysis": {"seconds": 2.5, "engines": ["dynamodb", "aurora_mysql"]},
        "schema_design": {"seconds": 60.0, "engines": ["dynamodb", "elasticache"]},
    }
    assert list(snapshot["phases"]) == ["analysis", "schema_design"]
    assert snapshot["wall_clock_seconds"] == 63.5


def test_a_phase_run_twice_adds_up_and_an_open_phase_counts_so_far(clock) -> None:
    LEDGER.start_timing("reality_check")
    clock.now += 1.0
    LEDGER.start_timing("engine_choices")
    clock.now += 0.5
    LEDGER.start_timing("reality_check")
    clock.now += 2.0  # still open when the snapshot is taken
    phases = LEDGER.snapshot()["phases"]
    assert phases["reality_check"] == {"seconds": 3.0, "engines": []}
    assert phases["engine_choices"] == {"seconds": 0.5, "engines": []}


def test_a_new_run_starts_with_no_timings(clock) -> None:
    LEDGER.start_timing("analysis")
    clock.now += 4.0
    LEDGER.reset()
    clock.now += 1.0
    snapshot = LEDGER.snapshot()
    assert snapshot["phases"] == {}
    assert snapshot["wall_clock_seconds"] == 1.0


def test_the_record_keeps_each_runs_timings_and_sums_them(tmp_path, clock) -> None:
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    for seconds, engines in ((10.0, ["dynamodb"]), (5.0, ["dynamodb", "opensearch"])):
        LEDGER.reset()
        LEDGER.start_timing("schema_design")
        LEDGER.record_engines(engines)
        with phase("schema_design"):
            record_usage(OPUS, {"inputTokens": 1})
        clock.now += seconds
        LEDGER.stop_timing()
        model_usage.write_usage(store, "db", "job")
    written = json.loads((tmp_path / "db" / "job" / "_logs" / "model_usage.json").read_text())
    assert [r["phases"]["schema_design"]["seconds"] for r in written["runs"]] == [10.0, 5.0]
    assert [r["wall_clock_seconds"] for r in written["runs"]] == [10.0, 5.0]
    assert written["phases"] == {
        "schema_design": {"seconds": 15.0, "engines": ["dynamodb", "opensearch"]}
    }
    assert written["wall_clock_seconds"] == 15.0


def test_an_old_record_without_timings_still_loads_and_sums(tmp_path, clock) -> None:
    """A record written before #145 has no ``phases`` or ``wall_clock_seconds``: its runs
    are kept as they are, add no time, and the new run's timings are added."""
    from src.storage.local_store import LocalArtifactStore

    old_run = {
        "run": 1,
        "totals": {**dict.fromkeys(model_usage.FIELDS, 0), "calls": 1, "inputTokens": 5},
        "by_model": {OPUS: {"calls": 1, "inputTokens": 5}},
        "by_phase": {"schema_design": {OPUS: {"calls": 1, "inputTokens": 5}}},
    }
    old = {
        "totals": old_run["totals"],
        "by_model": old_run["by_model"],
        "by_phase": old_run["by_phase"],
        "runs": [old_run],
    }
    path = tmp_path / "db" / "job" / "_logs" / "model_usage.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old))
    store = LocalArtifactStore(base_dir=str(tmp_path))
    LEDGER.start_timing("synthesis")
    LEDGER.record_engines(["dynamodb"])
    with phase("synthesis"):
        record_usage(SONNET, {"inputTokens": 2})
    clock.now += 3.0
    LEDGER.stop_timing()
    model_usage.write_usage(store, "db", "job")
    written = json.loads(path.read_text())
    assert written["runs"][0] == old_run
    assert written["runs"][1]["phases"] == {"synthesis": {"seconds": 3.0, "engines": ["dynamodb"]}}
    assert written["phases"] == {"synthesis": {"seconds": 3.0, "engines": ["dynamodb"]}}
    assert written["wall_clock_seconds"] == 3.0
    assert written["totals"]["calls"] == 2
    # usage_cost.py still prices both runs.
    from scripts import usage_cost

    cost = usage_cost.compute(written, usage_cost.load_prices())
    assert [r["run"] for r in cost["runs"]] == [1, 2]
