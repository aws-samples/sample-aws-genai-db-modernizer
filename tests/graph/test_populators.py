"""Tests for graph populators — validates nodes/edges created from artifacts."""

from src.graph.populators import (
    populate_from_analysis,
    populate_from_assignment,
    populate_from_collector,
    populate_from_load_test,
    populate_from_post_schema_router,
    populate_from_reality_check,
    populate_from_schema_design,
    populate_from_synthesis,
    populate_from_triage,
)


def test_populate_from_collector_creates_queries(graph_store, sample_collector_output):
    """Collector output produces Query nodes."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query("MATCH (q:Query) RETURN q.id ORDER BY q.id")
    assert len(results) == 3
    assert results[0]["q.id"] == "q1"
    assert results[1]["q.id"] == "q2"
    assert results[2]["q.id"] == "q3"


def test_populate_from_collector_creates_source_tables(graph_store, sample_collector_output):
    """Collector output produces SourceTable nodes (deduplicated)."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query("MATCH (st:SourceTable) RETURN st.id ORDER BY st.id")
    assert len(results) == 2
    assert results[0]["st.id"] == "customers"
    assert results[1]["st.id"] == "orders"


def test_populate_from_collector_creates_reads_from_edges(graph_store, sample_collector_output):
    """Collector output produces READS_FROM edges."""
    populate_from_collector(sample_collector_output, graph_store)
    results = graph_store.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) " "RETURN q.id, st.id ORDER BY q.id, st.id"
    )
    # q1 -> orders, q2 -> customers, q2 -> orders, q3 -> orders
    assert len(results) == 4


def test_populate_from_triage_creates_signals(
    graph_store, sample_collector_output, sample_triage_output
):
    """Triage output produces Signal nodes."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    results = graph_store.query("MATCH (s:Signal) RETURN s.id ORDER BY s.id")
    assert len(results) == 2
    assert results[0]["s.id"] == "complex_joins"
    assert results[1]["s.id"] == "key_value_lookups"


def test_populate_from_triage_creates_emits_signal_edges(
    graph_store, sample_collector_output, sample_triage_output
):
    """Triage output produces EMITS_SIGNAL edges linking queries to signals."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    results = graph_store.query(
        "MATCH (q:Query)-[:EMITS_SIGNAL]->(s:Signal) " "RETURN q.id, s.id ORDER BY q.id"
    )
    assert len(results) == 2
    assert results[0]["q.id"] == "q1"
    assert results[0]["s.id"] == "key_value_lookups"
    assert results[1]["q.id"] == "q2"
    assert results[1]["s.id"] == "complex_joins"


def test_populate_from_assignment_creates_destinations(
    graph_store, sample_collector_output, sample_assignment
):
    """Assignment produces Destination and Engine nodes, MIGRATES_TO edges."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    destinations = graph_store.query("MATCH (d:Destination) RETURN d.id ORDER BY d.id")
    assert len(destinations) >= 1
    engines = graph_store.query("MATCH (e:Engine) RETURN e.id ORDER BY e.id")
    assert len(engines) == 2  # dynamodb, documentdb
    migrates = graph_store.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination) RETURN q.id, d.engine"
    )
    assert len(migrates) == 3


def test_populate_from_assignment_creates_co_dependency_groups(
    graph_store, sample_collector_output, sample_assignment
):
    """Assignment produces CoDependencyGroup nodes and MEMBER_OF edges."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    groups = graph_store.query("MATCH (g:CoDependencyGroup) RETURN g.id")
    assert len(groups) == 1
    members = graph_store.query(
        "MATCH (q:Query)-[:MEMBER_OF]->(g:CoDependencyGroup) RETURN q.id ORDER BY q.id"
    )
    assert len(members) == 2
    assert members[0]["q.id"] == "q1"
    assert members[1]["q.id"] == "q3"


def test_populate_from_assignment_handles_list_of_lists_groups(
    graph_store, sample_collector_output
):
    """Real assignment artifacts store co_dependency_groups as bare lists of query ids."""
    populate_from_collector(sample_collector_output, graph_store)
    assignment = {
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "confidence": 0.9,
                "source_tables": ["orders"],
                "assignment_reason": "key-value",
            },
        ],
        "co_dependency_groups": [["q1", "q3"]],
    }
    populate_from_assignment(assignment, graph_store)
    groups = graph_store.query("MATCH (g:CoDependencyGroup) RETURN g.id")
    assert len(groups) == 1
    members = graph_store.query(
        "MATCH (q:Query)-[:MEMBER_OF]->(g:CoDependencyGroup) RETURN q.id ORDER BY q.id"
    )
    assert {m["q.id"] for m in members} == {"q1", "q3"}


def test_populate_from_analysis_creates_anti_patterns(
    graph_store, sample_collector_output, sample_analysis_output
):
    """Analysis output produces AntiPattern nodes linked to queries."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_analysis(sample_analysis_output, "dynamodb", graph_store)
    anti_patterns = graph_store.query("MATCH (ap:AntiPattern) RETURN ap.id, ap.anti_pattern_type")
    assert len(anti_patterns) == 1
    assert anti_patterns[0]["ap.anti_pattern_type"] == "hot-partition"
    edges = graph_store.query("MATCH (ap:AntiPattern)-[:OBSERVED_IN_QUERY]->(q:Query) RETURN q.id")
    assert edges[0]["q.id"] == "q1"


def test_populate_from_reality_check_creates_consolidation_decisions(
    graph_store, sample_reality_check_output
):
    """Reality check produces Decision nodes (consolidation category)."""
    populate_from_reality_check(sample_reality_check_output, graph_store)
    decisions = graph_store.query("MATCH (d:Decision) RETURN d.category, d.description")
    assert len(decisions) == 1
    assert decisions[0]["d.category"] == "consolidation"


def test_populate_from_schema_design_creates_trade_off_decisions(
    graph_store, sample_collector_output, sample_schema_design_output
):
    """Schema design produces Decision nodes (trade_off) linked to queries."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_schema_design(sample_schema_design_output, "documentdb", 1, graph_store)
    decisions = graph_store.query(
        "MATCH (d:Decision {category: 'trade_off'}) RETURN d.id, d.description"
    )
    assert len(decisions) == 1
    informed = graph_store.query("MATCH (d:Decision)-[:INFORMED_BY]->(q:Query) RETURN q.id")
    assert len(informed) == 1
    assert informed[0]["q.id"] == "q2"


