"""The OpenSearch decision trace is a versioned contract (ADR-008 Decision 1, #1189).

``analysis-opensearch/decision-trace.json`` was a plain dict: no ``contract_version``,
registered for no contract, never checked on read. It is now ``OpenSearchDecisionTrace``,
on the shared ``DecisionTraceBase`` (#1187): the builder builds it, the one writer
writes through it, the store and the schema designer check its version on read, and a
trace saved before it still loads.
"""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.analysis.handler import run_analysis
from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.decision_trace_base import DecisionTraceBase
from src.contracts.opensearch_decision_trace import OpenSearchDecisionTrace
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.opensearch_schema_agent import _project_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"
TRACE_KEY = f"{DB}/{JOB}/analysis-opensearch/decision-trace.json"
CURRENT = current_version(OpenSearchDecisionTrace)
REPO = Path(__file__).resolve().parents[3]
# The trace as the pipeline wrote it before #1189 (ecommerce fixture).
LEGACY_FIXTURE = REPO / "tests" / "fixtures" / "opensearch_decision_trace_pre_1189.json"
EXAMPLES = REPO / "docs" / "examples"


def _legacy() -> dict:
    data: dict = json.loads(LEGACY_FIXTURE.read_text())
    return data


def _ingest(raw_text: str, name: str) -> dict:
    """A raw collection as the collect phase turns it into collector output."""
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.collection_text import extract_json_object
    from src.tools.database.offline_parser import parse_offline_collection

    parsed = parse_offline_collection(json.loads(extract_json_object(raw_text, source=name)))
    return build_offline_collector_output(
        parsed, job_id=JOB, database_name=name, s3_key=f"uploads/{name}.json"
    )


def _example_collections() -> dict[str, dict]:
    """The bundled samples: the wordpress sample run's collector output, and the sample zips."""
    wordpress = json.loads((EXAMPLES / "wordpress/sample-run/collector/output.json").read_text())
    samples = {"wordpress": wordpress}
    for name in ("discourse", "trailgear"):
        with zipfile.ZipFile(EXAMPLES / name / f"{name}.zip") as archive:
            (member,) = [n for n in archive.namelist() if n.endswith(".json")]
            samples[name] = _ingest(archive.read(member).decode("utf-8"), name)
    return samples


def _trace_of(collector_output: dict) -> dict:
    analysis_input = AnalysisInput(
        job_id=JOB, collector_output=collector_output, target_database=TargetDatabase.OPENSEARCH
    )
    _, trace, _ = analyze_for_opensearch(analysis_input)
    return trace


def _built_trace() -> dict:
    return _trace_of(get_ecommerce_collector_output())


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
    assert contract_for_artifact(TRACE_KEY) is OpenSearchDecisionTrace
    assert issubclass(OpenSearchDecisionTrace, DecisionTraceBase)


def test_the_builder_builds_the_model() -> None:
    built = _built_trace()
    assert built["contract_version"] == CURRENT
    assert OpenSearchDecisionTrace.model_validate(built).model_dump(mode="json") == built


