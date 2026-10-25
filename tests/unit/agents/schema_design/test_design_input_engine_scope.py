"""A design request names no engine outside the customer's engine scope (#1574, item 2).

The analysis carries catalog guidance that suggests other engines ("If it's a hot
lookup, ElastiCache is more appropriate"). A job scoped to DynamoDB and Aurora
PostgreSQL sent that text to the Aurora PostgreSQL designer. Every design path reads the
analysis through ``_design_analysis_output``, which now drops each sentence that names
an engine outside the scope (with no scope, the analysis is unchanged).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.referee.engine_scope import scope_filtered_analysis, scope_filtered_text
from src.agents.schema_design import handler
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis.aurora_common_pattern_catalog import (
    NO_RELATIONAL_NEED_ENGINE_HINT,
    NO_RELATIONAL_NEED_GUIDANCE,
    SINGLE_ACCESS_PATTERN_GUIDANCE,
)
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

SCOPE = ("aurora_postgresql", "dynamodb")
DB, JOB = "ecommerce", "job-1574"


def test_a_sentence_naming_an_engine_outside_the_scope_is_dropped() -> None:
    out = scope_filtered_text(NO_RELATIONAL_NEED_GUIDANCE, SCOPE)
    assert "ElastiCache" not in out
    assert out.startswith("Evaluate whether this table benefits")
    assert "DynamoDB offers better cost/performance." in out


def test_a_labelled_concern_keeps_its_label() -> None:
    out = scope_filtered_text(
        f"single-access-pattern-table: {SINGLE_ACCESS_PATTERN_GUIDANCE}", SCOPE
    )
    assert out.startswith("single-access-pattern-table: Aurora adds connection overhead")
    assert "ElastiCache" not in out


def test_text_inside_the_scope_or_with_no_scope_is_unchanged() -> None:
    text = "Migrate high-frequency PK lookups to DynamoDB for predictable latency."
    assert scope_filtered_text(text, SCOPE) == text
    assert scope_filtered_text(NO_RELATIONAL_NEED_GUIDANCE, None) == NO_RELATIONAL_NEED_GUIDANCE


def test_text_that_only_names_outside_engines_is_emptied() -> None:
    assert scope_filtered_text("Consider ElastiCache/Redis for session stores.", SCOPE) == ""


def _analysis() -> dict:
    return {
        "contract_version": "2.0",
        "table_recommendations": [
            {
                "table_id": "t1",
                "rationale": "Hot reads. OpenSearch would serve its text search.",
                "concerns": [
                    f"no-relational-need: {NO_RELATIONAL_NEED_GUIDANCE}",
                    "single-access-pattern-table: Consider ElastiCache/Redis for lookups.",
                ],
            }
        ],
        "workload_analysis": {
            "patterns_detected": [{"pattern_id": "p1", "description": "Point reads by key."}],
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": "a1",
                    "anti_pattern_type": "no-relational-need",
                    "severity_weight": 0.6,
                    "description": f"Single-table CRUD. {NO_RELATIONAL_NEED_ENGINE_HINT}",
                    "recommendation": NO_RELATIONAL_NEED_GUIDANCE,
                },
                {
                    "anti_pattern_id": "a2",
                    "anti_pattern_type": "simple-cache-read",
                    "severity_weight": 0.3,
                    "recommendation": "Consider ElastiCache/Redis for config caches.",
                },
            ],
        },
    }


def _outside(text: str) -> bool:
    return any(name in text for name in ("ElastiCache", "OpenSearch", "Redis"))


def test_the_analysis_prose_fields_are_filtered() -> None:
    raw = _analysis()
    before = copy.deepcopy(raw)
    out = scope_filtered_analysis(raw, SCOPE)
    assert raw == before  # the input is not mutated
    assert not _outside(json.dumps(out))
    rec = out["table_recommendations"][0]
    assert rec["rationale"] == "Hot reads."
    # A concern left with nothing in scope is dropped; the other keeps its label.
    assert [c.split(":", 1)[0] for c in rec["concerns"]] == ["no-relational-need"]
    anti = out["workload_analysis"]["anti_patterns_detected"]
    assert anti[0]["description"] == "Single-table CRUD."
    assert anti[1]["recommendation"] is None
    assert scope_filtered_analysis(raw, None) == raw


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-aurora_postgresql/analysis.json", _analysis())
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in SCOPE], "engine_scope": list(SCOPE)},
    )
    return store


def test_every_design_path_reads_the_filtered_analysis(store: LocalArtifactStore) -> None:
    out = handler._design_analysis_output(store, DB, JOB, "aurora_postgresql", 0)
    assert not _outside(json.dumps(out))
    # The analysis on disk is unchanged: the design step filters its copy.
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-aurora_postgresql/analysis.json")
    assert _outside(json.dumps(on_disk))


def test_a_job_with_no_scope_reads_the_analysis_as_written(store: LocalArtifactStore) -> None:
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in SCOPE]},
    )
    out = handler._design_analysis_output(store, DB, JOB, "aurora_postgresql", 0)
    assert out == _analysis()


def test_a_documentdb_label_is_kept() -> None:
    out = scope_filtered_text(
        f"[single-access-pattern-table] {SINGLE_ACCESS_PATTERN_GUIDANCE}", SCOPE
    )
    assert out.startswith("[single-access-pattern-table] Aurora adds connection overhead")


def test_lines_are_filtered_one_by_one_and_kept_apart() -> None:
    text = "- Keep the reads on DynamoDB.\n- Cache them in ElastiCache.\n- Index the key."
    assert scope_filtered_text(text, SCOPE) == "- Keep the reads on DynamoDB.\n- Index the key."
