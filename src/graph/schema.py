"""Graph DDL — node and relationship table definitions."""

from __future__ import annotations

from src.graph.store import GraphStore

NODE_TABLES = [
    """CREATE NODE TABLE IF NOT EXISTS Query (
        id STRING, sql_text STRING, calls_per_second DOUBLE,
        operation_type STRING, in_scope BOOLEAN,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS SourceTable (
        id STRING, database STRING, row_estimate INT64,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Destination (
        id STRING, engine STRING, artifact_type STRING, artifact_name STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Engine (
        id STRING, display_name STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Signal (
        id STRING, category STRING, description STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS CoDependencyGroup (
        id STRING, reason STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Decision (
        id STRING, category STRING, description STRING,
        rationale STRING, phase STRING, metadata STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS LoadTestRun (
        id STRING, timestamp STRING, query_id STRING,
        source_latency_ms DOUBLE, target_latency_ms DOUBLE,
        improvement_factor DOUBLE, throughput_rps DOUBLE,
        error_rate_pct DOUBLE, cost_per_operation_usd DOUBLE,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS AntiPattern (
        id STRING, anti_pattern_type STRING, severity_weight DOUBLE,
        description STRING, recommendation STRING,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Risk (
        id STRING, risk_type STRING, severity STRING,
        description STRING, mitigation STRING,
        PRIMARY KEY (id)
    )""",
]

REL_TABLES = [
    "CREATE REL TABLE IF NOT EXISTS READS_FROM (FROM Query TO SourceTable)",
    "CREATE REL TABLE IF NOT EXISTS MIGRATES_TO (FROM Query TO Destination, confidence DOUBLE, assignment_reason STRING)",
    "CREATE REL TABLE IF NOT EXISTS EMITS_SIGNAL (FROM Query TO Signal, strength DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS MEMBER_OF (FROM Query TO CoDependencyGroup)",
    "CREATE REL TABLE IF NOT EXISTS TESTED_IN (FROM Query TO LoadTestRun)",
    "CREATE REL TABLE IF NOT EXISTS VALIDATES (FROM LoadTestRun TO Destination)",
    "CREATE REL TABLE IF NOT EXISTS HOSTED_ON (FROM Destination TO Engine)",
    "CREATE REL TABLE IF NOT EXISTS AFFECTS (FROM Decision TO Destination)",
    "CREATE REL TABLE IF NOT EXISTS INFORMED_BY (FROM Decision TO Query)",
    "CREATE REL TABLE IF NOT EXISTS SUPERSEDES (FROM Decision TO Decision)",
    "CREATE REL TABLE IF NOT EXISTS OBSERVED_IN_QUERY (FROM AntiPattern TO Query)",
    "CREATE REL TABLE IF NOT EXISTS OBSERVED_IN_TABLE (FROM AntiPattern TO SourceTable)",
    "CREATE REL TABLE IF NOT EXISTS IMPACTS (FROM Risk TO SourceTable)",
    "CREATE REL TABLE IF NOT EXISTS EVIDENCED_BY (FROM Risk TO Query)",
]


def initialize_schema(store: GraphStore) -> None:
    """Create all node and relationship tables. Safe to call multiple times."""
    for ddl in NODE_TABLES:
        store.execute(ddl)
    for ddl in REL_TABLES:
        store.execute(ddl)
