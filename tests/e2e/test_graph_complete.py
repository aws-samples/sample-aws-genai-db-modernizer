# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The complete context graph on every sample run (#306).

Each node and edge type the run's artifacts call for is present, and the types hold
together: the relations below, not counts that drift with routing. Waves, units,
sources of truth and the cache layer equal the explorer's overlays (the code they
are built with).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.graph.explorer_overlays import read_overlays
from src.graph.populators import rebuild_graph
from src.graph.schema import NODE_TABLES, REL_TABLES, initialize_schema
from src.graph.store import GraphStore
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult


def _labels(ddls: list[str]) -> list[str]:
    return [d.split("EXISTS", 1)[1].split("(", 1)[0].strip() for d in ddls]


# Present on every sample run: collected, routed, versioned, synthesized.
ALWAYS = {
    "nodes": {
        "Query",
        "SourceTable",
        "Destination",
        "Engine",
        "Signal",
        "AntiPattern",
        "Decision",
        "Risk",
        "Agent",
        "GraphMeta",
        "AssignmentVersion",
        "TargetObject",
        "Wave",
    },
    "edges": {
        "READS_FROM",
        "WRITES",
        "MIGRATES_TO",
        "HOSTED_ON",
        "EMITS_SIGNAL",
        "MOVED",
        "SCORED",
        "TARGETS",
        "SUPERSEDES",
        "AFFECTS",
        "INFORMED_BY",
        "IMPACTS",
        "EVIDENCED_BY",
        "PRODUCED_BY",
        "OBSERVED_IN_QUERY",
        "MOVES_IN",
        "INCLUDES",
        "SOURCE_OF_TRUTH",
        "READS_REPLICA",
    },
}


def _count(store, cypher):
    return store.query(cypher)[0]["n"]


def test_the_graph_is_complete_and_consistent(run: PipelineResult, tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    artifacts = LocalArtifactStore(base_dir=str(root))
    graph = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph)
    rebuild_graph(run.db, run.job_id, artifacts, graph)
    version = resolve_effective_assignment_version(artifacts, run.db, run.job_id)
    overlays = read_overlays(artifacts, run.db, run.job_id, version)

    nodes = {
        label: _count(graph, f"MATCH (n:{label}) RETURN count(n) AS n")  # noqa: S608 - schema label
        for label in _labels(NODE_TABLES)
    }
    edges = {
        rel: _count(graph, f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n")  # noqa: S608
        for rel in _labels(REL_TABLES)
    }
    assert {k for k, n in nodes.items() if n} >= ALWAYS["nodes"], nodes
    assert {k for k, n in edges.items() if n} >= ALWAYS["edges"], edges
    # Present when the run has them: units, a cache layer, cutover waves.
    assert bool(nodes["MoveUnit"]) == bool(overlays.query_units)
    assert bool(edges["CACHED_BY"]) == bool(overlays.query_cache)
    assert bool(edges["CUTS_OVER_IN"]) == bool(overlays.cutover)

    def single(cypher):
        return graph.query(cypher)

    # Every query: one MIGRATES_TO, at least one READS_FROM, a TARGETS in the
    # effective version, one current assigned SCORED engine at most.
    assert (
        single(
            "MATCH (q:Query) OPTIONAL MATCH (q)-[m:MIGRATES_TO]->() WITH q, count(m) AS n "
            "WHERE n <> 1 RETURN q.id"
        )
        == []
    )
    assert single("MATCH (q:Query) WHERE NOT (q)-[:READS_FROM]->() RETURN q.id") == []
    assert (
        single(
            "MATCH (q:Query) WHERE NOT EXISTS { MATCH (q)-[t:TARGETS]->() WHERE t.effective } "
            "RETURN q.id"
        )
        == []
    )
    assert (
        single(
            "MATCH (q:Query)-[s:SCORED]->() WHERE s.current AND s.assigned WITH q, count(s) AS n "
            "WHERE n > 1 RETURN q.id"
        )
        == []
    )
    # A written table is also read (READS_FROM is every accessed table).
    assert (
        single(
            "MATCH (q:Query)-[:WRITES]->(st:SourceTable) WHERE NOT (q)-[:READS_FROM]->(st) "
            "RETURN q.id"
        )
        == []
    )
    # One effective version, and the version chain ends on it.
    assert _count(graph, "MATCH (v:AssignmentVersion {effective: true}) RETURN count(v) AS n") == 1
    # A query moves in a wave of its own engine; a table's source of truth is the
    # engine its writers migrate to.
    assert (
        single(
            "MATCH (q:Query)-[:MOVES_IN]->(w:Wave)-[:INCLUDES]->(e:Engine) "
            "WITH q, w, collect(e.id) AS engines MATCH (q)-[:MIGRATES_TO]->(d:Destination) "
            "WHERE w.effective AND NOT d.engine IN engines RETURN q.id"
        )
        == []
    )
    assert (
        single(
            "MATCH (q:Query)-[:WRITES]->(st:SourceTable)-[s:SOURCE_OF_TRUTH]->(e:Engine), "
            "(q)-[:MIGRATES_TO]->(d:Destination) WHERE s.effective AND d.engine <> e.id "
            "RETURN q.id, st.id"
        )
        == []
    )
    # Waves and moves equal the explorer's overlays.
    moves = {
        r["q"]: r["w"]
        for r in graph.query(
            "MATCH (q:Query)-[:MOVES_IN]->(w:Wave {effective: true}) RETURN q.id AS q, w.wave AS w"
        )
    }
    assert moves == overlays.query_move_wave
    costs = {
        r["e"]: r["c"]
        for r in graph.query(
            "MATCH (e:Engine) WHERE e.monthly_cost_usd IS NOT NULL "
            "RETURN e.id AS e, e.monthly_cost_usd AS c"
        )
    }
    assert costs == overlays.engine_costs
    graph.close()


def _job_graph(run: PipelineResult, tmp_path: Path):
    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    artifacts = LocalArtifactStore(base_dir=str(root))
    graph = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph)
    rebuild_graph(run.db, run.job_id, artifacts, graph)
    version = resolve_effective_assignment_version(artifacts, run.db, run.job_id)
    return artifacts, graph, version


