# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The graph query catalog (#307): the questions the context graph answers.

Each entry is one fixed, read-only Cypher statement with:

- an id, a short title and the question in plain words;
- typed parameters, each with a read-only choice query that lists its values
  from the graph itself (tables, engines, query ids, access patterns);
- a maximum row bound, applied by ``cypher_guard.bound_result_rows``;
- a node/edge projection, so its rows can also be drawn as a graph;
- the stages it needs (schema design, load test, ...), so an empty result can
  be explained instead of read as "nothing there".

The cookbook's query section (``docs/guides/context-graph-query-cookbook.md``)
is rendered from this catalog by ``scripts/render_graph_catalog.py``, and every
entry runs against real job fixtures in ``tests/graph/test_catalog.py``.
Entries are fixed text: parameter values are always bound as parameters,
never formatted into the statement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from src.graph.cypher_guard import bound_result_rows
from src.graph.store import MAX_RESULT_ROWS, GraphStore

#: The largest row bound an entry may declare: the raw endpoint's limit.
MAX_ROWS = MAX_RESULT_ROWS

#: Values a parameter's choice query lists at most.
MAX_CHOICES = MAX_RESULT_ROWS

#: Cookbook sections, in rendering order, with their headings.
SECTIONS: dict[str, str] = {
    "summary": "Summary queries (reproduce UI views)",
    "impact": "Impact and provenance queries",
}


class ParamKind(str, Enum):
    """What a parameter names. Every value is a string id from the graph."""

    TABLE = "table"
    ENGINE = "engine"
    QUERY = "query"
    ACCESS_PATTERN = "access_pattern"
    VERSION = "version"
    TARGET = "target"


@dataclass(frozen=True)
class Param:
    """One bound parameter (``$name`` in the entry's Cypher)."""

    name: str
    kind: ParamKind
    description: str
    #: Read-only Cypher returning one ``value`` column: the values to choose from.
    choices_cypher: str
    #: A value from the wordpress sample, for the docs.
    example: str
    #: Read-only Cypher taking ``$value``: a row when the value is one of the
    #: choices. Checks a value without listing every choice (``value_exists``).
    exists_cypher: str


@dataclass(frozen=True)
class Need:
    """A pipeline stage an entry reads from, and how to tell whether it ran."""

    id: str
    stage: str
    #: The nodes or edges the stage writes, as shown in the docs.
    produces: str
    #: Read-only Cypher returning one ``n`` column: > 0 when the stage's data is there.
    probe_cypher: str


@dataclass(frozen=True)
class NodeRef:
    """A result column whose value (an id, or a list of ids) is a node of ``label``."""

    column: str
    label: str


@dataclass(frozen=True)
class EdgeRef:
    """An edge drawn from every node in ``source`` to every node in ``target``."""

    source: str
    target: str
    label: str


@dataclass(frozen=True)
class Projection:
    """How rows map to a drawable graph. Empty for count-only entries."""

    nodes: tuple[NodeRef, ...] = ()
    edges: tuple[EdgeRef, ...] = ()


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    #: The cookbook's number or letter, kept so existing references hold.
    ref: str
    section: str
    title: str
    question: str
    cypher: str
    max_rows: int
    projection: Projection = field(default_factory=Projection)
    params: tuple[Param, ...] = ()
    needs: tuple[Need, ...] = ()
    #: Extra guidance shown in the docs.
    note: str = ""
    #: The columns that identify a row. The final ORDER BY ends with them, so rows
    #: that tie on the leading sort keys still come back in one stable order.
    row_key: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogResult:
    rows: list[dict]
    truncated: bool


# ---------------------------------------------------------------------------
# Needs: the stages entries read from
# ---------------------------------------------------------------------------

TRIAGE = Need(
    "triage",
    "Referee triage",
    "`Signal` nodes",
    "MATCH (n:Signal) RETURN count(n) AS n",
)
ANALYSIS = Need(
    "analysis",
    "Analysis, per engine",
    "`AntiPattern` nodes",
    "MATCH (n:AntiPattern) RETURN count(n) AS n",
)
ASSIGNMENT = Need(
    "assignment",
    "Assignment",
    "`Destination` nodes and `MIGRATES_TO` edges",
    "MATCH (n:Destination) RETURN count(n) AS n",
)
MOVE_LEDGER = Need(
    "move_ledger",
    "Assignment move ledger",
    "`MOVED` edges",
    "MATCH ()-[m:MOVED]->() RETURN count(m) AS n",
)
DECISIONS = Need(
    "decisions",
    "Reality check or schema design",
    "`Decision` nodes",
    "MATCH (n:Decision) RETURN count(n) AS n",
)
QUERY_DECISIONS = Need(
    "query_decisions",
    "Schema design trade-offs",
    "`INFORMED_BY` edges from a `Decision` to a `Query`",
    "MATCH (:Decision)-[r:INFORMED_BY]->(:Query) RETURN count(r) AS n",
)
SCHEMA_DESIGN = Need(
    "schema_design",
    "Schema design, per engine",
    "`AccessPattern` nodes",
    "MATCH (n:AccessPattern) RETURN count(n) AS n",
)
LOAD_TEST = Need(
    "load_test",
    "Load test, per engine",
    "`LoadTestRun` nodes",
    "MATCH (n:LoadTestRun) RETURN count(n) AS n",
)
SYNTHESIS = Need(
    "synthesis",
    "Referee synthesis",
    "active `Risk` nodes",
    "MATCH (n:Risk {status: 'active'}) RETURN count(n) AS n",
)
VERSION_CHAIN = Need(
    "version_chain",
    "Assignment version chain",
    "`AssignmentVersion` nodes",
    "MATCH (n:AssignmentVersion) RETURN count(n) AS n",
)
RESOLVED_RISKS = Need(
    "resolved_risks",
    "Referee synthesis, when the assignment resolved a risk",
    "resolved `Risk` nodes",
    "MATCH (n:Risk {status: 'resolved'}) RETURN count(n) AS n",
)

