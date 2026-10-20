"""The DocumentDB decision trace is a versioned contract (ADR-008 Decision 1, #1188).

``analysis-documentdb/decision-trace.json`` was a plain dict: no ``contract_version``,
registered for no contract, never checked on read. It is now
``DocumentDBDecisionTrace``, on the shared ``DecisionTraceBase`` (#1187): the builder
builds it, the one writer writes through it, the store and the schema designer
check its version on read, and a trace saved before it still loads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.analysis.decision_trace import UNTYPED_TRACE_ENGINES
from src.agents.analysis.documentdb_analysis_agent import (
    analyze_for_documentdb_deterministic,
    apply_documentdb_llm_output,
)
from src.agents.analysis.handler import run_analysis
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.decision_trace_base import DecisionTraceBase
from src.contracts.documentdb_decision_trace import DocumentDBDecisionTrace
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis.documentdb_analysis_tools import (
    DenormalizationStrategy,
    LlmDocumentDBOutput,
)
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.documentdb_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"
TRACE_KEY = f"{DB}/{JOB}/analysis-documentdb/decision-trace.json"
CURRENT = current_version(DocumentDBDecisionTrace)
# The trace as the pipeline wrote it before #1188 (ecommerce fixture, deterministic).
LEGACY_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "documentdb_decision_trace_1_0.json"
)


def _built_trace() -> dict:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.DOCUMENTDB,
    )
    _, trace, _ = analyze_for_documentdb_deterministic(analysis_input)
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


def test_the_trace_is_registered_on_the_shared_base() -> None:
    assert contract_for_artifact(TRACE_KEY) is DocumentDBDecisionTrace
    assert issubclass(DocumentDBDecisionTrace, DecisionTraceBase)
    assert "documentdb" not in UNTYPED_TRACE_ENGINES


def test_the_builder_builds_the_model() -> None:
    built = _built_trace()
    assert built["contract_version"] == CURRENT
    assert DocumentDBDecisionTrace.model_validate(built).model_dump(mode="json") == built


def test_the_builder_writes_the_current_contract_version(tmp_path: Path) -> None:
    run_analysis(JOB, DB, "documentdb", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert DocumentDBDecisionTrace.model_validate(written).model_dump(mode="json") == written


def test_the_written_trace_is_the_pre_contract_trace_plus_the_version(tmp_path: Path) -> None:
    """Typing it changed nothing but the version field (same fixture as the legacy file)."""
    run_analysis(JOB, DB, "documentdb", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    legacy = json.loads(LEGACY_FIXTURE.read_text())
    assert written == {"contract_version": CURRENT, **legacy}
    assert list(written) == ["contract_version", *legacy]


def test_the_builder_refuses_an_unknown_strategy() -> None:
    trace = _built_trace()
    trace["denormalization_strategies"][0]["strategy"] = "shard"
    with pytest.raises(ValidationError, match="strategy"):
        DocumentDBDecisionTrace.model_validate(trace)


def test_an_unknown_relationship_type_is_refused() -> None:
    trace = _built_trace()
    trace["embedding_candidates"][0]["relationship_type"] = "one_to_few"
    with pytest.raises(ValidationError, match="relationship_type"):
        DocumentDBDecisionTrace.model_validate(trace)


def test_the_advisor_strategies_are_written_through_the_model(tmp_path: Path) -> None:
    """The LLM advisor's strategies replace the fallback ones; the optional fields it
    leaves null are not written, the ones it fills are."""
    trace = _built_trace()
    first = trace["denormalization_strategies"][0]
    advice = LlmDocumentDBOutput(
        denormalization_strategies=[
            DenormalizationStrategy(
                parent_table=first["parent_table"],
                child_table=first["child_table"],
                strategy="hybrid",
                rationale="r",
                trade_offs="t",
                hybrid_embedded_fields=["name"],
                risk_level="medium",
            )
        ]
    )
    _, updated = apply_documentdb_llm_output(None, advice, trace)  # type: ignore[arg-type]
    from src.agents.analysis.decision_trace import write_decision_trace

    write_decision_trace(LocalArtifactStore(base_dir=str(tmp_path)), TRACE_KEY, updated)
    (strategy,) = _raw_file(tmp_path, TRACE_KEY)["denormalization_strategies"]
    assert strategy["strategy"] == "hybrid" and strategy["hybrid_embedded_fields"] == ["name"]
    assert "estimated_document_size_kb" not in strategy


# --- reading ------------------------------------------------------------------


def test_a_saved_pre_1188_trace_still_loads(tmp_path: Path) -> None:
    legacy = json.loads(LEGACY_FIXTURE.read_text())
    assert "contract_version" not in legacy and legacy["trace_version"] == "1.0"
    _put(tmp_path, TRACE_KEY, legacy)
    read = LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)
    assert read == legacy
    loaded = load_contract(DocumentDBDecisionTrace, read, artifact=TRACE_KEY)
    assert loaded.contract_version == CURRENT
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


def test_the_store_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    _put(tmp_path, TRACE_KEY, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="DocumentDBDecisionTrace"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)


def test_the_store_reads_a_trace_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {**_built_trace(), "contract_version": "1.9", "x_added_later": True}
    _put(tmp_path, TRACE_KEY, newer)
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY) == newer


def _designer_paths(tmp_path: Path, trace: dict) -> AgentInputPaths:
    store = _store(tmp_path)
    run_analysis(JOB, DB, "documentdb", store, llm_mode="none")
    _put(tmp_path, TRACE_KEY, trace)
    return AgentInputPaths(
        collector=str(tmp_path / f"{DB}/{JOB}/collector/output.json"),
        analysis=str(tmp_path / f"{DB}/{JOB}/analysis-documentdb/analysis.json"),
        decision_trace=str(tmp_path / TRACE_KEY),
    )


def test_the_schema_designer_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="DocumentDBDecisionTrace"):
        load_agent_input(paths)


def test_the_schema_designer_reads_a_legacy_trace(tmp_path: Path) -> None:
    legacy = json.loads(LEGACY_FIXTURE.read_text())
    paths = _designer_paths(tmp_path, copy.deepcopy(legacy))
    assert load_agent_input(paths)["decision_trace"] == legacy


# --- the --finalize resume ---------------------------------------------------

_ADVICE: dict[str, list] = {"denormalization_strategies": []}


def _external_partial(tmp_path: Path) -> tuple[LocalArtifactStore, str]:
    from scripts import run_analysis as script

    store = _store(tmp_path)
    script.run_external(store, JOB, DB, "documentdb")
    store.write_json(f"{DB}/{JOB}/llm_responses/analysis_documentdb.json", _ADVICE)
    return store, f"{DB}/{JOB}/analysis-documentdb/partial.json"


def test_the_finalize_resume_writes_the_trace_through_its_model(tmp_path: Path) -> None:
    from scripts import run_analysis as script

    store, partial_key = _external_partial(tmp_path)
    partial = _raw_file(tmp_path, partial_key)
    del partial["decision_trace"]["contract_version"]  # a partial saved before #1188
    store.write_json(partial_key, partial)

    script.run_finalize(store, JOB, DB, "documentdb")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert written == {
        **partial["decision_trace"],
        "contract_version": CURRENT,
        "denormalization_strategies": [],
    }


def test_the_finalize_resume_refuses_a_partial_trace_at_another_major(tmp_path: Path) -> None:
    from scripts import run_analysis as script

    store, partial_key = _external_partial(tmp_path)
    partial = _raw_file(tmp_path, partial_key)
    partial["decision_trace"]["contract_version"] = "2.0"
    store.write_json(partial_key, partial)

    with pytest.raises(ContractVersionError, match="partial.json"):
        script.run_finalize(store, JOB, DB, "documentdb")
    assert not (tmp_path / TRACE_KEY).exists()