def test_the_wave_entry_is_the_explorers_wave_list(run: PipelineResult, tmp_path: Path) -> None:
    """Catalog O lists each wave's moves first, then its cached reads, exactly the
    explorer's wave list, and never the cutover passengers (discourse's first wave cuts
    over hundreds of queries)."""
    from src.graph import catalog
    from src.graph.explorer_overlays import wave_query_ids

    artifacts, graph, version = _job_graph(run, tmp_path)
    overlays = read_overlays(artifacts, run.db, run.job_id, version)
    entry = catalog.get_entry("wave_queries")
    for w in overlays.plan:
        rows = catalog.run_entry(graph, entry, {"wave_id": f"v{version}:wave-{w['wave']}"}).rows
        listed = wave_query_ids(overlays, w["wave"])
        moves = sorted(q for q, n in overlays.query_move_wave.items() if n == w["wave"])
        assert len(listed) <= catalog.MAX_ROWS, "the wave list fits the entry's bound"
        assert {r["query_id"] for r in rows} == set(listed), w["wave"]
        assert [r["query_id"] for r in rows[: len(moves)]] == moves
    graph.close()


def test_the_stored_figures_equal_their_sources(run: PipelineResult, tmp_path: Path) -> None:
    """The graph stores two figures (ADR-032 Decision 1): a move unit's size and a
    wave's query count. Each equals its source on every sample: routing's placement
    groups and the wave plan. A wave's query count is its moves plus its cached reads."""
    from src.agents.referee.write_groups import assignment_placement_groups

    artifacts, graph, version = _job_graph(run, tmp_path)
    assignment = artifacts.read_json(f"{run.db}/{run.job_id}/assignment/v{version}/assignment.json")
    groups = [
        sorted(str(q) for q in g) for g in assignment_placement_groups(assignment) if len(g) >= 2
    ]
    units = {}
    for r in graph.query(
        "MATCH (q:Query)-[:MEMBER_OF]->(u:MoveUnit {effective: true}) "
        "RETURN u.id AS u, u.size AS size, q.id AS q"
    ):
        units.setdefault(r["u"], (r["size"], set()))[1].add(r["q"])
    assert sorted(sorted(qs) for _, qs in units.values()) == sorted(groups)
    assert all(size == len(qs) for size, qs in units.values())

    report = read_overlays(artifacts, run.db, run.job_id, version)
    rows = graph.query(
        "MATCH (w:Wave {effective: true}) "
        "OPTIONAL MATCH (m:Query)-[:MOVES_IN]->(w) WITH w, count(DISTINCT m) AS moves "
        "OPTIONAL MATCH (c:Query)-[:CACHED_IN]->(w) "
        "RETURN w.wave AS wave, w.query_count AS qc, moves, count(DISTINCT c) AS cached"
    )
    plan = {w["wave"]: w["query_count"] for w in report.plan}
    assert {r["wave"]: r["qc"] for r in rows} == plan
    assert {r["wave"]: r["moves"] + r["cached"] for r in rows} == plan
    graph.close()
