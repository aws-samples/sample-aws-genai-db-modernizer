"""Files written before #972 carry no ``contract_version`` and must still load.

#972 gave every contract written to disk a ``contract_version``. A file one of
these contracts wrote before that has no such field; it must keep loading
(ADR-008 Decision 5: a new field is optional with a default) and, written back
by today's code, carry the current version. Each case below is a pre-#972 file
in the shape that contract wrote, with no ``contract_version`` key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from src.contracts.agent_interaction_models import AgentAnswers, AgentQuestions
from src.contracts.assignment_models import Assignment, ValidationResult
from src.contracts.load_test_models import (
    InfrastructureManifest,
    LoadTestOutput,
    PatternResult,
    SeedManifest,
)
from src.contracts.phase_models import PhaseProgression
from src.contracts.post_schema_router_output import RouterOutput
from src.contracts.schema_design_input import SchemaDesignGroupsManifest
from src.contracts.schema_revision_models import (
    SchemaConfirmation,
    SchemaRevisionRequest,
    SchemaVersionMeta,
)

_TS = "2026-10-01T12:00:00Z"
_LATENCY = {"p50": 1.0, "p90": 2.0, "p95": 3.0, "p99": 4.0, "p999": 5.0, "min": 0.5, "max": 6.0}
_PATTERN_RESULT = {
    "query_id": "q1",
    "access_pattern_description": "get user by id",
    "original_query_text": "SELECT * FROM users WHERE id = ?",
    "operation_type": "GetItem",
    "steps": ["GetItem users"],
    "source_latency_ms": _LATENCY,
    "target_latency_ms": _LATENCY,
    "improvement_factor": 2.0,
    "throughput_rps": 100.0,
    "total_requests": 10000,
    "error_count": 0,
    "error_rate_pct": 0.0,
    "throttle_count": 0,
    "cost_per_operation_usd": 0.0000001,
    "consumed_capacity_avg": 0.5,
    "code_artifact_path": "code/q1.py",
}
_MANIFEST = {
    "resources": [{"resource_type": "table", "resource_arn": "arn:example", "configuration": {}}],
    "tags": {"job": "job-1"},
}
_REVISION_REQUEST = {
    "base_version": 1,
    "pattern_modifications": [{"pattern_id": "DDB-AP-1", "action": "DROP"}],
    "table_modifications": [],
    "new_patterns": [],
}

# model -> (the version a loaded file says today, a pre-#972 file it wrote)
LEGACY_FILES: dict[type[BaseModel], tuple[str, dict[str, Any]]] = {
    Assignment: (
        "1.18",
        {
            "job_id": "job-1",
            "version": 1,
            "status": "auto_generated",
            "timestamp": _TS,
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "confidence": 80,
                    "source_tables": ["app.users"],
                    "assignment_reason": "key-value",
                }
            ],
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
        },
    ),
    ValidationResult: ("1.0", {"valid": True, "warnings": ["w"], "errors": []}),
    AgentQuestions: (
        "1.1",
        {
            "agent_type": "schema-dynamodb",
            "questions": [
                {
                    "question_id": "q-1",
                    "category": "partition_key_strategy",
                    "question": "Which key?",
                }
            ],
            "timestamp": _TS,
        },
    ),
    AgentAnswers: (
        "1.1",
        {"agent_type": "schema-dynamodb", "answers": {"q-1": "A"}, "timestamp": _TS},
    ),
    PhaseProgression: (
        "1.1",
        {
            "job_id": "job-1",
            "current_phase": "analysis",
            "phases": {"analysis": {"phase": "analysis", "status": "completed"}},
        },
    ),
    SchemaRevisionRequest: ("1.1", _REVISION_REQUEST),
    SchemaVersionMeta: (
        "1.1",
        {
            "version": 2,
            "base_version": 1,
            "initiated_by": "customer",
            "timestamp": _TS,
            "modifications": _REVISION_REQUEST,
            "redesigned_groups": ["dynamodb"],
            "verification": {"passed": True, "hard_errors": [], "warnings": []},
            "changelog": [
                {
                    "change_type": "removed",
                    "entity_type": "access_pattern",
                    "entity_id": "DDB-AP-1",
                    "description": "dropped",
                }
            ],
        },
    ),
    SchemaConfirmation: (
        "1.1",
        {"confirmed_version": 2, "confirmed_at": _TS, "engine": "dynamodb"},
    ),
    RouterOutput: (
        "1.4",
        {
            "job_id": "job-1",
            "routings": [
                {
                    "query_id": "q1",
                    "from_engine": "dynamodb",
                    "to_engine": "aurora_postgresql",
                    "reason": "unsupported pattern",
                }
            ],
            "terminal_queries": [],
            "cascade_depth": 1,
        },
    ),
    SchemaDesignGroupsManifest: (
        "1.1",
        {
            "job_id": "job-1",
            "database_name": "app",
            "target_engine": "dynamodb",
            "total_queries": 1,
            "total_groups": 1,
            "groups": [
                {
                    "group_index": 0,
                    "group_name": "users",
                    "primary_tables": ["users"],
                    "query_count": 1,
                    "table_count": 1,
                    "input_file": "group_0.json",
                }
            ],
        },
    ),
    PatternResult: ("1.0", _PATTERN_RESULT),
    InfrastructureManifest: ("1.0", _MANIFEST),
    SeedManifest: ("1.0", {"resources": {"users": 10}, "total_items": 10, "duration_seconds": 1.5}),
    LoadTestOutput: (
        "1.0",
        {
            "run_id": "run-1",
            "version": 1,
            "target_engine": "dynamodb",
            "test_duration_minutes": 15.0,
            "total_patterns_tested": 1,
            "patterns_passed": 1,
            "patterns_failed": 0,
            "total_cost_usd": 0.1,
            "infrastructure_deployed": _MANIFEST,
            "seed_summary": {
                "total_records": 10,
                "entities": {"users": 10},
                "relationships": {},
                "seed_duration_seconds": 1.5,
                "key_registry_path": "keys.json",
            },
            "pattern_results": [_PATTERN_RESULT],
            "assumptions": [],
        },
    ),
}

_IDS = [m.__name__ for m in LEGACY_FILES]


@pytest.mark.parametrize(("model", "case"), LEGACY_FILES.items(), ids=_IDS)
def test_a_file_written_before_the_version_still_loads(
    model: type[BaseModel], case: tuple[str, dict[str, Any]]
) -> None:
    version, legacy = case
    assert "contract_version" not in legacy
    loaded = model.model_validate(json.loads(json.dumps(legacy)))
    assert loaded.contract_version == version  # type: ignore[attr-defined]


@pytest.mark.parametrize(("model", "case"), LEGACY_FILES.items(), ids=_IDS)
def test_written_back_it_carries_the_current_version(
    model: type[BaseModel], case: tuple[str, dict[str, Any]]
) -> None:
    version, legacy = case
    dumped = model.model_validate(legacy).model_dump(mode="json")
    assert dumped["contract_version"] == version
    assert model.model_validate(dumped).model_dump(mode="json") == dumped


@pytest.mark.parametrize("model", list(LEGACY_FILES), ids=_IDS)
def test_a_version_that_is_not_major_minor_is_refused(model: type[BaseModel]) -> None:
    _, legacy = LEGACY_FILES[model]
    with pytest.raises(ValueError, match="contract_version"):
        model.model_validate({**legacy, "contract_version": "1.15.0"})
