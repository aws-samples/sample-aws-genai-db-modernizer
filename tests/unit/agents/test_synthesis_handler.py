"""Unit tests for referee-synthesis agent handler."""

from unittest.mock import MagicMock

from src.storage.artifact_store import ArtifactStore


def _mock_store(artifacts: dict[str, dict]) -> MagicMock:
    """Create a mock ArtifactStore that returns artifacts by key pattern."""
    store = MagicMock(spec=ArtifactStore)
    written = {}

    def read_json(path):
        for pattern, data in artifacts.items():
            if pattern in path:
                return data
        raise Exception(f"Artifact not found: {path}")

    def exists(path):
        for pattern in artifacts:
            if pattern in path:
                return True
        return False

    def write_json(path, data):
        written[path] = data

    store.read_json.side_effect = read_json
    store.exists.side_effect = exists
    store.write_json.side_effect = write_json
    store._written = written
    return store


MOCK_TRIAGE = {
    "selected_agents": [
        {"agent_type": "dynamodb", "reasons": ["key-value patterns"]},
    ],
}

MOCK_ANALYSIS = {
    "contract_version": "2.1",
    "table_recommendations": [
        {"table_id": "mydb.users", "confidence_score": 85, "migration_complexity": "LOW"},
        {"table_id": "mydb.orders", "confidence_score": 72, "migration_complexity": "MEDIUM"},
    ],
    "workload_analysis": {
        "patterns_detected": [
            {"pattern_id": "p1", "pattern_type": "key-value-lookup", "confidence": "HIGH"},
            {"pattern_id": "p2", "pattern_type": "write-heavy", "confidence": "HIGH"},
        ],
        "anti_patterns_detected": [
            {
                "anti_pattern_id": "ap1",
                "anti_pattern_type": "full-scan",
                "severity_weight": 0.8,
                "description": "Full table scans detected",
            },
        ],
    },
    "cost_estimate": {"monthly_cost_usd": 45.50, "cost_components": {"pricing_mode": "on-demand"}},
    "aggregate_recommendations": [
        {
            "aggregate_id": "agg-1",
            "root_table": "mydb.orders",
            "member_tables": ["mydb.orders", "mydb.users"],
            "co_access_confidence": 90,
            "combined_migration_complexity": "MEDIUM",
        },
    ],
}


MOCK_COLLECTOR = {
    "contract_version": "3.0",
    "job_id": "job-001",
    "metadata": {
        "collection_timestamp": "2024-01-01T00:00:00Z",
        "collector_version": "1.0.0",
        "source_database": {"engine": "mysql", "version": "8.0", "hostname": "test"},
    },
    "database_schema": {
        "tables": [
            {
                "table_id": "mydb.users",
                "table_name": "users",
                "row_count": 1000,
                "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
            },
            {
                "table_id": "mydb.orders",
                "table_name": "orders",
                "row_count": 5000,
                "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
            },
        ],
    },
    "queries": {
        "query_patterns": [
            {
                "query_id": "q1",
                "query_text": "SELECT * FROM users WHERE id=?",
                "query_type": "SELECT",
                "frequency_per_hour": 3600,
                "tables_accessed": ["mydb.users"],
            },
        ],
    },
    "metrics": {"performance_metrics": {}},
}

MOCK_SCHEMA_DESIGN = {
    "contract_version": "1.0",
    "job_id": "job-001",
    "source_database": "mydb",
    "target_engine": "dynamodb",
    "table_definitions": [
        {
            "table_name": "Users",
            "aggregate_pattern": "separate",
            "source_tables": ["mydb.users"],
            "gsis": [],
            "item_count": 1000,
            "item_size_bytes": 200,
        },
    ],
    "access_patterns": [
        {
            "pattern_id": "AP-1",
            "pattern_group": "User reads",
            "query_ids": ["q1"],
            "source_tables": ["mydb.users"],
            "description": "Get user by ID",
            "operation": "GetItem",
            "table_name": "Users",
            "key_condition": "PK=user_id",
            "design_rps": 30.0,
            "item_size_bytes": 200,
        },
    ],
    "hot_partition_analysis": [],
    "trade_offs": [
        {
            "description": "Single table for simplicity",
            "impact": "All data in one table reduces operational overhead but limits independent scaling.",
            "source_tables": ["db.users"],
            "target_tables": ["Users"],
            "query_ids": ["q1"],
            "engine": "dynamodb",
        }
    ],
    "validation_passed": True,
}


def _standard_artifacts():
    return {
        "referee-triage/triage.json": MOCK_TRIAGE,
        "collector/output.json": MOCK_COLLECTOR,
        "analysis-dynamodb/analysis.json": MOCK_ANALYSIS,
        "schema-dynamodb/schema_output.json": MOCK_SCHEMA_DESIGN,
    }


