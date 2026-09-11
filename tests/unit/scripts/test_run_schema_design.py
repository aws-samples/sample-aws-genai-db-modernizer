"""Unit tests for scripts/run_schema_design.py::run_external.

Verifies the aurora_postgresql branch attaches the deterministic
script-first draft (ADR-028) to the written llm_request, giving the
external/interactive seam the same script-first fidelity as the automated
Bedrock path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.run_schema_design import run_external
from src.storage.artifact_store import ArtifactStore

_COLLECTOR_AURORA_PG = {
    "contract_version": "3.0",
    "job_id": "job-001",
    "metadata": {
        "collection_timestamp": "2024-01-15T10:00:00Z",
        "collector_version": "1.0.0",
        "source_database": {
            "engine": "oracle",
            "version": "19c",
            "hostname": "test-db.local",
        },
    },
    "database_schema": {
        "tables": [
            {
                "table_id": "mydb.users",
                "table_name": "users",
                "row_count": 100,
                "primary_key": ["id"],
                "columns": [
                    {
                        "column_name": "id",
                        "data_type": "number",
                        "normalized_data_type": "integer",
                        "nullable": False,
                        "is_auto_increment": True,
                    }
                ],
            }
        ]
    },
    "queries": {
        "query_patterns": [
            {
                "query_id": "q1",
                "query_text": "SELECT * FROM users",
                "frequency_per_hour": 10.0,
                "tables_accessed": ["mydb.users"],
            }
        ]
    },
    "metrics": {"performance_metrics": {}},
}

_ANALYSIS_AURORA_PG = {
    "contract_version": "2.1",
    "agent_metadata": {
        "agent_name": "aurora-postgresql-analysis-agent",
        "agent_version": "1.0.0",
        "target_database": "aurora_postgresql",
        "analysis_timestamp": "2024-01-15T12:00:00Z",
    },
    "table_recommendations": [
        {
            "table_id": "mydb.users",
            "confidence_score": 90,
            "score_breakdown": {
                "pattern_match_score": 80,
                "complexity_score": 20,
                "performance_score": 85,
                "cost_score": 70,
            },
        }
    ],
    "workload_analysis": {"patterns_detected": []},
    "cost_estimate": {
        "monthly_cost_usd": 100.0,
        "cost_components": {"compute": 80.0, "storage": 20.0},
    },
}


def _mock_store(artifacts: dict[str, dict]) -> MagicMock:
    """Create a mock ArtifactStore pre-loaded with the given artifacts."""
    store = MagicMock(spec=ArtifactStore)
    written: dict[str, dict] = {}

    def read_json(path: str) -> dict:
        for pattern, data in artifacts.items():
            if pattern in path:
                return data
        raise FileNotFoundError(f"Artifact not found in mock store: {path}")

    def write_json(path: str, data: dict) -> None:
        written[path] = data

    store.read_json.side_effect = read_json
    store.write_json.side_effect = write_json
    store._written = written
    return store


def _base_store() -> MagicMock:
    return _mock_store(
        {
            "collector/output.json": _COLLECTOR_AURORA_PG,
            "analysis-aurora_postgresql/analysis.json": _ANALYSIS_AURORA_PG,
        }
    )


def test_run_external_attaches_deterministic_draft_for_aurora_postgresql(capsys):
    store = _base_store()

    run_external(store, "job-001", "mydb", "aurora_postgresql", assignment_version=0)

    written_key = next(k for k in store._written if "schema_design_aurora_postgresql.json" in k)
    llm_request = store._written[written_key]

    assert "draft" in llm_request
    draft = llm_request["draft"]
    assert "full_ddl" in draft
    assert "users" in draft["full_ddl"]
    assert draft["tables"][0]["columns"][0]["aurora_type"] == "BIGINT"
    assert llm_request["migration_strategy"] == "translate"

    # stdout status line should still report awaiting_llm
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    assert status["status"] == "awaiting_llm"


def test_run_external_other_engines_do_not_get_a_draft():
    store = _mock_store(
        {
            "collector/output.json": {
                "contract_version": "3.0",
                "job_id": "job-001",
                "metadata": {
                    "collection_timestamp": "2024-01-15T10:00:00Z",
                    "collector_version": "1.0.0",
                    "source_database": {
                        "engine": "mysql",
                        "version": "8.0",
                        "hostname": "h",
                    },
                },
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "mydb.users",
                            "table_name": "users",
                            "row_count": 1,
                            "columns": [
                                {"column_name": "id", "data_type": "int", "nullable": False}
                            ],
                        }
                    ]
                },
                "queries": {"query_patterns": []},
                "metrics": {"performance_metrics": {}},
            },
            "analysis-dynamodb/analysis.json": {
                "contract_version": "2.1",
                "table_recommendations": [],
            },
        }
    )

    run_external(store, "job-001", "mydb", "dynamodb", assignment_version=0)

    written_key = next(k for k in store._written if "schema_design_dynamodb.json" in k)
    llm_request = store._written[written_key]
    assert "draft" not in llm_request
