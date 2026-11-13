# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis needs a design or a skip record for every engine it designs (#2154).

Maintainer decision on #2154: an engine whose design was skipped (the interactive skip
of ``run_schema_design.py --skip``, or a run with ``--llm-mode none``) stays allowed,
and the deliverables say it was not designed and why. An assigned engine with neither
a design nor a skip record is missing: synthesis fails, naming the engine and the
version. Before #2154 a missing ``schema-<engine>/v{N}/schema_output.json`` only logged
a warning, and the report read the engine as not designed with no reason.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import MissingSchemaDesigns, load_synthesis_data
from src.agents.referee.synthesis_handler import run_synthesis
from src.agents.schema_design import handler
from src.agents.schema_design.handler import run_schema_design
from src.contracts.phase_models import SCHEMA_DESIGN_SKIP_REASON_NO_LLM
from src.shared.design_records import skip_reason
from src.shared.wave_readiness import wave_readiness
from src.storage.local_store import LocalArtifactStore

DB = "wordpress"
JOB = "job-designs"
DESIGN = {"access_patterns": [], "validation_passed": True, "validation_failures": []}
REPORT = f"{DB}/{JOB}/synthesis/v1/report.json"
USER_SKIP = "Skipped at the user's request after its design failed"


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "opensearch"},
                {"agent_type": "documentdb"},
            ]
        },
    )
    s.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
    for engine in ("dynamodb", "opensearch", "documentdb"):
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    # documentdb was consolidated away: no in-scope query, so it gets no design.
    s.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "opensearch", "in_scope": True},
                {"query_id": "q3", "assigned_engine": "documentdb", "in_scope": False},
            ],
        },
    )
    return s


def _key(engine: str) -> str:
    return f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"


def _design(store: LocalArtifactStore, engine: str) -> None:
    store.write_json(_key(engine), DESIGN)


def _skip(
    store: LocalArtifactStore, engine: str, reason: str = USER_SKIP, **extra
) -> None:  # noqa: ANN003
    store.write_json(
        _key(engine),
        {"target_type": engine, "status": "skipped", "reason": reason, **extra},
    )


def test_an_engine_with_neither_a_design_nor_a_skip_record_fails(store) -> None:
    _design(store, "dynamodb")
    with pytest.raises(MissingSchemaDesigns) as raised:
        load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert str(raised.value) == (
        "Synthesis needs a schema design or a skip record for every engine of assignment "
        "v1: opensearch has neither. Run its schema design, or skip it, first."
    )


def test_no_design_and_no_skip_record_at_all_fails_too(store) -> None:
    with pytest.raises(MissingSchemaDesigns, match="dynamodb, opensearch have neither"):
        load_synthesis_data(store, JOB, DB, assignment_version=1)


def test_an_unreadable_design_is_missing(store) -> None:
    _design(store, "dynamodb")
    path = store.base_dir / _key("opensearch")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MissingSchemaDesigns, match="opensearch has neither"):
        load_synthesis_data(store, JOB, DB, assignment_version=1)


def test_a_skipped_engine_loads_with_its_reason(store) -> None:
    _design(store, "dynamodb")
    _skip(store, "opensearch")
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert data.engines["opensearch"].design_skip_reason == USER_SKIP
    assert data.engines["dynamodb"].design_skip_reason is None


def test_an_old_saved_skip_placeholder_loads(store) -> None:
    """A placeholder saved before #1023 has no contract_version or assignment_version."""
    _skip(store, "dynamodb", reason="No queries or tables assigned to this engine")
    _skip(store, "opensearch", reason=SCHEMA_DESIGN_SKIP_REASON_NO_LLM)
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert data.engines["opensearch"].design_skip_reason == SCHEMA_DESIGN_SKIP_REASON_NO_LLM


@pytest.mark.passes_on_main
def test_a_caller_that_stops_before_schema_design_never_asks(store) -> None:
    _design(store, "dynamodb")
    data = load_synthesis_data(store, JOB, DB, assignment_version=1, schema_design_expected=False)
    assert data.engines["dynamodb"].schema_design


def test_a_skipped_engine_is_shown_as_skipped_and_why(store) -> None:
    _design(store, "dynamodb")
    _skip(store, "opensearch")
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")
    report = store.read_json(REPORT)
    assert report["schema_designs"]["opensearch"] == {"status": "skipped", "reason": USER_SKIP}