DESIGN_TARGETS = Need(
    "design_targets",
    "Schema design, per engine",
    "`TargetObject` nodes from a design (not a fallback)",
    "MATCH (n:TargetObject {fallback: false}) RETURN count(n) AS n",
)
SCORED_LADDERS = Need(
    "scored",
    "Assignment move ledger, with candidate ladders",
    "`SCORED` edges",
    "MATCH ()-[s:SCORED]->() RETURN count(s) AS n",
)

NEEDS: tuple[Need, ...] = (
    TRIAGE,
    ANALYSIS,
    ASSIGNMENT,
    MOVE_LEDGER,
    VERSION_CHAIN,
    DECISIONS,
    QUERY_DECISIONS,
    SCHEMA_DESIGN,
    LOAD_TEST,
    SYNTHESIS,
    RESOLVED_RISKS,
    DESIGN_TARGETS,
    SCORED_LADDERS,
)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

TABLE_PARAM = Param(
    "table_id",
    ParamKind.TABLE,
    "A source table id, schema-qualified as the collector wrote it.",
    "MATCH (st:SourceTable) RETURN st.id AS value ORDER BY value",
    "wordpress.wp_posts",
    "MATCH (st:SourceTable {id: $value}) RETURN st.id AS value",
)
DESTINATION_ENGINE_PARAM = Param(
    "engine",
    ParamKind.ENGINE,
    "An engine id the assignment migrates queries to.",
    "MATCH (d:Destination) RETURN DISTINCT d.engine AS value ORDER BY value",
    "dynamodb",
    "MATCH (d:Destination {engine: $value}) RETURN d.engine AS value",
)
TARGET_PARAM = Param(
    "target_id",
    ParamKind.TARGET,
    "A design target object id: engine, kind, name and schema version.",
    "MATCH (o:TargetObject {fallback: false}) RETURN o.id AS value ORDER BY value",
    "aurora_mysql:table:wp_posts@v2",
    "MATCH (o:TargetObject {id: $value, fallback: false}) RETURN o.id AS value",
)
QUERY_PARAM = Param(
    "query_id",
    ParamKind.QUERY,
    "A query id, busiest first.",
    "MATCH (q:Query) RETURN q.id AS value ORDER BY q.calls_per_second DESC, value",
    "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799",
    "MATCH (q:Query {id: $value}) RETURN q.id AS value",
)
DECIDED_QUERY_PARAM = Param(
    "query_id",
    ParamKind.QUERY,
    "A query id that a design decision names.",
    "MATCH (:Decision)-[:INFORMED_BY]->(q:Query) RETURN DISTINCT q.id AS value ORDER BY value",
    "66c96f951dd01698438c1be2887ff2553664cb0b99c1a1b0108b18ca22dcb799",
    "MATCH (:Decision)-[:INFORMED_BY]->(q:Query {id: $value}) RETURN q.id AS value",
)
ACCESS_PATTERN_PARAM = Param(
    "access_pattern_id",
    ParamKind.ACCESS_PATTERN,
    "An access pattern id from a schema design.",
    "MATCH (ap:AccessPattern) RETURN ap.id AS value ORDER BY value",
    "DDB-AP-1",
    "MATCH (ap:AccessPattern {id: $value}) RETURN ap.id AS value",
)
VERSION_PARAM = Param(
    "version_id",
    ParamKind.VERSION,
    "An assignment version id (`assignment-v<N>`), newest first.",
    "MATCH (v:AssignmentVersion) RETURN v.id AS value ORDER BY v.version DESC, value",
    "assignment-v2",
    "MATCH (v:AssignmentVersion {id: $value}) RETURN v.id AS value",
)

# ---------------------------------------------------------------------------
# The entries
# ---------------------------------------------------------------------------

_SEVERITY_RANK = (
    "CASE r.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
    "WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END"
)

CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="node_census",
        ref="1",
        section="summary",
        title="Node census",
        question="Which kinds of nodes does the graph hold, and how many of each?",
        cypher="MATCH (n) RETURN label(n) AS type, count(*) AS n ORDER BY n DESC, type",
        max_rows=50,
        note="Run this first: a missing label means its stage has not run (see Empty results).",
        row_key=("type",),
    ),
    CatalogEntry(
        id="engine_distribution",
        ref="2",
        section="summary",
        title="Engine distribution: queries per target engine",
        question="How many queries go to each target engine?",
        cypher=(
            "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination)-[:HOSTED_ON]->(e:Engine) "
            "RETURN e.id AS engine, count(DISTINCT q) AS queries ORDER BY queries DESC, engine"
        ),
        max_rows=50,
        projection=Projection(nodes=(NodeRef("engine", "Engine"),)),
        needs=(ASSIGNMENT,),
        row_key=("engine",),
    ),
    CatalogEntry(
        id="table_engines",
        ref="3",
        section="summary",
        title="Table to engine mapping",
        question="Which engines serve each source table's queries?",
        cypher=(
            "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) "
            "RETURN st.id AS table_id, collect(DISTINCT d.engine) AS engines, "
            "count(DISTINCT q) AS queries ORDER BY queries DESC, table_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("table_id", "SourceTable"), NodeRef("engines", "Engine")),
            edges=(EdgeRef("table_id", "engines", "served by"),),
        ),
        needs=(ASSIGNMENT,),
        row_key=("table_id",),
    ),
    CatalogEntry(
        id="access_patterns_per_engine",
        ref="4",
        section="summary",
        title="Access patterns per engine",
        question="How many access patterns did each engine's schema design define, at what rate?",
        cypher=(
            "MATCH (ap:AccessPattern) RETURN ap.engine AS engine, count(*) AS patterns, "
            "sum(ap.design_rps) AS total_rps ORDER BY total_rps DESC, engine"
        ),
        max_rows=50,
        projection=Projection(nodes=(NodeRef("engine", "Engine"),)),
        needs=(SCHEMA_DESIGN,),
        row_key=("engine",),
    ),
    CatalogEntry(
        id="anti_patterns_by_type",
        ref="5",
        section="summary",
        title="Anti-patterns by type",
        question="Which anti-pattern types were found, in how many queries, and how severe?",
        # The severity is averaged per anti-pattern before the query count: a
        # DISTINCT aggregate next to avg() in one projection returns null or
        # NaN for the avg on this engine.
        cypher=(
            "MATCH (a:AntiPattern) "
            "WITH a.anti_pattern_type AS type, avg(a.severity_weight) AS avg_severity "
            "MATCH (b:AntiPattern {anti_pattern_type: type})-[:OBSERVED_IN_QUERY]->(q:Query) "
            "RETURN type, count(DISTINCT q) AS queries, avg_severity "
            "ORDER BY queries DESC, type"
        ),
        max_rows=MAX_ROWS,
        needs=(ANALYSIS,),
        row_key=("type",),
    ),
    CatalogEntry(
        id="risk_register",
        ref="6",
        section="summary",
        title="Risk register",
        question="Which migration risks did synthesis raise, and which tables do they touch?",
        cypher=(
            "MATCH (r:Risk {status: 'active'})-[:IMPACTS]->(st:SourceTable) "
            "WITH r, collect(st.id) AS affected_tables "
            "RETURN r.id AS risk_id, r.severity AS severity, r.risk_type AS type, "
            "r.description AS description, affected_tables "
            f"ORDER BY {_SEVERITY_RANK}, risk_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("risk_id", "Risk"), NodeRef("affected_tables", "SourceTable")),
            edges=(EdgeRef("risk_id", "affected_tables", "IMPACTS"),),
        ),
        needs=(SYNTHESIS,),
        row_key=("risk_id",),
    ),
    CatalogEntry(
        id="resolved_risks",
        ref="6b",
        section="summary",
        title="Resolved risks",
        question="Which risks did the assignment resolve, where and why, and which queries do they name?",
        cypher=(
            "MATCH (r:Risk {status: 'resolved'}) "
            "OPTIONAL MATCH (r)-[:EVIDENCED_BY]->(q:Query) "
            "RETURN r.id AS risk_id, r.severity AS severity, r.engine AS raised_by, "
            "r.resolved_on AS resolved_on, r.reason AS reason, "
            "collect(DISTINCT q.id) AS queries ORDER BY risk_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("risk_id", "Risk"), NodeRef("queries", "Query")),
            edges=(EdgeRef("risk_id", "queries", "EVIDENCED_BY"),),
        ),
        needs=(RESOLVED_RISKS,),
        row_key=("risk_id",),
    ),
    CatalogEntry(
        id="signals",
        ref="7",
        section="summary",
        title="Signals from triage",
        question="Which triage signals did the queries emit, and how often?",
        cypher=(
            "MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal) RETURN s.id AS signal, "
            "s.category AS category, count(q) AS queries ORDER BY queries DESC, signal"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(nodes=(NodeRef("signal", "Signal"),)),
        needs=(TRIAGE,),
        row_key=("signal",),
    ),
    CatalogEntry(
        id="decisions_summary",
        ref="8",
        section="summary",
        title="Decisions: provenance summary",
        question="Which kinds of decisions were recorded, with a few examples of each?",
        # The engine needs SKIP or LIMIT after ORDER BY in WITH; SKIP 0 keeps every
        # decision and makes the examples the first three by id.
        cypher=(
            "MATCH (d:Decision) WITH d ORDER BY d.id SKIP 0 "
            "RETURN d.category AS category, count(*) AS n, "
            "collect(d.description)[0..3] AS examples ORDER BY n DESC, category"
        ),
        max_rows=50,
        needs=(DECISIONS,),
        row_key=("category",),
    ),
    CatalogEntry(
        id="load_test_per_pattern",
        ref="9",
        section="summary",
        title="Load-test results per pattern",
        question="How did each access pattern's queries perform in the load test?",
        # A run belongs to the pattern of its own engine and schema version, as
        # in the curated load-test endpoint. count(q), not count(DISTINCT q): a run
        # id is unique per engine, version and query, so it counts each query once,
        # and a DISTINCT aggregate here would turn both avg() columns into NaN.
        cypher=(
            "MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]->(lt:LoadTestRun) "
            "WHERE lt.engine = ap.engine AND lt.schema_version = ap.schema_version "
            "RETURN ap.id AS pattern, ap.engine AS engine, count(q) AS queries, "
            "avg(lt.improvement_factor) AS avg_improvement, "
            "avg(lt.target_p90) AS avg_target_p90 ORDER BY pattern, engine"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(nodes=(NodeRef("pattern", "AccessPattern"),)),
        needs=(SCHEMA_DESIGN, LOAD_TEST),
        row_key=("pattern", "engine"),
    ),
    CatalogEntry(
        id="top_traffic_queries",
        ref="10",
        section="summary",
        title="Top-traffic queries",
        question="Which queries carry the most traffic?",
        cypher=(
            "MATCH (q:Query) RETURN q.id AS query_id, q.operation_type AS operation_type, "
            "q.calls_per_second AS calls_per_second "
            "ORDER BY calls_per_second DESC, query_id LIMIT 20"
        ),
        max_rows=20,
        projection=Projection(nodes=(NodeRef("query_id", "Query"),)),
        row_key=("query_id",),
    ),
    CatalogEntry(
        id="graph_build",
        ref="11",
        section="summary",
        title="Graph build stamp",
        question="Which schema revision and assignment version was this graph built from?",
        cypher=(
            "MATCH (m:GraphMeta) RETURN m.schema_revision AS schema_revision, "
            "m.effective_version AS effective_version ORDER BY effective_version"
        ),
        max_rows=1,
        row_key=("effective_version",),
    ),
    CatalogEntry(
        id="table_blast_radius",
        ref="A",
        section="impact",
        title="Blast radius: what is affected if I change this table",
        question="Which queries, destinations and access patterns are affected if this table changes?",
        cypher=(
            "MATCH (st:SourceTable {id: $table_id})<-[:READS_FROM]-(q:Query) "
            "OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) "
            "OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) "
            "OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_QUERY]->(q) "
            "RETURN st.id AS table_id, q.id AS query_id, q.calls_per_second AS cps, "
            "collect(DISTINCT d.id) AS destinations, collect(DISTINCT ap.id) AS access_patterns, "
            "collect(DISTINCT a.anti_pattern_type) AS anti_patterns ORDER BY cps DESC, query_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(
                NodeRef("table_id", "SourceTable"),
                NodeRef("query_id", "Query"),
                NodeRef("destinations", "Destination"),
                NodeRef("access_patterns", "AccessPattern"),
            ),
            edges=(
                EdgeRef("query_id", "table_id", "READS_FROM"),
                EdgeRef("query_id", "destinations", "MIGRATES_TO"),
                EdgeRef("query_id", "access_patterns", "PART_OF"),
            ),
        ),
        params=(TABLE_PARAM,),
        row_key=("query_id",),
    ),
    CatalogEntry(
        id="engine_destinations",
        ref="B",
        section="impact",
        title="Everything going to a given engine",
        question="Which queries, source tables and access patterns does this engine get?",
        cypher=(
            "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination {engine: $engine}) "
            "OPTIONAL MATCH (q)-[:PART_OF]->(ap:AccessPattern) "
            "OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) "
            "RETURN d.id AS destination, collect(DISTINCT st.id) AS source_tables, "
            "collect(DISTINCT ap.id) AS access_patterns, count(DISTINCT q) AS queries "
            "ORDER BY queries DESC, destination"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(
                NodeRef("destination", "Destination"),
                NodeRef("source_tables", "SourceTable"),
                NodeRef("access_patterns", "AccessPattern"),
            ),
            edges=(
                EdgeRef("source_tables", "destination", "migrates to"),
                EdgeRef("destination", "access_patterns", "serves"),
            ),
        ),
        params=(DESTINATION_ENGINE_PARAM,),
        needs=(ASSIGNMENT,),
        row_key=("destination",),
        note=(
            "A query's `destination` is its engine (#306); where it lands in the design "
            "is `TARGETS` (questions L and M)."
        ),
    ),
    CatalogEntry(
        id="query_provenance",
        ref="C",
        section="impact",
        title="Full provenance for one query: why did it go where it went",
        question="Where did this query go, with what confidence, and what informed it?",
        cypher=(
            "MATCH (q:Query {id: $query_id}) "
            "OPTIONAL MATCH (q)-[m:MIGRATES_TO]->(d:Destination) "
            "OPTIONAL MATCH (q)-[:EMITS_SIGNAL]->(s:Signal) "
            "OPTIONAL MATCH (dec:Decision)-[:INFORMED_BY]->(q) "
            "OPTIONAL MATCH (q)-[:MEMBER_OF]->(g:CoDependencyGroup) "
            "RETURN q.id AS query_id, d.id AS destination, m.confidence AS confidence, "
            "m.assignment_reason AS reason, collect(DISTINCT s.id) AS signals, "
            "collect(DISTINCT dec.id) AS decisions, "
            "collect(DISTINCT g.id) AS co_dependency_groups ORDER BY destination"
        ),
        max_rows=50,
        projection=Projection(
            nodes=(
                NodeRef("query_id", "Query"),
                NodeRef("destination", "Destination"),
                NodeRef("signals", "Signal"),
                NodeRef("decisions", "Decision"),
                NodeRef("co_dependency_groups", "CoDependencyGroup"),
            ),
            edges=(
                EdgeRef("query_id", "destination", "MIGRATES_TO"),
                EdgeRef("query_id", "signals", "EMITS_SIGNAL"),
                EdgeRef("decisions", "query_id", "INFORMED_BY"),
                EdgeRef("query_id", "co_dependency_groups", "MEMBER_OF"),
            ),
        ),
        params=(QUERY_PARAM,),
        row_key=("destination",),
    ),
    CatalogEntry(
        id="risk_hotspots",
        ref="D",
        section="impact",
        title="Migration risk hotspots: high traffic and risk and anti-patterns",
        question="Which busy tables carry risks or anti-patterns?",
        # Traffic is summed before the optional matches, so each query counts
        # once, not once per risk and anti-pattern on its table.
        cypher=(
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query) "
            "WITH st, sum(q.calls_per_second) AS total_cps "
            "OPTIONAL MATCH (r:Risk {status: 'active'})-[:IMPACTS]->(st) "
            "WITH st, total_cps, count(DISTINCT r) AS risks "
            "OPTIONAL MATCH (a:AntiPattern)-[:OBSERVED_IN_TABLE]->(st) "
            "WITH st, total_cps, risks, count(DISTINCT a) AS anti_patterns "
            "WHERE risks > 0 OR anti_patterns > 0 "
            "RETURN st.id AS table_id, total_cps, risks, anti_patterns "
            "ORDER BY total_cps DESC, table_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(nodes=(NodeRef("table_id", "SourceTable"),)),
        needs=(ANALYSIS, SYNTHESIS),
        row_key=("table_id",),
    ),
    CatalogEntry(
        id="cross_engine_tables",
        ref="E",
        section="impact",
        title="Cross-engine tables: a table split across more than one engine",
        question="Which source tables are served by more than one engine?",
        cypher=(
            "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable), (q)-[:MIGRATES_TO]->(d:Destination) "
            "WITH st, count(DISTINCT d.engine) AS engine_count, "
            "collect(DISTINCT d.engine) AS engines WHERE engine_count > 1 "
            "RETURN st.id AS table_id, engine_count, engines "
            "ORDER BY engine_count DESC, table_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("table_id", "SourceTable"), NodeRef("engines", "Engine")),
            edges=(EdgeRef("table_id", "engines", "served by"),),
        ),
        needs=(ASSIGNMENT,),
        row_key=("table_id",),
    ),
    CatalogEntry(
        id="load_test_regressions",
        ref="F",
        section="impact",
        title="Load-test regressions: queries that got worse after migration",
        question="Which queries were slower on the target than on the source in the load test?",
        cypher=(
            "MATCH (q:Query)-[:TESTED_IN]->(lt:LoadTestRun) WHERE lt.improvement_factor < 1.0 "
            "RETURN q.id AS query_id, lt.id AS run_id, lt.engine AS engine, "
            "lt.source_p90 AS source_p90, lt.target_p90 AS target_p90, "
            "lt.improvement_factor AS improvement_factor ORDER BY improvement_factor, query_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("query_id", "Query"), NodeRef("run_id", "LoadTestRun")),
            edges=(EdgeRef("query_id", "run_id", "TESTED_IN"),),
        ),
        needs=(LOAD_TEST,),
        note="An empty result after a load test ran means no query regressed.",
        row_key=("query_id",),
    ),
    CatalogEntry(
        id="query_moves",
        ref="G",
        section="impact",
        title="Routing history of one query: every Move",
        question="Which steps moved this query between engines, and why?",
        cypher=(
            "MATCH (q:Query {id: $query_id})-[m:MOVED]->(e:Engine) "
            "RETURN q.id AS query_id, m.version AS version, m.seq AS seq, m.pass AS pass, "
            "m.from_engine AS from_engine, e.id AS to_engine, m.reason_code AS reason_code, "
            "m.score_before AS score_before, m.score_after AS score_after, "
            "m.runner_up_engine AS runner_up_engine, m.runner_up_score AS runner_up_score "
            "ORDER BY version, seq"
        ),
        max_rows=200,
        projection=Projection(
            nodes=(NodeRef("query_id", "Query"), NodeRef("to_engine", "Engine")),
            edges=(EdgeRef("query_id", "to_engine", "MOVED"),),
        ),
        params=(QUERY_PARAM,),
        needs=(MOVE_LEDGER,),
        note=(
            "The curated `GET .../graph/queries/{query_id}/trace` endpoint "
            "explains the same Moves with each engine's score."
        ),
        row_key=("version", "seq"),
    ),
    CatalogEntry(
        id="query_decisions",
        ref="H",
        section="impact",
        title="Design decisions about one query, with the agent that made them",
        question="Which design decisions name this query, and which agent made each?",
        cypher=(
            "MATCH (dec:Decision)-[:INFORMED_BY]->(q:Query {id: $query_id}) "
            "OPTIONAL MATCH (dec)-[:PRODUCED_BY]->(a:Agent) "
            "RETURN q.id AS query_id, dec.id AS decision_id, dec.category AS category, "
            "dec.description AS description, a.id AS agent_id, a.phase AS phase "
            "ORDER BY decision_id, agent_id"
        ),
        max_rows=200,
        projection=Projection(
            nodes=(
                NodeRef("query_id", "Query"),
                NodeRef("decision_id", "Decision"),
                NodeRef("agent_id", "Agent"),
            ),
            edges=(
                EdgeRef("decision_id", "query_id", "INFORMED_BY"),
                EdgeRef("decision_id", "agent_id", "PRODUCED_BY"),
            ),
        ),
        params=(DECIDED_QUERY_PARAM,),
        needs=(QUERY_DECISIONS,),
        row_key=("decision_id", "agent_id"),
    ),
    CatalogEntry(
        id="access_pattern_queries",
        ref="I",
        section="impact",
        title="Queries behind one access pattern",
        question="Which queries does this access pattern serve, and how did their load tests go?",
        cypher=(
            "MATCH (ap:AccessPattern {id: $access_pattern_id})<-[:PART_OF]-(q:Query) "
            "OPTIONAL MATCH (q)-[:TESTED_IN]->(lt:LoadTestRun) "
            "WHERE lt.engine = ap.engine AND lt.schema_version = ap.schema_version "
            "RETURN ap.id AS pattern, ap.engine AS engine, ap.operation AS operation, "
            "q.id AS query_id, q.calls_per_second AS cps, lt.id AS run_id, "
            "lt.improvement_factor AS improvement_factor ORDER BY cps DESC, query_id, run_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(
                NodeRef("pattern", "AccessPattern"),
                NodeRef("query_id", "Query"),
                NodeRef("run_id", "LoadTestRun"),
            ),
            edges=(
                EdgeRef("query_id", "pattern", "PART_OF"),
                EdgeRef("query_id", "run_id", "TESTED_IN"),
            ),
        ),
        params=(ACCESS_PATTERN_PARAM,),
        needs=(SCHEMA_DESIGN,),
        row_key=("query_id", "run_id"),
    ),
    CatalogEntry(
        id="version_chain",
        ref="J",
        section="impact",
        title="Assignment version chain",
        question=(
            "Which assignment versions does the lineage hold, which pass made each, "
            "and which one is effective?"
        ),
        cypher=(
            "MATCH (v:AssignmentVersion) "
            "OPTIONAL MATCH (v)-[:SUPERSEDES]->(p:AssignmentVersion) "
            "OPTIONAL MATCH (v)-[:RESTORES]->(r:AssignmentVersion) "
            "RETURN v.id AS version_id, v.version AS version, v.kind AS kind, "
            "v.status AS status, v.effective AS effective, v.move_count AS move_count, "
            "p.id AS supersedes, r.id AS restores ORDER BY version, version_id"
        ),
        max_rows=200,
        projection=Projection(
            nodes=(
                NodeRef("version_id", "AssignmentVersion"),
                NodeRef("supersedes", "AssignmentVersion"),
                NodeRef("restores", "AssignmentVersion"),
            ),
            edges=(
                EdgeRef("version_id", "supersedes", "SUPERSEDES"),
                EdgeRef("version_id", "restores", "RESTORES"),
            ),
        ),
        needs=(VERSION_CHAIN,),
        row_key=("version_id",),
    ),
    CatalogEntry(
        id="version_changes",
        ref="K",
        section="impact",
        title="What one assignment version changed",
        question="Which queries did this assignment version move, from which engines and to which?",
        cypher=(
            "MATCH (v:AssignmentVersion {id: $version_id})-[a:AFFECTS]->(q:Query) "
            "OPTIONAL MATCH (q)-[m:MOVED]->(e:Engine) WHERE m.version = v.version "
            "RETURN v.id AS version_id, q.id AS query_id, a.role AS role, "
            "collect(DISTINCT m.from_engine) AS from_engines, "
            "collect(DISTINCT e.id) AS to_engines ORDER BY role, query_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(
                NodeRef("version_id", "AssignmentVersion"),
                NodeRef("query_id", "Query"),
                NodeRef("to_engines", "Engine"),
            ),
            edges=(
                EdgeRef("version_id", "query_id", "AFFECTS"),
                EdgeRef("query_id", "to_engines", "MOVED"),
            ),
        ),
        params=(VERSION_PARAM,),
        needs=(VERSION_CHAIN,),
        row_key=("query_id",),
        note="Role `moved` is a query the version's Moves changed; `tie` one whose tie alone changed.",
    ),
    CatalogEntry(
        id="query_targets",
        ref="L",
        section="impact",
        title="Where one query lands in the design",
        question="Which design objects does this query land on, per assignment version?",
        cypher=(
            "MATCH (q:Query {id: $query_id})-[t:TARGETS]->(o:TargetObject) "
            "RETURN q.id AS query_id, t.version AS version, t.effective AS effective, "
            "o.id AS target_id, o.engine AS engine, o.kind AS kind, o.name AS name, "
            "o.parent AS parent, t.schema_version AS schema_version, o.fallback AS fallback "
            "ORDER BY version DESC, target_id"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(NodeRef("query_id", "Query"), NodeRef("target_id", "TargetObject")),
            edges=(EdgeRef("query_id", "target_id", "TARGETS"),),
        ),
        params=(QUERY_PARAM,),
        needs=(ASSIGNMENT,),
        row_key=("version", "target_id"),
        note=(
            "A `fallback` target is no design object: `not_designed` (no design for the "
            "engine at that version), `out_of_scope` (the design does not serve the "
            "query) or `not_in_design` (no access pattern for it in the design)."
        ),
    ),
    CatalogEntry(
        id="target_queries",
        ref="M",
        section="impact",
        title="The queries one design object serves",
        question="Which queries does this table, index, collection or key pattern serve?",
        cypher=(
            "MATCH (o:TargetObject {id: $target_id}) "
            "MATCH (q:Query)-[t:TARGETS]->(x:TargetObject) "
            "WHERE x.id = o.id OR (o.kind = 'table' AND x.kind = 'gsi' "
            "AND x.engine = o.engine AND x.parent = o.name "
            "AND x.schema_version = o.schema_version) "
            "RETURN o.id AS target_id, q.id AS query_id, x.id AS via, t.version AS version, "
            "t.effective AS effective, t.pattern_ids_json AS access_patterns, "
            "q.calls_per_second AS calls_per_second "
            "ORDER BY calls_per_second DESC, query_id, version, via"
        ),
        max_rows=MAX_ROWS,
        projection=Projection(
            nodes=(
                NodeRef("target_id", "TargetObject"),
                NodeRef("via", "TargetObject"),
                NodeRef("query_id", "Query"),
            ),
            edges=(EdgeRef("query_id", "via", "TARGETS"),),
        ),
        params=(TARGET_PARAM,),
        needs=(DESIGN_TARGETS,),
        row_key=("query_id", "version", "via"),
        note=(
            "For a DynamoDB table, the queries that read one of its GSIs are listed too, "
            "with `via` the GSI; otherwise `via` is the object itself. A cache key pattern "
            "lists the reads it serves, not the writes that invalidate it."
        ),
    ),
    CatalogEntry(
        id="query_scores_now",
        ref="N",
        section="impact",
        title="One query's score on every engine now",
        question="What did routing score this query on every engine, in the effective version?",
        cypher=(
            "MATCH (q:Query {id: $query_id})-[s:SCORED {current: true}]->(e:Engine) "
            "RETURN q.id AS query_id, e.id AS engine, s.score AS score, s.gate AS gate, "
            "s.assigned AS assigned, s.ladder_version AS ladder_version, s.pass AS pass, "
            "s.ladder_copied_from AS ladder_copied_from ORDER BY engine"
        ),
        max_rows=50,
        projection=Projection(
            nodes=(NodeRef("query_id", "Query"), NodeRef("engine", "Engine")),
            edges=(EdgeRef("query_id", "engine", "SCORED"),),
        ),
        params=(QUERY_PARAM,),
        needs=(SCORED_LADDERS,),
        row_key=("engine",),
        note=(
            "The ladder comes from `ladder_version`'s Move (`pass`); a rollback's ladder is "
            "copied from the abandoned reroute `ladder_copied_from`."
        ),
    ),
)

