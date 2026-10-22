"""The Aurora PostgreSQL and MySQL decision traces are versioned contracts (ADR-008 Decision 1, #1190).

``analysis-aurora_postgresql/decision-trace.json`` and ``analysis-aurora_mysql/decision-trace.json``
were plain dicts: no ``contract_version``, registered for no contract, never checked on
read. Both are now ``AuroraDecisionTrace``, on the shared ``DecisionTraceBase`` (#1187):
the builders build it, the one writer writes through it, the store and the schema
designers check its version on read, and a trace saved before it still loads.
"""

from __future__ import annotations

import copy
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
from src.agents.analysis.handler import run_analysis
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.aurora_decision_trace import AuroraDecisionTrace
from src.contracts.decision_trace_base import DecisionTraceBase
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.schema import aurora_mysql_schema_agent, aurora_postgresql_schema_agent
from src.tools.schema.base_schema_agent import AgentInputPaths
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"
CURRENT = current_version(AuroraDecisionTrace)
REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "docs" / "examples"

# engine -> (target, analysis entry point, schema designer module, the query feature list)
ENGINES: dict[str, tuple[TargetDatabase, Callable, ModuleType, str]] = {
    "aurora_postgresql": (
        TargetDatabase.AURORA_POSTGRESQL,
        analyze_for_aurora_pg,
        aurora_postgresql_schema_agent,
        "pg_features",
    ),
    "aurora_mysql": (
        TargetDatabase.AURORA_MYSQL,
        analyze_for_aurora_mysql,
        aurora_mysql_schema_agent,
        "mysql_features",
    ),
}
engines = pytest.mark.parametrize("engine", sorted(ENGINES))


def _key(engine: str) -> str:
    return f"{DB}/{JOB}/analysis-{engine}/decision-trace.json"


def _legacy(engine: str) -> dict:
    """The trace as the pipeline wrote it before #1190 (ecommerce fixture)."""
    path = REPO / "tests" / "fixtures" / f"{engine}_decision_trace_pre_1190.json"
    data: dict = json.loads(path.read_text())
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


def _trace_of(engine: str, collector_output: dict, llm_mode: str = "none") -> dict:
    target, analyze, _, _ = ENGINES[engine]
    analysis_input = AnalysisInput(
        job_id=JOB, collector_output=collector_output, target_database=target
    )
    _, trace, _ = analyze(analysis_input, llm_mode=llm_mode)
    return dict(trace)


def _built_trace(engine: str) -> dict:
    return _trace_of(engine, get_ecommerce_collector_output())


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


# --- the model and the builders -----------------------------------------------


@engines
def test_the_trace_is_registered_on_the_shared_base(engine: str) -> None:
    assert contract_for_artifact(_key(engine)) is AuroraDecisionTrace
    assert issubclass(AuroraDecisionTrace, DecisionTraceBase)


@engines
def test_the_builder_builds_the_model(engine: str) -> None:
    built = _built_trace(engine)
    assert built["contract_version"] == CURRENT
    assert AuroraDecisionTrace.model_validate(built).model_dump(mode="json") == built


@engines
def test_the_builder_writes_the_current_contract_version(engine: str, tmp_path: Path) -> None:
    run_analysis(JOB, DB, engine, _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, _key(engine))
    assert written["contract_version"] == CURRENT
    assert AuroraDecisionTrace.model_validate(written).model_dump(mode="json") == written


@engines
def test_the_written_trace_is_the_pre_contract_trace_plus_the_version(
    engine: str, tmp_path: Path
) -> None:
    """Typing it changed nothing but the version field (same fixture as the legacy file)."""
    run_analysis(JOB, DB, engine, _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, _key(engine))
    assert written == {"contract_version": CURRENT, **_legacy(engine)}
    assert list(written) == ["contract_version", *_legacy(engine)]


@engines
def test_an_external_mode_trace_validates_and_says_it_awaits_the_model(
    engine: str, tmp_path: Path
) -> None:
    run_analysis(JOB, DB, engine, _store(tmp_path), llm_mode="external")
    written = _raw_file(tmp_path, _key(engine))
    assert written["llm_advisor"]["status"] == "awaiting_external"
    AuroraDecisionTrace.model_validate(written)


@engines
@pytest.mark.parametrize("sample", ["wordpress", "discourse", "trailgear"])
def test_a_trace_built_from_each_bundled_sample_validates_and_round_trips(
    engine: str, sample: str
) -> None:
    """Every trace the analysis writes on the bundled samples is accepted unchanged."""
    built = _trace_of(engine, _example_collections()[sample])
    assert built["contract_version"] == CURRENT
    legacy = {k: v for k, v in built.items() if k != "contract_version"}
    loaded = load_contract(AuroraDecisionTrace, legacy, artifact=_key(engine))
    assert loaded.model_dump(mode="json") == built