def test_the_wave_line_says_why_a_skipped_engine_is_not_designed() -> None:
    [row] = wave_readiness(
        [{"wave": 1, "engines": ["dynamodb", "opensearch"]}],
        risks=[],
        designed_engines=[],
        skipped_by_engine={
            "dynamodb": SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
            "opensearch": USER_SKIP,
        },
    )
    assert row["readiness"] == "not_designed"
    assert row["readiness_line"] == (
        "Not designed: the DynamoDB design was skipped because this run used no model, and "
        "every schema designer needs one; the OpenSearch design was skipped at your request "
        "after it failed. Run the DynamoDB and OpenSearch schema design, then check the wave "
        "again."
    )


@pytest.mark.passes_on_main
def test_an_engine_with_no_record_keeps_the_not_designed_yet_line() -> None:
    [row] = wave_readiness([{"wave": 1, "engines": ["dynamodb"]}], risks=[], designed_engines=[])
    assert row["readiness_line"] == (
        "Not designed yet: run the DynamoDB schema design, then check the wave again."
    )


def test_llm_mode_none_synthesis_records_the_skip_and_passes(store) -> None:
    """A run with no model never ran schema design: synthesis records each engine's
    design as skipped for that reason, and the report says so."""
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")
    report = store.read_json(REPORT)
    for engine in ("dynamodb", "opensearch"):
        placeholder = store.read_json(_key(engine))
        assert (placeholder["status"], placeholder["reason"]) == (
            "skipped",
            SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
        )
        assert report["schema_designs"][engine]["reason"] == SCHEMA_DESIGN_SKIP_REASON_NO_LLM
    assert not store.exists(_key("documentdb"))


def test_llm_mode_none_synthesis_records_every_missing_engine(store) -> None:
    """A run without a model means nobody designs; every engine of the version with no
    record gets a skip record, even next to real designs the user wrote by hand."""
    _design(store, "dynamodb")
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")
    record = store.read_json(_key("opensearch"))
    assert (record["status"], record["reason"]) == ("skipped", SCHEMA_DESIGN_SKIP_REASON_NO_LLM)
    assert store.read_json(_key("dynamodb")) == DESIGN  # the real design is left alone


def test_a_bedrock_synthesis_still_fails_on_a_missing_design(store) -> None:
    """The deterministic path writes skips; the model-driven path never does, so a
    missing design still fails loudly on it."""
    _design(store, "dynamodb")
    with pytest.raises(MissingSchemaDesigns, match="opensearch has neither"):
        run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="external")
    assert not store.exists(_key("opensearch"))


def test_schema_design_without_a_model_writes_the_skip_record(store) -> None:
    run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="none")
    placeholder = store.read_json(_key("dynamodb"))
    assert (placeholder["status"], placeholder["reason"]) == (
        "skipped",
        SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
    )


def test_a_design_that_writes_its_request_replaces_the_skip_record(store, monkeypatch) -> None:
    """A run without a model, designed later with one: the external request on disk is
    work in progress, so the record goes and no check reads the engine as skipped."""
    monkeypatch.setattr(
        handler, "prepare_schema_design_input", lambda *a, **k: {"collector_output": {}}
    )
    run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="none")
    assert skip_reason(store.read_json(_key("dynamodb"))) is not None
    run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="external")
    assert not store.exists(_key("dynamodb"))


def test_only_a_design_that_writes_work_clears_the_skip_record(store, monkeypatch) -> None:
    """#2154 round 2: clearing the record at the start of a design that then writes
    nothing left the engine with neither, which is what the e2e shards hit. Only the
    paths that write design work to disk clear it: the external request and the split,
    never a Bedrock or injected design (its own output replaces the record)."""
    cleared: list[str] = []
    monkeypatch.setattr(
        handler,
        "clear_design_skip",
        lambda store, db, job, engine, version: cleared.append(engine),
    )
    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(
        handler, "prepare_schema_design_input", lambda *a, **k: {"collector_output": {}}
    )
    handler.run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="bedrock")
    handler.run_schema_design_with_injected(
        JOB, DB, "dynamodb", store, {"q1"}, assignment_version=1, llm_mode="bedrock"
    )
    assert cleared == []
    handler.run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="external")
    assert cleared == ["dynamodb"]


def test_a_real_design_replaces_the_skip_record_when_it_is_written(store) -> None:
    _skip(store, "dynamodb")
    _design(store, "dynamodb")
    assert store.read_json(_key("dynamodb")) == DESIGN
