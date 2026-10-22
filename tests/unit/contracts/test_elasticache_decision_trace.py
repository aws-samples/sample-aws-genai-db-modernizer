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
from src.tools.analysis.scoring import compute_confidence
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


SHARED_SECTIONS = [
    "trace_version",
    "summary",
    "query_matches",
    "pattern_summaries",
    "recommendation_derivations",
]


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


def test_the_written_trace_keeps_the_pre_contract_keys_and_adds_the_shared_sections(
    tmp_path: Path,
) -> None:
    run_analysis(JOB, DB, "elasticache", _store(tmp_path), llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    legacy = _legacy()
    assert {k: written[k] for k in legacy} == legacy
    assert written["contract_version"] == CURRENT
    assert list(written)[: 1 + len(legacy)] == ["contract_version", *legacy]
    assert list(written)[1 + len(legacy) :] == SHARED_SECTIONS


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
    # A 1.0 trace has none of the shared sections, and is not given nulls for them.
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


# --- the shared sections (#1276) ----------------------------------------------


def _analysis() -> tuple:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.ELASTICACHE,
    )
    output, trace, _ = analyze_for_elasticache(analysis_input)
    return output, trace, analysis_input.collector_output


def test_the_agent_writes_the_shared_sections() -> None:
    _, trace, _ = _analysis()
    for section in SHARED_SECTIONS:
        assert section in trace, section
    assert trace["trace_version"] == "1.0"


def test_the_summary_counts_agree_with_the_run_counts_and_the_matches() -> None:
    _, trace, collector = _analysis()
    summary = trace["summary"]
    queries = collector["queries"]["query_patterns"]
    assert summary["queries_analyzed"] == len(queries) == len(trace["query_matches"])
    assert summary["patterns_detected"] == trace["patterns_detected"]
    assert summary["anti_patterns_detected"] == trace["anti_patterns_detected"]
    matched = [
        m for m in trace["query_matches"] if m["matched_patterns"] or m["matched_anti_patterns"]
    ]
    assert summary["queries_matched"] == len(matched)
    assert summary["queries_matched"] + summary["queries_unmatched"] == len(queries)


def test_the_matches_summaries_and_derivations_come_from_the_analysis() -> None:
    output, trace, _ = _analysis()
    detected = output.workload_analysis.patterns_detected
    assert [p["pattern_id"] for p in trace["pattern_summaries"]] == [p.pattern_id for p in detected]
    for summary, pattern in zip(trace["pattern_summaries"], detected, strict=True):
        assert summary["queries_matched_count"] == len(pattern.query_ids or [])
        assert summary["tables_involved"] == (pattern.table_ids or [])
    # every pattern a query is said to match was detected
    detected_ids = {p.pattern_id for p in detected}
    anti_ids = {a.anti_pattern_id for a in output.workload_analysis.anti_patterns_detected or []}
    for match in trace["query_matches"]:
        assert set(match["matched_patterns"]) <= detected_ids
        assert set(match["matched_anti_patterns"]) <= anti_ids
    recs = output.table_recommendations
    assert [d["table_id"] for d in trace["recommendation_derivations"]] == [
        r.table_id for r in recs
    ]
    for derivation, rec in zip(trace["recommendation_derivations"], recs, strict=True):
        assert derivation["weighted_confidence"] == rec.confidence_score
        assert (
            derivation["score_breakdown"]["pattern_match"]
            == rec.score_breakdown.pattern_match_score
        )
        assert derivation["segments_contributing"] == (rec.supporting_patterns or [])
        weights = derivation["weights_used"]
        assert set(weights) == {"pattern_match", "complexity", "performance", "cost"}
        assert sum(weights.values()) == pytest.approx(1.0)
        # the weights named are the ones that produced the confidence
        assert compute_confidence(rec.score_breakdown, weights) == derivation["weighted_confidence"]


def test_a_query_with_no_match_is_listed_with_empty_matches() -> None:
    _, trace, _ = _analysis()
    unmatched = [m for m in trace["query_matches"] if not m["matched_patterns"]]
    assert unmatched, "the fixture should have a query no pattern matched"
    assert len(trace["query_matches"]) == len(_analysis()[2]["queries"]["query_patterns"])


def test_a_workload_with_no_queries_and_no_tables_has_empty_sections() -> None:
    empty = AnalysisInput(
        job_id=JOB,
        collector_output={"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
        target_database=TargetDatabase.ELASTICACHE,
    )
    _, trace, _ = analyze_for_elasticache(empty)
    assert trace["query_matches"] == []
    assert trace["pattern_summaries"] == []
    assert trace["recommendation_derivations"] == []
    assert trace["summary"]["queries_analyzed"] == 0


@pytest.mark.passes_on_main
def test_a_trace_with_the_shared_sections_round_trips() -> None:
    built = _built_trace()
    assert ElastiCacheDecisionTrace.model_validate(built).model_dump(mode="json") == built


def test_a_section_the_contract_refuses_is_refused() -> None:
    bad = {**_built_trace(), "summary": {"queries_analyzed": "many"}}
    with pytest.raises(ValidationError, match="summary"):
        ElastiCacheDecisionTrace.model_validate(bad)


def test_the_schema_designer_reads_a_trace_with_the_shared_sections(tmp_path: Path) -> None:
    paths = _designer_paths(tmp_path, _built_trace())
    assert load_agent_input(paths)["decision_trace"]["summary"] == _built_trace()["summary"]


def test_the_weights_named_are_the_defaults_compute_confidence_applies() -> None:
    """``weights_used`` is not a copy that can drift: it is the weight set the analysis's
    own confidence (``compute_confidence`` with no weights) used."""
    from src.tools.analysis.scoring import DEFAULT_WEIGHTS

    output, trace, _ = _analysis()
    assert trace["recommendation_derivations"]
    for derivation, rec in zip(
        trace["recommendation_derivations"], output.table_recommendations, strict=True
    ):
        assert derivation["weights_used"] == dict(DEFAULT_WEIGHTS)
        assert compute_confidence(rec.score_breakdown) == derivation["weighted_confidence"]