def test_populate_from_schema_design_creates_access_patterns(
    graph_store, sample_collector_output, sample_schema_design_output
):
    """Schema design produces AccessPattern nodes and PART_OF edges from queries."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_schema_design(sample_schema_design_output, "dynamodb", 1, graph_store)

    patterns = graph_store.query(
        "MATCH (ap:AccessPattern) RETURN ap.id, ap.engine, ap.schema_version, "
        "ap.pattern_group, ap.design_rps"
    )
    assert len(patterns) == 1
    assert patterns[0]["ap.id"] == "DDB-AP-1"
    assert patterns[0]["ap.engine"] == "dynamodb"
    assert patterns[0]["ap.schema_version"] == 1
    assert patterns[0]["ap.pattern_group"] == "Order reads"

    part_of = graph_store.query(
        "MATCH (q:Query)-[:PART_OF]->(ap:AccessPattern {id: 'DDB-AP-1'}) "
        "RETURN q.id ORDER BY q.id"
    )
    assert [r["q.id"] for r in part_of] == ["q1", "q3"]


def test_populate_from_post_schema_router_creates_reroute_decisions(
    graph_store, sample_collector_output, sample_assignment, sample_router_output
):
    """Post-schema router produces Decision nodes (reroute)."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    populate_from_post_schema_router(sample_router_output, graph_store)
    decisions = graph_store.query("MATCH (d:Decision {category: 'reroute'}) RETURN d.description")
    assert len(decisions) == 1


def test_populate_from_load_test_creates_runs(
    graph_store, sample_collector_output, sample_load_test_output
):
    """Load test produces LoadTestRun nodes with TESTED_IN edges."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_load_test(sample_load_test_output, "dynamodb", 1, graph_store)
    runs = graph_store.query("MATCH (lt:LoadTestRun) RETURN lt.query_id, lt.improvement_factor")
    assert len(runs) == 1
    assert runs[0]["lt.improvement_factor"] == 15.0
    tested = graph_store.query("MATCH (q:Query)-[:TESTED_IN]->(lt:LoadTestRun) RETURN q.id")
    assert len(tested) == 1
    assert tested[0]["q.id"] == "q1"


def test_populate_from_load_test_stores_latency_percentiles(
    graph_store, sample_collector_output, sample_load_test_output
):
    """Latency percentiles persist as flattened DOUBLE columns per source/target."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_load_test(sample_load_test_output, "dynamodb", 1, graph_store)
    runs = graph_store.query(
        "MATCH (lt:LoadTestRun) RETURN lt.engine, lt.schema_version, "
        "lt.source_p50, lt.source_p99, lt.source_max, lt.target_p50, lt.target_p90"
    )
    assert len(runs) == 1
    assert runs[0]["lt.engine"] == "dynamodb"
    assert runs[0]["lt.schema_version"] == 1
    assert runs[0]["lt.source_p50"] == 45.0
    assert runs[0]["lt.source_p99"] == 132.0
    assert runs[0]["lt.source_max"] == 440.0
    assert runs[0]["lt.target_p50"] == 3.0
    assert runs[0]["lt.target_p90"] == 4.5


def test_populate_from_load_test_creates_validates_edge(
    graph_store, sample_collector_output, sample_assignment, sample_load_test_output
):
    """A LoadTestRun VALIDATES the destination its query migrates to."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_assignment(sample_assignment, graph_store)
    populate_from_load_test(sample_load_test_output, "dynamodb", 1, graph_store)
    validated = graph_store.query(
        "MATCH (lt:LoadTestRun)-[:VALIDATES]->(d:Destination) RETURN d.id"
    )
    assert len(validated) == 1
    assert validated[0]["d.id"] == "orders-dynamodb"


def test_populate_from_synthesis_creates_risks(
    graph_store, sample_collector_output, sample_synthesis_output
):
    """Synthesis produces Risk nodes linked to source tables."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_synthesis(sample_synthesis_output, graph_store)
    risks = graph_store.query("MATCH (r:Risk) RETURN r.id, r.severity")
    assert len(risks) == 1
    assert risks[0]["r.severity"] == "MEDIUM"
    impacts = graph_store.query(
        "MATCH (r:Risk)-[:IMPACTS]->(st:SourceTable) RETURN st.id ORDER BY st.id"
    )
    assert len(impacts) == 2


def test_rebuild_is_idempotent(graph_store, sample_collector_output, sample_triage_output):
    """Running populators twice produces the same node count."""
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    count1 = graph_store.query("MATCH (n) RETURN COUNT(n) AS c")[0]["c"]
    populate_from_collector(sample_collector_output, graph_store)
    populate_from_triage(sample_triage_output, graph_store)
    count2 = graph_store.query("MATCH (n) RETURN COUNT(n) AS c")[0]["c"]
    assert count1 == count2