@engines
def test_each_engine_writes_its_own_feature_list_only(engine: str) -> None:
    feature_key = ENGINES[engine][3]
    other = ({"pg_features", "mysql_features"} - {feature_key}).pop()
    for match in _built_trace(engine)["query_matches"]:
        assert feature_key in match and other not in match


def test_the_engines_name_their_agent() -> None:
    assert _built_trace("aurora_postgresql")["agent"] == "aurora-pg-analysis-agent"
    assert _built_trace("aurora_mysql")["agent"] == "aurora-mysql-analysis-agent"


@engines
def test_a_query_match_with_both_feature_lists_is_refused(engine: str) -> None:
    trace = _legacy(engine)
    trace["query_matches"][0].update(pg_features=[], mysql_features=[])
    with pytest.raises(ValidationError, match="exactly one of pg_features and mysql_features"):
        AuroraDecisionTrace.model_validate(trace)


@engines
def test_a_query_match_with_neither_feature_list_is_refused(engine: str) -> None:
    trace = _legacy(engine)
    for key in ("pg_features", "mysql_features"):
        trace["query_matches"][0].pop(key, None)
    with pytest.raises(ValidationError, match="exactly one of pg_features and mysql_features"):
        AuroraDecisionTrace.model_validate(trace)


@engines
def test_an_unknown_migration_complexity_is_refused(engine: str) -> None:
    trace = _legacy(engine)
    trace["recommendation_derivations"][0]["migration_complexity"] = "EASY"
    with pytest.raises(ValidationError, match="migration_complexity"):
        AuroraDecisionTrace.model_validate(trace)


@engines
def test_a_trace_missing_a_section_is_refused(engine: str) -> None:
    trace = _legacy(engine)
    del trace["llm_advisor"]
    with pytest.raises(ValidationError, match="llm_advisor"):
        AuroraDecisionTrace.model_validate(trace)


# --- reading ------------------------------------------------------------------


@engines
def test_a_saved_pre_1190_trace_still_loads(engine: str, tmp_path: Path) -> None:
    legacy = _legacy(engine)
    assert "contract_version" not in legacy and legacy["trace_version"] == "1.0"
    _put(tmp_path, _key(engine), legacy)
    read = LocalArtifactStore(base_dir=str(tmp_path)).read_json(_key(engine))
    assert read == legacy
    loaded = load_contract(AuroraDecisionTrace, read, artifact=_key(engine))
    assert loaded.contract_version == CURRENT
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


@engines
def test_the_store_refuses_a_trace_at_another_major(engine: str, tmp_path: Path) -> None:
    _put(tmp_path, _key(engine), {**_built_trace(engine), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="AuroraDecisionTrace"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(_key(engine))


@engines
def test_the_store_reads_a_trace_at_a_newer_minor(engine: str, tmp_path: Path) -> None:
    newer = {**_built_trace(engine), "contract_version": "1.9", "x_added_later": True}
    _put(tmp_path, _key(engine), newer)
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(_key(engine)) == newer


def _designer_paths(engine: str, tmp_path: Path, trace: dict) -> AgentInputPaths:
    run_analysis(JOB, DB, engine, _store(tmp_path), llm_mode="none")
    _put(tmp_path, _key(engine), trace)
    return AgentInputPaths(
        collector=str(tmp_path / f"{DB}/{JOB}/collector/output.json"),
        analysis=str(tmp_path / f"{DB}/{JOB}/analysis-{engine}/analysis.json"),
        decision_trace=str(tmp_path / _key(engine)),
    )


@engines
def test_the_schema_designer_refuses_a_trace_at_another_major(engine: str, tmp_path: Path) -> None:
    paths = _designer_paths(engine, tmp_path, {**_built_trace(engine), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="AuroraDecisionTrace"):
        ENGINES[engine][2].load_agent_input(paths)


@engines
def test_the_schema_designer_reads_a_legacy_trace(engine: str, tmp_path: Path) -> None:
    paths = _designer_paths(engine, tmp_path, copy.deepcopy(_legacy(engine)))
    assert ENGINES[engine][2].load_agent_input(paths)["decision_trace"] == _legacy(engine)


@engines
def test_a_trace_from_before_the_locking_clause_still_loads(engine: str) -> None:
    """Traces saved before #799 have no ``locking_clause`` on a query match."""
    legacy = _legacy(engine)
    for match in legacy["query_matches"]:
        del match["locking_clause"]
    loaded = load_contract(AuroraDecisionTrace, legacy, artifact=_key(engine))
    assert all(m.locking_clause is None for m in loaded.query_matches)