_BY_ID = {entry.id: entry for entry in CATALOG}


def entries() -> tuple[CatalogEntry, ...]:
    """Every catalog entry, in rendering order."""
    return CATALOG


def get_entry(entry_id: str) -> CatalogEntry:
    """The entry with this id; ``KeyError`` when there is none."""
    return _BY_ID[entry_id]


def bounded_cypher(entry: CatalogEntry) -> str:
    """The entry's Cypher with a trailing LIMIT of at most ``max_rows + 1``."""
    return bound_result_rows(entry.cypher, entry.max_rows)


def check_params(entry: CatalogEntry, params: Mapping[str, object]) -> dict[str, str]:
    """The parameters as strings; ValueError when one is missing, unknown or not a string."""
    names = {p.name for p in entry.params}
    missing = sorted(names - set(params))
    if missing:
        raise ValueError(f"{entry.id}: missing parameter(s): {', '.join(missing)}")
    unknown = sorted(set(params) - names)
    if unknown:
        raise ValueError(f"{entry.id}: unknown parameter(s): {', '.join(unknown)}")
    bad = sorted(name for name, value in params.items() if not isinstance(value, str))
    if bad:
        raise ValueError(f"{entry.id}: parameter(s) must be a string id: {', '.join(bad)}")
    return {name: str(value) for name, value in params.items()}


