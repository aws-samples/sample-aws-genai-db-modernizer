"""Realistic S3 artifact fixtures matching agent contract outputs.

These mirror the exact JSON structure each agent writes to S3 per ADR-016.
Path convention: s3://<bucket>/<database-name>/<job-id>/<agent-name>/artifact.json
"""

DATABASE_NAME = "ecommerce_prod"
JOB_ID = "550e8400-e29b-41d4-a716-446655440000"
BUCKET = "modernizer-test-data"

COLLECTOR_OUTPUT = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "collector",
    "status": "completed",
    "source_database_type": "mysql",
    "metadata": {
        "collection_timestamp": "2026-02-23T14:37:22Z",
        "collector_version": "1.0.0",
        "tables_collected": 1247,
        "queries_analyzed": 145000,
        "collection_duration_seconds": 906,
        "output_size_bytes": 25796608,
    },
    "schema": {
        "tables": [
            {"name": "users", "row_count": 500000, "columns": 12},
            {"name": "orders", "row_count": 2000000, "columns": 18},
            {"name": "products", "row_count": 50000, "columns": 24},
            {"name": "sessions", "row_count": 10000000, "columns": 6},
        ],
    },
    "metrics": {
        "database_size_gb": 24.6,
        "total_iops": 170103,
        "read_iops": 141442,
        "write_iops": 38558,
        "cpu_utilization_avg": 45.2,
    },
    "timestamp": "2026-02-23T14:37:22Z",
}

TRIAGE_OUTPUT = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "referee-triage",
    "selected_agents": [
        {"agent_type": "dynamodb", "reason": "95% key-value access patterns detected"},
        {"agent_type": "documentdb", "reason": "Nested JSON columns in 297 tables"},
        {"agent_type": "elasticache", "reason": "Hot key patterns with TTL usage"},
    ],
    "skipped_agents": [
        {"agent_type": "neptune", "reason": "No graph traversal patterns"},
        {"agent_type": "opensearch", "reason": "No full-text search queries"},
        {"agent_type": "keyspaces", "reason": "No wide-column access patterns"},
        {"agent_type": "aurora", "reason": "Source is MySQL — lateral move"},
    ],
    "confidence": 0.87,
    "timestamp": "2026-02-23T14:39:22Z",
}

ANALYSIS_DYNAMODB = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "dynamodb",
    "status": "completed",
    "confidence": 0.92,
    "estimated_monthly_cost": 1200,
    "table_count": 850,
    "pattern": "Key-value and single-table access",
    "timestamp": "2026-02-23T14:50:00Z",
}

ANALYSIS_DOCUMENTDB = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "documentdb",
    "status": "completed",
    "confidence": 0.88,
    "estimated_monthly_cost": 950,
    "table_count": 297,
    "pattern": "Document-oriented and JSON workloads",
    "timestamp": "2026-02-23T14:52:00Z",
}

ANALYSIS_ELASTICACHE = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "elasticache",
    "status": "completed",
    "confidence": 0.95,
    "estimated_monthly_cost": 650,
    "table_count": 100,
    "pattern": "High-frequency caching patterns",
    "timestamp": "2026-02-23T14:48:00Z",
}

SYNTHESIS_REPORT = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "referee-synthesis",
    "status": "completed",
    "ranking": [
        {"target": "elasticache", "confidence": 0.95, "weight": 0.33},
        {"target": "dynamodb", "confidence": 0.92, "weight": 0.33},
        {"target": "documentdb", "confidence": 0.88, "weight": 0.34},
    ],
    "needs_deeper_analysis": False,
    "recommended_schema_designs": ["dynamodb"],
    "timestamp": "2026-02-23T15:00:00Z",
}

SCHEMA_DYNAMODB = {
    "job_id": JOB_ID,
    "database_name": DATABASE_NAME,
    "agent_type": "schema-design-dynamodb",
    "target_type": "dynamodb",
    "status": "completed",
    "tables": [
        {
            "table_name": "Users",
            "partition_key": {"name": "user_id", "type": "S"},
            "sort_key": None,
            "gsi": [{"name": "email-index", "partition_key": "email"}],
        },
    ],
    "timestamp": "2026-02-23T15:10:00Z",
}

# Map of S3 keys to fixture data
ALL_ARTIFACTS = {
    f"{DATABASE_NAME}/{JOB_ID}/collector/output.json": COLLECTOR_OUTPUT,
    f"{DATABASE_NAME}/{JOB_ID}/referee-triage/triage.json": TRIAGE_OUTPUT,
    f"{DATABASE_NAME}/{JOB_ID}/analysis-dynamodb/analysis.json": ANALYSIS_DYNAMODB,
    f"{DATABASE_NAME}/{JOB_ID}/analysis-documentdb/analysis.json": ANALYSIS_DOCUMENTDB,
    f"{DATABASE_NAME}/{JOB_ID}/analysis-elasticache/analysis.json": ANALYSIS_ELASTICACHE,
    f"{DATABASE_NAME}/{JOB_ID}/referee-synthesis/report.json": SYNTHESIS_REPORT,
    f"{DATABASE_NAME}/{JOB_ID}/schema-dynamodb/schema_output.json": SCHEMA_DYNAMODB,
}
