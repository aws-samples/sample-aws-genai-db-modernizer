# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Graph DDL — node and relationship table definitions."""

from __future__ import annotations

from src.graph.store import GraphStore

# The graph's schema revision (ADR-023: a schema change needs a rebuild). A
# saved graph (``{db}/{job}/graph/context.lbug``) whose GraphMeta revision is
# not this one is stale and is rebuilt from the artifacts before it is read;
# a graph saved before GraphMeta existed reads as revision 0. Bump it whenever
# a table or column is added, renamed or dropped, a populator writes a new
# kind of edge, or the same inputs now build a different graph (a populator
# reads other artifacts or computes other values, as in 3 and 5).
# - 1 (#339): GraphMeta, and MOVED edges (the move ledger of every lineage
#   version). Every graph saved before reads as revision 0.
# - 2 (#1361): ``Query.key_candidates_json``, the columns the redesign considered
#   as a key for the query (the effective version's access-pattern record).
# - 3 (#1373): MIGRATES_TO.pinned (a utility pin), and confidence follows the
#   query's engine; a graph built before keeps stale figures, so it is rebuilt.
# - 4 (#847): ``GraphMeta.inputs_digest``, a digest of the artifacts the graph
#   was built from, so a re-run on the same assignment version rebuilds it.
# - 5 (#306): schema design and load test are read for every catalog engine
#   (Aurora included) at the effective assignment version, not the newest of
#   v1-v10; a graph built before can miss Aurora's access patterns or show an
#   older or abandoned design on unchanged inputs, so it is rebuilt.
# - 6 (#306): Risk.status (active or resolved), rule, wave, engine, resolved_on
#   and reason; the report's resolved risks become Risk nodes, and EVIDENCED_BY
#   edges link each risk to its queries.
# - 7 (#306): the version chain: AssignmentVersion nodes, SUPERSEDES and
#   RESTORES between them, AFFECTS from a version to its Moves' queries and
#   engines; consolidation Decisions INFORMED_BY the queries of their net
#   ledger moves, and
#   consolidation and reroute Decisions AFFECTS their engines.
# - 8 (#306): SCORED edges, each query's candidate ladder per assignment
#   version with its recorded score dimensions.
# - 9 (#306): TargetObject nodes and TARGETS edges, each query's design objects
#   per version; a Destination is the query's engine, no longer a guess from its
#   first source table.
# - 10 (#306): WRITES edges (the tables a query writes or locks, as routing's
#   one-writer rule reads them) and SourceTable.placeholder / placeholder_reason.
GRAPH_SCHEMA_REVISION = 10
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
    # placeholder: a collector's name for "no table" (report_facts.
    # PLACEHOLDER_TABLE_NAMES: 'unknown', a dummy table), kept and flagged, with
    # placeholder_reason (#306).
    """CREATE NODE TABLE IF NOT EXISTS SourceTable (
        id STRING, database STRING, row_estimate INT64,
        placeholder BOOLEAN, placeholder_reason STRING,
        PRIMARY KEY (id)
    )""",
    # A design's target object (#306): a DynamoDB table or GSI (parent: its table),
    # a DocumentDB collection, an OpenSearch index or stream, an ElastiCache key
    # pattern, an Aurora table, at one schema version (id ...@v<N>). A fallback
    # (no schema version) labels a query with no design object: 'not_designed'
    # (no design for the engine at that version), 'out_of_scope' (the design marks
    # its patterns out of scope or unsupported) or 'not_in_design' (no access
    # pattern for the query, or one naming an object the design does not define);
    # id '<engine>:<kind>'.
    """CREATE NODE TABLE IF NOT EXISTS TargetObject (
        id STRING, engine STRING, kind STRING, name STRING, parent STRING,
        schema_version INT64, fallback BOOLEAN,
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
    # One node per assignment version of the effective lineage (ADR-028, #306):
    # kind is the pass that made it (first_assignment, reality_check,
    # customer_pick, customer_scope, post_schema_reroute, rollback,
    # source_of_truth_reroute, auto_decided; unknown for a source this code does
    # not know), from its status and source, or the legacy shapes for a version
    # written before source; rolled_back_version is set on a rollback only;
    # passes_json and actors_json are its Moves' distinct passes and actors;
    # created_at its timestamp ('' when none).
    """CREATE NODE TABLE IF NOT EXISTS AssignmentVersion (
        id STRING, version INT64, kind STRING, source STRING, status STRING,
        effective BOOLEAN, created_at STRING, move_count INT64,
        passes_json STRING, actors_json STRING,
        previous_version INT64, rolled_back_version INT64,
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
    # status is 'active' (risk_assessment.risks) or 'resolved' (resolved_risks,
    # #306). rule and wave come from a migration-risk rule's risk; engine,
    # resolved_on and reason explain a resolved risk (the engine whose analysis
    # raised it, the engine it is resolved on, and why). Absent values are null.
    """CREATE NODE TABLE IF NOT EXISTS Risk (
        id STRING, status STRING, risk_type STRING, severity STRING,
        description STRING, mitigation STRING, rule STRING, wave INT64,
        engine STRING, resolved_on STRING, reason STRING,
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
        id STRING, schema_revision INT64, effective_version INT64, inputs_digest STRING,
        PRIMARY KEY (id)
    )""",
]