def run_entry(
    store: GraphStore, entry: CatalogEntry, params: Mapping[str, object] | None = None
) -> CatalogResult:
    """Run one entry with its parameters bound, returning at most ``max_rows`` rows."""
    bound = check_params(entry, params or {})
    rows, truncated = store.query_bounded(
        bounded_cypher(entry), bound or None, max_rows=entry.max_rows
    )
    return CatalogResult(rows=rows, truncated=truncated)


class Choices(list[str]):
    """A parameter's choices; ``truncated`` is True when the graph holds more."""

    truncated: bool = False


def param_choices(store: GraphStore, param: Param) -> Choices:
    """The values this parameter can take on this graph (at most ``MAX_CHOICES``)."""
    rows, truncated = store.query_bounded(
        bound_result_rows(param.choices_cypher, MAX_CHOICES), None, max_rows=MAX_CHOICES
    )
    choices = Choices(str(r["value"]) for r in rows if r.get("value") is not None)
    choices.truncated = truncated
    return choices


def bounded_probe(need: Need) -> str:
    """The need's probe, filtered as a read statement and bounded to one row."""
    return bound_result_rows(need.probe_cypher, 1)


def unmet_needs(store: GraphStore, entry: CatalogEntry) -> list[Need]:
    """The entry's needs whose stage left nothing in this graph."""
    unmet = []
    for need in entry.needs:
        rows, _ = store.query_bounded(bounded_probe(need), None, max_rows=1)
        if not rows or not rows[0].get("n"):
            unmet.append(need)
    return unmet


