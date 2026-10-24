"""Graph DDL — node and relationship table definitions."""

from __future__ import annotations

from src.graph.store import GraphStore

# The graph's schema revision (ADR-023: a schema change needs a rebuild). A
# saved graph (``{db}/{job}/graph/context.lbug``) whose GraphMeta revision is
# not this one is stale and is rebuilt from the artifacts before it is read;
# a graph saved before GraphMeta existed reads as revision 0. Bump it whenever
# a table or column is added, renamed or dropped, or a populator writes a new
# kind of edge.
# - 1 (#339): GraphMeta, and MOVED edges (the move ledger of every lineage
#   version). Every graph saved before reads as revision 0.
# - 2 (#1361): ``Query.key_candidates_json``, the columns the redesign considered
#   as a key for the query (the effective version's access-pattern record).
# - 3 (#1373): MIGRATES_TO.pinned (a utility pin), and confidence follows the
#   query's engine; a graph built before keeps stale figures, so it is rebuilt.
GRAPH_SCHEMA_REVISION = 3
GRAPH_META_ID = "graph"

NODE_TABLES = [
    # performance_json / characteristics_json hold the collector's nested
    # ``source.performance`` and ``source.characteristics`` blocks as JSON strings.
    # The report only DISPLAYS them (Performance / Characteristics drill-down tabs),
    # never traverses into them, so a display-only string faithfully round-trips
    # the payload without inventing ~20 queryable columns. frequency_per_hour is a
    # scalar the General tab shows alongside calls_per_second.
    """CREATE NODE TABLE IF NOT EXISTS Query (
        id STRING, sql_text STRING, calls_per_second DOUBLE,
        operation_type STRING, in_scope BOOLEAN,
        frequency_per_hour DOUBLE, performance_json STRING, characteristics_json STRING,
        key_candidates_json STRING,
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
        engine STRING, schema_version INT64,
        source_p50 DOUBLE, source_p90 DOUBLE, source_p95 DOUBLE,
        source_p99 DOUBLE, source_p999 DOUBLE, source_min DOUBLE, source_max DOUBLE,
        target_p50 DOUBLE, target_p90 DOUBLE, target_p95 DOUBLE,
        target_p99 DOUBLE, target_p999 DOUBLE, target_min DOUBLE, target_max DOUBLE,
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
    """CREATE NODE TABLE IF NOT EXISTS AccessPattern (
        id STRING, engine STRING, schema_version INT64,
        description STRING, pattern_group STRING, operation STRING,
        design_rps DOUBLE, in_scope BOOLEAN,
        PRIMARY KEY (id)
    )""",
    """CREATE NODE TABLE IF NOT EXISTS Agent (
        id STRING, name STRING, phase STRING,
        PRIMARY KEY (id)
    )""",
    # One node (id GRAPH_META_ID): the schema revision the graph was built
    # with and the effective assignment version it was built from (0: none).
    """CREATE NODE TABLE IF NOT EXISTS GraphMeta (
        id STRING, schema_revision INT64, effective_version INT64,
        PRIMARY KEY (id)
    )""",
]

REL_TABLES = [
    "CREATE REL TABLE IF NOT EXISTS READS_FROM (FROM Query TO SourceTable)",
    "CREATE REL TABLE IF NOT EXISTS MIGRATES_TO (FROM Query TO Destination, confidence DOUBLE, pinned STRING, assignment_reason STRING)",
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
    "CREATE REL TABLE IF NOT EXISTS PART_OF (FROM Query TO AccessPattern)",
    # One edge per Move of the move ledger (#616, #339), for every assignment
    # version in the effective lineage: never overwritten, so the query's whole
    # routing history stays. (version, seq) identifies the Move. Nullable
    # scores are DOUBLE (written through CAST); evidence_json is the Move's
    # evidence (candidate ladder, group scores, tie rule, earlier tie) and
    # tied_engines_json its unresolved tie, both display-only JSON strings.
    """CREATE REL TABLE IF NOT EXISTS MOVED (
        FROM Query TO Engine,
        version INT64, seq INT64, pass STRING, reason_code STRING, actor STRING,
        from_engine STRING, score_before DOUBLE, score_after DOUBLE,
        runner_up_engine STRING, runner_up_score DOUBLE, group_key STRING,
        tied_engines_json STRING, evidence_json STRING
    )""",
    """CREATE REL TABLE IF NOT EXISTS PRODUCED_BY (
        FROM Decision TO Agent,
        FROM Signal TO Agent,
        FROM AntiPattern TO Agent,
        FROM AccessPattern TO Agent,
        FROM LoadTestRun TO Agent,
        FROM Risk TO Agent
    )""",
]


def stamp_graph_meta(store: GraphStore, effective_version: int = 0) -> None:
    """Record the schema revision and the effective assignment version on the graph."""
    store.execute(
        "MERGE (m:GraphMeta {id: $id}) SET m.schema_revision = $rev, m.effective_version = $ver",
        {"id": GRAPH_META_ID, "rev": GRAPH_SCHEMA_REVISION, "ver": int(effective_version)},
    )


def initialize_schema(store: GraphStore) -> None:
    """Create all node and relationship tables. Safe to call multiple times."""
    for ddl in NODE_TABLES:
        store.execute(ddl)
    for ddl in REL_TABLES:
        store.execute(ddl)