REL_TABLES = [
    # READS_FROM: every table a query accesses. WRITES: every table it writes or
    # locks, as routing's one-writer rule reads it (write_groups.written_tables,
    # ADR-031 Amendment 9); locking for a locking read (#306). Named like the
    # planned Routine-to-SourceTable WRITES (#1677), which can add its pair here.
    "CREATE REL TABLE IF NOT EXISTS READS_FROM (FROM Query TO SourceTable)",
    "CREATE REL TABLE IF NOT EXISTS WRITES (FROM Query TO SourceTable, locking BOOLEAN)",
    "CREATE REL TABLE IF NOT EXISTS MIGRATES_TO (FROM Query TO Destination, confidence DOUBLE, pinned STRING, assignment_reason STRING)",
    "CREATE REL TABLE IF NOT EXISTS EMITS_SIGNAL (FROM Query TO Signal, strength DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS MEMBER_OF (FROM Query TO CoDependencyGroup)",
    "CREATE REL TABLE IF NOT EXISTS TESTED_IN (FROM Query TO LoadTestRun)",
    "CREATE REL TABLE IF NOT EXISTS VALIDATES (FROM LoadTestRun TO Destination)",
    """CREATE REL TABLE IF NOT EXISTS HOSTED_ON (
        FROM Destination TO Engine,
        FROM TargetObject TO Engine
    )""",
    # A query's design objects per assignment version (#306), with the schema
    # version they come from (null for a fallback), whether the version is the
    # effective one, and the access patterns that name the object (JSON list).
    """CREATE REL TABLE IF NOT EXISTS TARGETS (
        FROM Query TO TargetObject,
        version INT64, schema_version INT64, effective BOOLEAN, pattern_ids_json STRING
    )""",
    # AFFECTS and SUPERSEDES keep their meaning, made versioned (#306): an
    # assignment version AFFECTS the queries its Moves changed (role 'moved', or
    # 'tie' when they changed no engine) and the engines they left (role 'from')
    # or landed on ('to'); a consolidation or reroute
    # Decision AFFECTS the engines it moves queries from and to. A version
    # SUPERSEDES the one before it; a rollback RESTORES the version it restates.
    """CREATE REL TABLE IF NOT EXISTS AFFECTS (
        FROM Decision TO Destination,
        FROM Decision TO Engine,
        FROM AssignmentVersion TO Query,
        FROM AssignmentVersion TO Engine,
        role STRING
    )""",
    "CREATE REL TABLE IF NOT EXISTS INFORMED_BY (FROM Decision TO Query)",
    """CREATE REL TABLE IF NOT EXISTS SUPERSEDES (
        FROM Decision TO Decision,
        FROM AssignmentVersion TO AssignmentVersion
    )""",
    "CREATE REL TABLE IF NOT EXISTS RESTORES (FROM AssignmentVersion TO AssignmentVersion)",
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
    # One edge per rung of a query's candidate ladder per assignment version
    # (#306), for each version the query has a Move in and for every query in the
    # effective version (current). The ladder is the one the query's last Move with
    # recorded candidates holds, in that version or an earlier one: ladder_version,
    # move_seq and pass (that Move's own pass, as on MOVED) say where it came
    # from; ladder_copied_from is the abandoned reroute version a rollback's Move
    # copied its candidates from, null otherwise. score is null and gate set when a gate excluded the engine. assigned
    # is the version's own assigned_engine. dimensions_json holds the version's
    # access-pattern record's score dimensions (#1471) when they explain the score
    # (the record's score equals it), else null. Routing never reads these edges.
    """CREATE REL TABLE IF NOT EXISTS SCORED (
        FROM Query TO Engine,
        version INT64, score DOUBLE, gate STRING, assigned BOOLEAN, current BOOLEAN,
        ladder_version INT64, move_seq INT64, pass STRING, ladder_copied_from INT64,
        dimensions_json STRING
    )""",
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


def stamp_graph_meta(
    store: GraphStore, effective_version: int = 0, inputs_digest: str = ""
) -> None:
    """Record the schema revision, the effective assignment version and the inputs digest."""
    store.execute(
        "MERGE (m:GraphMeta {id: $id}) SET m.schema_revision = $rev, "
        "m.effective_version = $ver, m.inputs_digest = $digest",
        {
            "id": GRAPH_META_ID,
            "rev": GRAPH_SCHEMA_REVISION,
            "ver": int(effective_version),
            "digest": inputs_digest,
        },
    )


def initialize_schema(store: GraphStore) -> None:
    """Create all node and relationship tables. Safe to call multiple times."""
    for ddl in NODE_TABLES:
        store.execute(ddl)
    for ddl in REL_TABLES:
        store.execute(ddl)