def value_exists(store: GraphStore, param: Param, value: str) -> bool:
    """True when ``value`` is one of ``param``'s choices on this graph.

    One bounded, one-row lookup, so a value is checked however many choices
    the graph holds (``param_choices`` lists at most ``MAX_CHOICES``).
    """
    rows, _ = store.query_bounded(
        bound_result_rows(param.exists_cypher, 1), {"value": value}, max_rows=1
    )
    return bool(rows)


def unlisted_params(store: GraphStore, entry: CatalogEntry, params: Mapping[str, str]) -> list[str]:
    """The names of the parameters whose value is not among their choices on this graph."""
    return [
        param.name
        for param in entry.params
        if not value_exists(store, param, str(params.get(param.name, "")))
    ]


def need_summary(need: Need) -> dict[str, str]:
    """A need as plain data: its id, stage and what the stage writes."""
    return {"id": need.id, "stage": need.stage, "produces": need.produces}


def explain_empty(unmet: list[Need]) -> str:
    """Why an entry returned no rows, naming the stages the job has not run."""
    if not unmet:
        return "No rows: the graph holds the data this question needs, and none matched."
    stages = "; ".join(f"{n.stage} ({n.produces})" for n in unmet)
    return f"No rows: this job's graph has no data from: {stages}."


def _ids(value: object) -> Iterable[str]:
    values = value if isinstance(value, list) else [value]
    return (str(v) for v in values if v is not None and v != "")


def project(entry: CatalogEntry, rows: Iterable[Mapping[str, object]]) -> dict[str, list[dict]]:
    """Turn result rows into ``{"nodes": [{id, label}], "edges": [{source, target, label}]}``.

    A column may hold one id or a list of ids; nulls and empty values are
    skipped, and repeated nodes and edges are listed once.
    """
    nodes: dict[tuple[str, str], dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    labels = {n.column: n.label for n in entry.projection.nodes}
    for row in rows:
        for column, label in labels.items():
            for node_id in _ids(row.get(column)):
                nodes.setdefault((label, node_id), {"id": node_id, "label": label})
        for edge in entry.projection.edges:
            for source in _ids(row.get(edge.source)):
                for target in _ids(row.get(edge.target)):
                    key = (source, target, edge.label)
                    edges.setdefault(key, {"source": source, "target": target, "label": edge.label})
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
