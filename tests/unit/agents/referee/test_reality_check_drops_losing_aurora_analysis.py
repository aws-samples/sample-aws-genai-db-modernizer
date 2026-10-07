"""#381 review: Reality Check must not resurrect the #381 losing Aurora engine.

Triage selects both ``aurora_mysql`` and ``aurora_postgresql`` for a
heterogeneous source (SQL Server, Oracle, DB2), so both have an
``analysis-<engine>/analysis.json`` artifact on disk by the time Reality Check
runs -- but the assignment resolver already collapsed them to one winner
(``Assignment.aurora_engine_choice``) before anything was assigned.
``run_reality_check_deterministic`` must drop the loser's analysis from its own
``analysis_outputs`` too, so the absorption pass and consolidation logic never
treat it as a live candidate to move a query onto.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.referee.reality_check_handler import run_reality_check_deterministic
from src.storage.artifact_store import ArtifactStore

_ASSIGNMENT_WITH_CHOICE = {
    "version": 1,
    "query_assignments": [
        {
            "query_id": "q1",
            "assigned_engine": "aurora_postgresql",
            "assignment_reason": "highest confidence for aurora_postgresql",
        },
        {
            "query_id": "q2",
            "assigned_engine": "aurora_postgresql",
            "assignment_reason": "highest confidence for aurora_postgresql",
        },
    ],
    "aurora_engine_choice": {
        "source_engine": "sqlserver",
        "engine": "aurora_postgresql",
        "totals": {"aurora_mysql": 60.0, "aurora_postgresql": 65.0},
        "margin": 5.0,
        "deciding_features": [],
        "reason": "higher total adjusted score wins (60.0 vs 65.0)",
    },
}

_TRIAGE_BOTH_AURORA = {
    "selected_agents": [
        {"agent_type": "aurora_mysql", "reasons": ["#381"]},
        {"agent_type": "aurora_postgresql", "reasons": ["#381"]},
    ],
    "signals": [],
    "query_capabilities": {},
}

_COLLECTOR = {
    "metadata": {"source_database": {"engine": "sqlserver"}},
    "database_schema": {"tables": [{"table_id": "Sales.Orders"}]},
    "queries": {
        "query_patterns": [
            {
                "query_id": "q1",
                "query_text": "SELECT * FROM Sales.Orders WHERE id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["Sales.Orders"],
                "calls_per_second": 1.0,
            },
            {
                "query_id": "q2",
                "query_text": "SELECT * FROM Sales.Orders WHERE id = ?",
                "query_type": "SELECT",
                "tables_accessed": ["Sales.Orders"],
                "calls_per_second": 1.0,
            },
        ]
    },
}

_ANALYSIS_MYSQL = {
    "table_recommendations": [{"table_id": "Sales.Orders", "confidence_score": 60}],
    "signals": [],
}
_ANALYSIS_POSTGRES = {
    "table_recommendations": [{"table_id": "Sales.Orders", "confidence_score": 65}],
    "signals": [],
}


def _mock_store() -> MagicMock:
    store = MagicMock(spec=ArtifactStore)
    artifacts = {
        "assignment/v1/assignment.json": _ASSIGNMENT_WITH_CHOICE,
        "referee-triage/triage.json": _TRIAGE_BOTH_AURORA,
        "collector/output.json": _COLLECTOR,
        "analysis-aurora_mysql/analysis.json": _ANALYSIS_MYSQL,
        "analysis-aurora_postgresql/analysis.json": _ANALYSIS_POSTGRES,
    }

    def read_json(path):
        for pattern, data in artifacts.items():
            if pattern in path:
                return data
        raise FileNotFoundError(f"Artifact not found in mock store: {path}")

    def exists(path):
        return any(pattern in path for pattern in artifacts)

    store.read_json.side_effect = read_json
    store.exists.side_effect = exists
    return store


class TestRealityCheckDropsLosingAuroraAnalysis:
    def test_losing_engine_analysis_is_dropped(self):
        store = _mock_store()
        result = run_reality_check_deterministic("job-1", "mydb", store, assignment_version=1)
        assert "aurora_mysql" not in result["analysis_outputs"]
        assert "aurora_postgresql" in result["analysis_outputs"]

    def test_homogeneous_assignment_without_a_choice_is_unaffected(self):
        store = _mock_store()
        # No aurora_engine_choice at all (homogeneous source, or pre-#381 artifact):
        # both engines' analysis -- if both happened to be present -- are left alone.
        homogeneous_assignment = {**_ASSIGNMENT_WITH_CHOICE, "aurora_engine_choice": None}
        store.read_json.side_effect = lambda path, _a=homogeneous_assignment: (
            _a
            if "assignment/v1/assignment.json" in path
            else next(
                data
                for pattern, data in {
                    "referee-triage/triage.json": _TRIAGE_BOTH_AURORA,
                    "collector/output.json": _COLLECTOR,
                    "analysis-aurora_mysql/analysis.json": _ANALYSIS_MYSQL,
                    "analysis-aurora_postgresql/analysis.json": _ANALYSIS_POSTGRES,
                }.items()
                if pattern in path
            )
        )
        result = run_reality_check_deterministic("job-1", "mydb", store, assignment_version=1)
        assert "aurora_mysql" in result["analysis_outputs"]
        assert "aurora_postgresql" in result["analysis_outputs"]
