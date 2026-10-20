"""The ElastiCache decision trace is a versioned contract (ADR-008 Decision 1, #1191).

``analysis-elasticache/decision-trace.json`` was a plain dict built inline in the
analysis agent: no ``contract_version``, registered for no contract, never checked
on read. It is now ``ElastiCacheDecisionTrace``, typed as it is (six keys plus the
version): the agent builds it, the one writer writes through it, the store and the
ElastiCache schema designer check its version on read, and a trace saved before it
still loads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.analysis.decision_trace import UNTYPED_TRACE_ENGINES
from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache
from src.agents.analysis.handler import run_analysis
from src.agents.schema_design.group_trace import scope_decision_trace
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.elasticache_decision_trace import ElastiCacheDecisionTrace
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.elasticache_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"
TRACE_KEY = f"{DB}/{JOB}/analysis-elasticache/decision-trace.json"
CURRENT = current_version(ElastiCacheDecisionTrace)
# The trace as the pipeline wrote it before #1191 (ecommerce fixture).
LEGACY_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "elasticache_decision_trace_pre_1191.json"
)


def _legacy() -> dict:
    data: dict = json.loads(LEGACY_FIXTURE.read_text())
    return data


def _built_trace() -> dict:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.ELASTICACHE,
    )
    _, trace, _ = analyze_for_elasticache(analysis_input)
    return trace


def _store(tmp_path: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", get_ecommerce_collector_output())
    return store


def _raw_file(tmp_path: Path, key: str) -> dict:
    data: dict = json.loads((tmp_path / key).read_text())
    return data


def _put(tmp_path: Path, key: str, data: dict) -> None:
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# --- the model and the builder ------------------------------------------------


def test_the_trace_is_registered_and_no_longer_pending() -> None:
    assert contract_for_artifact(TRACE_KEY) is ElastiCacheDecisionTrace
    assert "elasticache" not in UNTYPED_TRACE_ENGINES


def test_the_agent_builds_the_model() -> None:
    built = _built_trace()
    assert built["contract_version"] == CURRENT
    assert ElastiCacheDecisionTrace.model_validate(built).model_dump(mode="json") == built


def test_the_written_trace_is_the_pre_contract_trace_plus_the_version(tmp_path: Path) -> None:
    run_analysis(JOB, DB, "elasticache", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written == {"contract_version": CURRENT, **_legacy()}
    assert list(written) == ["contract_version", *_legacy()]


def test_a_trace_missing_a_count_is_refused() -> None:
    trace = _built_trace()
    del trace["tables_analyzed"]
    with pytest.raises(ValidationError, match="tables_analyzed"):
        ElastiCacheDecisionTrace.model_validate(trace)


# --- reading ------------------------------------------------------------------


def test_a_saved_pre_1191_trace_still_loads(tmp_path: Path) -> None:
    legacy = _legacy()
    assert "contract_version" not in legacy
    _put(tmp_path, TRACE_KEY, legacy)
    read = LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)
    assert read == legacy
    loaded = load_contract(ElastiCacheDecisionTrace, read, artifact=TRACE_KEY)
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


def test_a_trace_from_before_935_loads_and_is_not_given_a_null() -> None:
    legacy = _legacy()
    del legacy["timestamp_columns"]
    loaded = load_contract(ElastiCacheDecisionTrace, legacy, artifact=TRACE_KEY)
    assert "timestamp_columns" not in loaded.model_dump(mode="json")


def test_the_store_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    _put(tmp_path, TRACE_KEY, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="ElastiCacheDecisionTrace"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)


def test_the_store_reads_a_trace_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {**_built_trace(), "contract_version": "1.9", "x_added_later": True}
    _put(tmp_path, TRACE_KEY, newer)
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY) == newer


def _designer_paths(tmp_path: Path, trace: dict) -> AgentInputPaths:
    run_analysis(JOB, DB, "elasticache", _store(tmp_path), llm_mode="none")
    _put(tmp_path, TRACE_KEY, trace)
    return AgentInputPaths(
        collector=str(tmp_path / f"{DB}/{JOB}/collector/output.json"),
        analysis=str(tmp_path / f"{DB}/{JOB}/analysis-elasticache/analysis.json"),
        decision_trace=str(tmp_path / TRACE_KEY),
    )


def test_the_schema_designer_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="ElastiCacheDecisionTrace"):
        load_agent_input(paths)


def test_the_schema_designer_reads_a_legacy_trace(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, copy.deepcopy(_legacy()))
    assert load_agent_input(paths)["decision_trace"] == _legacy()


def test_the_trace_scoped_for_a_design_call_keeps_its_version() -> None:
    """The designer checks the scoped trace's version, so scoping keeps it."""
    scoped = scope_decision_trace(_built_trace(), get_ecommerce_collector_output())
    assert scoped["contract_version"] == CURRENT
