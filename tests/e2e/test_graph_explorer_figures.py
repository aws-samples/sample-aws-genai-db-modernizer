# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The experimental graph explorer's figures agree with the report (#308, ADR-032 Decision 1).

The explorer reads some figures from ``report.json`` (each engine's workload share and
monthly cost, the wave plan's groups) and works the rest out from the context graph and the
assignment at read time (bundle totals, per-table traffic, unit sizes, each engine's and each
wave's queries). Every graph-derived figure that a deliverable also states must equal it, on
every sample: each engine's query count against the wave plan's per-wave query count, the
queries each wave moves or caches, the cached reads, and the in-scope total; and the
walkthroughs' engine and group figures are the report's own.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.graph import explorer
from src.graph.explorer_overlays import Overlays, read_overlays, wave_query_ids
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.assignment_versioning import (
    resolve_effective_assignment_version,
    synthesis_report_candidates,
)
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e


@pytest.fixture
def job(run: PipelineResult, tmp_path: Path) -> Iterator[tuple[GraphStore, Overlays, dict]]:
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    graph = GraphStore(str(tmp_path / "context.lbug"))
    try:
        initialize_schema(graph)
        rebuild_graph(run.db, run.job_id, store, graph)
        version = resolve_effective_assignment_version(store, run.db, run.job_id)
        overlays = read_overlays(store, run.db, run.job_id, version)
        report = store.read_json(synthesis_report_candidates(store, run.db, run.job_id)[0])
        yield graph, overlays, report
    finally:
        graph.close()


def test_each_waves_queries_equal_the_wave_plan(job) -> None:
    graph, overlays, report = job
    counts = {e.id: e.query_count for e in explorer.explorer_options(graph).engines}
    cache = (report["facts"].get("cache_layer") or {}).get("engine")
    plan = [w for w in report["migration_waves"] if w.get("query_count") is not None]
    assert plan
    for wave in plan:
        # The queries the explorer draws in the wave: what moves in it, or the reads its cache fronts.
        assert len(wave_query_ids(overlays, wave["wave"])) == wave["query_count"], wave["wave"]
        if cache not in wave["engines"]:
            # The engine picker's query counts, for the engines that wave moves to.
            assert sum(counts.get(e, 0) for e in wave["engines"]) == wave["query_count"], wave
    assert sum(counts.values()) == report["facts"]["in_scope_query_count"]


def test_the_cached_and_in_scope_queries_equal_the_facts(job) -> None:
    _, overlays, report = job
    facts = report["facts"]
    assert len(overlays.query_cached_in) == facts["cache_layer"]["designed_read_count"]
    assert len(overlays.query_move_wave) == facts["in_scope_query_count"]


def test_the_walkthroughs_state_the_reports_own_figures(job) -> None:
    graph, overlays, report = job
    engines = {e["engine"]: e for e in report["facts"]["engines"]}
    groups = {
        f"{w['wave']}:{i}": g
        for w in report["migration_waves"]
        for i, g in enumerate(w.get("table_groups") or [])
    }
    steps = [s for w in explorer.walkthroughs(graph, overlays).walkthroughs for s in w.steps]
    engine_steps = [s for s in steps if s.kind == "engine"]
    group_steps = [s for s in steps if s.kind == "group"]
    assert engine_steps and group_steps
    for step in engine_steps:
        fact = engines[step.engine]
        assert step.workload_percent == fact.get("workload_percent"), step.engine
        assert step.monthly_cost_usd == fact.get("monthly_cost_usd"), step.engine
    for step in group_steps:
        group = groups[step.start_id]
        assert step.query_count == group.get("query_count"), step.start_id
        assert step.table_count == len(group["tables"]), step.start_id


def test_who_writes_each_table_equals_the_reports_writers(job) -> None:
    """The graph's writers of each table (WRITES, #306) are the report's
    ``table_sources[].write_query_ids``: both come from routing's one-writer rule.
    Held on every sample whose report states the table sources."""
    store, _, report = job
    sources = report.get("table_sources")
    if not sources:
        return  # the report states no table sources: nothing to hold equal
    graph: dict[str, set[str]] = {}
    for r in store.query(
        "MATCH (q:Query)-[:WRITES]->(st:SourceTable) RETURN st.id AS t, q.id AS q"
    ):
        graph.setdefault(r["t"], set()).add(r["q"])
    stated = {ts["table"]: set(ts.get("write_query_ids") or []) for ts in sources}
    assert graph == stated
