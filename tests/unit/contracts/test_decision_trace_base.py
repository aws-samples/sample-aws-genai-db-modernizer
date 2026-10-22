"""Every analysis engine's decision trace is a contract (ADR-008 Decision 1, #1187, #1190).

Only the DynamoDB trace was a versioned contract; the other engines' traces were
plain dicts that any change passed. The shared sections now live in
``DecisionTraceBase``, ``write_decision_trace`` refuses a trace that has no
registered contract, and every analysis engine's trace is registered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.analysis import decision_trace as writer
from src.agents.analysis.decision_trace import (
    UntypedDecisionTraceError,
    trace_engine,
    write_decision_trace,
)
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.contracts import decision_trace_base as base
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.dynamodb_decision_trace import DynamoDBDecisionTrace
from src.shared.catalog import engine_ids
from src.shared.contract_versioning import contract_for_artifact, is_internal_artifact
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"

# The engines whose trace is a registered contract: every analysis engine. Dropping a
# registration, or adding an engine without one, means editing this set, which a
# reviewer sees.
TYPED_ENGINES = frozenset(
    {"aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb", "elasticache", "opensearch"}
)

# Trace contracts that do not extend DecisionTraceBase, each with its reason. Every
# other registered trace contract must extend it.
OFF_BASE_TRACE_CONTRACTS: dict[str, str] = {
    "elasticache": (
        "the base's core sections are required and a saved 1.0 trace has none of them; "
        "the shared sections are optional fields of its own since 1.1 (#1276)"
    ),
}


def _key(engine: str) -> str:
    return f"{DB}/{JOB}/analysis-{engine}/decision-trace.json"


def _analysis_engines() -> frozenset[str]:
    """The engines that run an analysis and write a decision trace."""
    return engine_ids(status="active")


def test_every_analysis_trace_is_a_registered_contract() -> None:
    problems = [
        f"{engine}: no contract registered in _ARTIFACT_CONTRACTS"
        for engine in sorted(_analysis_engines())
        if contract_for_artifact(_key(engine)) is None
    ]
    assert not problems, "\n".join(problems)


def test_the_typed_engines_are_exactly_the_registered_traces() -> None:
    registered = {e for e in _analysis_engines() if contract_for_artifact(_key(e)) is not None}
    assert registered == TYPED_ENGINES
    assert TYPED_ENGINES == _analysis_engines()


def test_no_analysis_trace_is_a_declared_internal_artifact() -> None:
    """A typed trace is a contract (#1192), so none is listed as internal."""
    assert not {e for e in _analysis_engines() if is_internal_artifact(_key(e))}


def test_every_trace_contract_extends_the_base_unless_exempt() -> None:
    problems = []
    for engine in sorted(TYPED_ENGINES):
        model = contract_for_artifact(_key(engine))
        assert model is not None
        on_base = issubclass(model, base.DecisionTraceBase)
        if not on_base and engine not in OFF_BASE_TRACE_CONTRACTS:
            problems.append(f"{engine}: {model.__name__} does not extend DecisionTraceBase")
        if on_base and engine in OFF_BASE_TRACE_CONTRACTS:
            problems.append(f"{engine}: extends the base, remove it from OFF_BASE_TRACE_CONTRACTS")
    stale = sorted(set(OFF_BASE_TRACE_CONTRACTS) - TYPED_ENGINES)
    problems += [f"{engine}: exempt but not a typed trace" for engine in stale]
    problems += [
        f"{e}: exemption has no reason" for e, r in OFF_BASE_TRACE_CONTRACTS.items() if not r
    ]
    assert not problems, "\n".join(problems)


def test_the_dynamodb_trace_extends_the_shared_base() -> None:
    assert contract_for_artifact(_key("dynamodb")) is DynamoDBDecisionTrace
    assert issubclass(DynamoDBDecisionTrace, base.DecisionTraceBase)
    core = list(base.DecisionTraceBase.model_fields)
    assert list(DynamoDBDecisionTrace.model_fields)[: len(core)] == core


def test_the_base_is_not_a_contract_of_its_own() -> None:
    assert base.DecisionTraceBase.model_fields["contract_version"].is_required()


def _built_dynamodb_trace() -> dict:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.DYNAMODB,
    )
    _, trace, _ = analyze_for_dynamodb_deterministic(analysis_input)
    return trace