def test_synthesis_reads_all_artifacts():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    assert body["status"] == "completed"
    assert len(body["ranking"]) == 1
    assert body["ranking"][0]["target"] == "dynamodb"


def test_synthesis_ranking_confidence():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    r = body["ranking"][0]
    assert r["confidence_score"] == 78  # avg(85, 72)
    assert r["tables_highly_suitable"] == 1
    assert r["tables_suitable"] == 1
    assert r["monthly_cost_usd"] == 45.50
    assert r["patterns_detected"] == 2
    assert r["anti_patterns_detected"] == 1
    assert r["aggregate_count"] == 1
    assert r["schema_design_available"] is True
    assert r["target_tables"] == 1
    assert r["access_patterns"] == 1


def test_synthesis_table_mappings():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    mappings = body["table_mappings"]
    assert len(mappings) == 1  # Only users is in the schema design
    assert mappings[0]["source_table"] == "mydb.users"
    assert mappings[0]["recommended_database"] == "dynamodb"
    assert mappings[0]["target_table"] == "Users"


def test_synthesis_query_groups():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    groups = body["query_groups"]
    assert len(groups) == 1
    assert groups[0]["group_name"] == "User reads"
    assert groups[0]["engines"] == ["dynamodb"]
    assert len(groups[0]["source_queries"]) == 1


def test_synthesis_risk_assessment():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    risks = body["risk_assessment"]
    assert risks["overall_risk_level"] in ("LOW", "MEDIUM", "HIGH")
    assert len(risks["risks"]) >= 1  # At least the anti-pattern


def test_synthesis_architecture():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    arch = body["recommended_architecture"]
    assert arch["architecture_type"] == "SINGLE_DATABASE"
    assert len(arch["databases"]) >= 1
    assert arch["databases"][0]["service"] == "dynamodb"


def test_synthesis_summary_contains_key_info():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    summary = body["summary"]
    assert "dynamodb" in summary.lower()
    # The ranking score is verified deterministically in test_synthesis_ranking;
    # the LLM-generated narrative may or may not include the exact percentage.
    assert len(summary) > 50, "Executive summary should be substantive"


def test_synthesis_handles_missing_artifacts():
    """Synthesis works with only triage — all other artifacts missing."""
    store = _mock_store({"referee-triage/triage.json": MOCK_TRIAGE})

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    assert body["ranking"][0]["confidence_score"] == 0
    assert body["ranking"][0]["schema_design_available"] is False


def test_synthesis_schema_design_summaries():
    store = _mock_store(_standard_artifacts())

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    schemas = body["schema_designs"]
    assert "dynamodb" in schemas
    assert schemas["dynamodb"]["status"] == "completed"
    assert schemas["dynamodb"]["validation_passed"] is True
    assert len(schemas["dynamodb"]["tables"]) == 1


def test_synthesis_filters_resolved_risks():
    """Anti-patterns resolved by schema design access patterns are filtered out."""
    # Schema design covers query q-scan with an in-scope access pattern
    schema_with_coverage = {
        **MOCK_SCHEMA_DESIGN,
        "access_patterns": [
            {
                "pattern_id": "AP-1",
                "pattern_group": "User reads",
                "query_ids": ["q-scan"],  # covers the anti-pattern's query
                "source_tables": ["mydb.users"],
                "description": "Get user by ID",
                "operation": "GetItem",
                "table_name": "Users",
                "key_condition": "PK=user_id",
                "design_rps": 30.0,
                "item_size_bytes": 200,
                "in_scope": True,
            },
        ],
    }

    # Analysis has an anti-pattern on query q-scan
    analysis_with_scan = {
        **MOCK_ANALYSIS,
        "workload_analysis": {
            "patterns_detected": MOCK_ANALYSIS["workload_analysis"]["patterns_detected"],
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": "ap1",
                    "anti_pattern_type": "frequent-full-scan",
                    "severity_weight": 0.8,
                    "description": "Full table scans detected",
                    "query_ids": ["q-scan"],
                    "table_ids": ["mydb.users"],
                },
            ],
        },
    }

    artifacts = {
        "referee-triage/triage.json": MOCK_TRIAGE,
        "collector/output.json": MOCK_COLLECTOR,
        "analysis-dynamodb/analysis.json": analysis_with_scan,
        "schema-dynamodb/schema_output.json": schema_with_coverage,
    }

    store = _mock_store(artifacts)

    from src.agents.referee.synthesis_handler import run_synthesis

    run_synthesis("job-001", "mydb", store)

    body = store._written["mydb/job-001/referee-synthesis/report.json"]
    risks = body["risk_assessment"]["risks"]

    # The full-scan anti-pattern should be filtered out since AP-1 covers q-scan
    scan_risks = [r for r in risks if "Full table scans" in r.get("description", "")]
    assert len(scan_risks) == 0, f"Expected resolved risk to be filtered, got: {scan_risks}"