def test_the_builder_writes_the_current_contract_version(tmp_path: Path) -> None:
    run_analysis(JOB, DB, "opensearch", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert OpenSearchDecisionTrace.model_validate(written).model_dump(mode="json") == written


def test_the_written_trace_is_the_pre_contract_trace_plus_the_version(tmp_path: Path) -> None:
    """Typing it changed nothing but the version field (same fixture as the legacy file)."""
    run_analysis(JOB, DB, "opensearch", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written == {"contract_version": CURRENT, **_legacy()}


@pytest.mark.parametrize("sample", ["wordpress", "discourse", "trailgear"])
def test_a_trace_built_from_each_bundled_sample_validates_and_round_trips(sample: str) -> None:
    """Every trace the analysis writes on the bundled samples is accepted unchanged."""
    built = _trace_of(_example_collections()[sample])
    assert built["contract_version"] == CURRENT
    legacy = {k: v for k, v in built.items() if k != "contract_version"}
    loaded = load_contract(OpenSearchDecisionTrace, legacy, artifact=TRACE_KEY)
    assert loaded.model_dump(mode="json") == built


def test_the_samples_cover_every_workload_type() -> None:
    """Guards the sample test above: a search and a time-series table are both classified."""
    seen = {
        c["workload_type"]
        for collection in _example_collections().values()
        for c in _trace_of(collection)["workload_classifications"]
    }
    assert seen >= {"SEARCH", "NOT_SUITABLE"}


def test_an_unknown_workload_type_is_refused() -> None:
    trace = _legacy()
    trace["workload_classifications"][0]["workload_type"] = "GRAPH"
    with pytest.raises(ValidationError, match="workload_type"):
        OpenSearchDecisionTrace.model_validate(trace)


def test_an_unknown_final_recommendation_is_refused() -> None:
    trace = _legacy()
    trace["recommendation_derivations"][0]["final_recommendation"] = "MAYBE"
    with pytest.raises(ValidationError, match="final_recommendation"):
        OpenSearchDecisionTrace.model_validate(trace)


def test_a_trace_missing_a_section_is_refused() -> None:
    trace = _legacy()
    del trace["workload_classifications"]
    with pytest.raises(ValidationError, match="workload_classifications"):
        OpenSearchDecisionTrace.model_validate(trace)


# --- reading ------------------------------------------------------------------


def test_a_saved_pre_1189_trace_still_loads(tmp_path: Path) -> None:
    legacy = _legacy()
    assert "contract_version" not in legacy and legacy["trace_version"] == "1.0"
    _put(tmp_path, TRACE_KEY, legacy)
    read = LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)
    assert read == legacy
    loaded = load_contract(OpenSearchDecisionTrace, read, artifact=TRACE_KEY)
    assert loaded.contract_version == CURRENT
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


def test_a_trace_from_before_935_loads_and_is_not_given_a_null() -> None:
    legacy = _legacy()
    del legacy["timestamp_columns"]
    loaded = load_contract(OpenSearchDecisionTrace, legacy, artifact=TRACE_KEY)
    assert "timestamp_columns" not in loaded.model_dump(mode="json")


def test_the_store_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    _put(tmp_path, TRACE_KEY, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="OpenSearchDecisionTrace"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)


def test_the_store_reads_a_trace_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {**_built_trace(), "contract_version": "1.9", "x_added_later": True}
    _put(tmp_path, TRACE_KEY, newer)
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY) == newer


def _designer_paths(tmp_path: Path, trace: dict) -> AgentInputPaths:
    run_analysis(JOB, DB, "opensearch", _store(tmp_path), llm_mode="none")
    _put(tmp_path, TRACE_KEY, trace)
    return AgentInputPaths(
        collector=str(tmp_path / f"{DB}/{JOB}/collector/output.json"),
        analysis=str(tmp_path / f"{DB}/{JOB}/analysis-opensearch/analysis.json"),
        decision_trace=str(tmp_path / TRACE_KEY),
    )


def test_the_schema_designer_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, {**_built_trace(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="OpenSearchDecisionTrace"):
        _project_input(paths)


def test_the_schema_designer_reads_a_legacy_trace(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, copy.deepcopy(_legacy()))
    assert _project_input(paths)["decision_trace"] == _legacy()


# --- the schema design handler's read ------------------------------------------


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
def test_the_handler_reads_no_trace_as_none(engine: str, tmp_path: Path) -> None:
    """A trace that was never written is not an error: the designer runs without it."""
    from src.agents.schema_design.handler import _read_decision_trace

    store = LocalArtifactStore(base_dir=str(tmp_path))
    assert _read_decision_trace(store, DB, JOB, engine) is None


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
def test_the_handler_stops_on_a_trace_at_another_major(engine: str, tmp_path: Path) -> None:
    """A trace at another contract MAJOR is not 'not found': it stops the designer."""
    from src.agents.schema_design.handler import _read_decision_trace

    key = f"{DB}/{JOB}/analysis-{engine}/decision-trace.json"
    _put(tmp_path, key, {**_legacy(), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="DecisionTrace"):
        _read_decision_trace(LocalArtifactStore(base_dir=str(tmp_path)), DB, JOB, engine)


def test_the_handler_reads_a_legacy_and_a_current_trace(tmp_path: Path) -> None:
    from src.agents.schema_design.handler import _read_decision_trace

    store = LocalArtifactStore(base_dir=str(tmp_path))
    _put(tmp_path, TRACE_KEY, _legacy())
    assert _read_decision_trace(store, DB, JOB, "opensearch") == _legacy()
    _put(tmp_path, TRACE_KEY, _built_trace())
    assert _read_decision_trace(store, DB, JOB, "opensearch")["contract_version"] == CURRENT


def test_the_scoped_trace_given_to_the_designer_carries_the_contract_version() -> None:
    """The OpenSearch designer input gains a ``contract_version`` key (#1189)."""
    from src.agents.schema_design.group_trace import scope_decision_trace

    scoped = scope_decision_trace(_built_trace(), get_ecommerce_collector_output())
    assert scoped["contract_version"] == CURRENT