def _key_paths(value: object, prefix: str = "") -> list[str]:
    """Every key path in written order, list items collapsed to ``[]``."""
    paths: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            paths.append(f"{prefix}{k}")
            paths += _key_paths(v, f"{prefix}{k}.")
    elif isinstance(value, list):
        seen: list[str] = []
        for item in value:
            for p in _key_paths(item, f"{prefix}[]."):
                if p not in seen:
                    seen.append(p)
        paths += seen
    return paths


def test_the_dynamodb_trace_keeps_its_key_order(tmp_path: Path) -> None:
    """The shared sections keep the order the DynamoDB trace wrote before the base."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_decision_trace(store, _key("dynamodb"), _built_dynamodb_trace())
    written = json.loads((tmp_path / _key("dynamodb")).read_text())
    paths = _key_paths(written)
    assert list(written)[:8] == [
        "contract_version",
        "trace_version",
        "agent",
        "summary",
        "query_matches",
        "pattern_summaries",
        "recommendation_derivations",
        "table_groups",
    ]
    shared = [p for p in paths if p.startswith(("query_matches.", "recommendation_derivations."))]
    assert shared[:4] == [
        "query_matches.[].query_id",
        "query_matches.[].query_text_preview",
        "query_matches.[].matched_patterns",
        "query_matches.[].matched_anti_patterns",
    ]
    derivation = [p for p in shared if p.count(".") == 2 and p.startswith("recommendation")]
    assert derivation == [
        "recommendation_derivations.[].table_id",
        "recommendation_derivations.[].segments_contributing",
        "recommendation_derivations.[].score_breakdown",
        "recommendation_derivations.[].weights_used",
        "recommendation_derivations.[].weighted_confidence",
    ]
    assert list(written["llm_advisor"]) == [
        "status",
        "duration_seconds",
        "attempts",
        "aggregate_key_designs",
        "denormalization_strategies",
    ]


def test_a_dynamodb_derivation_still_requires_its_segments() -> None:
    trace = _built_dynamodb_trace()
    del trace["recommendation_derivations"][0]["segments_contributing"]
    with pytest.raises(ValueError, match="segments_contributing"):
        DynamoDBDecisionTrace.model_validate(trace)


def test_a_shared_derivation_without_segments_writes_none() -> None:
    derivation = base.RecommendationDerivation.model_validate(
        {
            "table_id": "t",
            "score_breakdown": {"pattern_match": 1, "complexity": 2, "performance": 3, "cost": 4},
            "weights_used": {"pattern_match": 1.0},
            "weighted_confidence": 5,
        }
    )
    assert "segments_contributing" not in derivation.model_dump()


def test_a_trace_with_no_registered_contract_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping an engine's registration does not turn its trace back into a plain dict."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    monkeypatch.setattr(writer, "contract_for_artifact", lambda key: None)
    with pytest.raises(UntypedDecisionTraceError, match="aurora_mysql"):
        write_decision_trace(store, _key("aurora_mysql"), {"agent": "x"})
    assert not (tmp_path / _key("aurora_mysql")).exists()


def test_an_unknown_engine_trace_is_refused(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(UntypedDecisionTraceError, match="neptune"):
        write_decision_trace(store, _key("neptune"), {"agent": "x"})
    assert not (tmp_path / _key("neptune")).exists()


def test_a_key_that_is_not_a_trace_path_is_refused(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(UntypedDecisionTraceError, match="not an analysis decision-trace path"):
        write_decision_trace(store, f"{DB}/{JOB}/analysis-dynamodb/analysis.json", {})
    assert trace_engine("analysis-documentdb/decision-trace.json") == "documentdb"
    assert trace_engine("x/analysis-documentdb/decision-trace.json.bak") is None
