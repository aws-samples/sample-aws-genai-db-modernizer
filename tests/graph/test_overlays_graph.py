# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Waves, move units, sources of truth, the cache layer and engine cost in the
context graph (#306).

They are built by the explorer's own overlay code (``explorer_overlays.build_overlays``
on the inputs ``read_overlays`` reads), per assignment version with the effective one
marked, so the graph and the explorer cannot disagree. These tests hold the graph to
``read_overlays`` and to the artifacts on real fixtures.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.graph.explorer_overlays import query_path, read_overlays
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore

FIXTURES = Path(__file__).parents[1] / "fixtures"
OLD_JOB = FIXTURES / "old_jobs/v0.4.0-beta.2/wordpress/aa4e840a"
REAL_RUN = FIXTURES / "real_runs/wordpress-both"


def _graph(tmp_path, job: Path, report: Path | None = None):
    base = tmp_path / "artifacts"
    shutil.copytree(job, base / "wordpress" / "job")
    if report is not None:
        dest = base / "wordpress/job/synthesis/v2/report.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(report, dest)
    artifacts = LocalArtifactStore(str(base))
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", artifacts, store)
    version = resolve_effective_assignment_version(artifacts, "wordpress", "job")
    return store, read_overlays(artifacts, "wordpress", "job", version), version, base


@pytest.fixture(params=["old_job", "real_run"])
def job(request, tmp_path):
    if request.param == "old_job":
        store, overlays, version, base = _graph(tmp_path, OLD_JOB)
    else:
        store, overlays, version, base = _graph(
            tmp_path, REAL_RUN / "job", REAL_RUN / "report.json"
        )
    yield store, overlays, version, base / "wordpress" / "job"
    store.close()


def _pairs(store, cypher):
    return sorted(tuple(r.values()) for r in store.query(cypher))


def test_waves_are_the_wave_plan(job):
    store, overlays, version, _ = job
    rows = store.query(
        "MATCH (w:Wave {effective: true}) RETURN w.wave AS wave, w.version AS version, "
        "w.title AS title, w.rationale AS rationale, w.gate AS gate, "
        "w.engines_json AS engines, w.migrating AS migrating, w.query_count AS qc, "
        "w.cutover AS cutover ORDER BY wave"
    )
    plan = {w["wave"]: w for w in overlays.plan}
    facts = {w["wave"]: w for w in overlays.waves}
    assert [r["wave"] for r in rows] == sorted(plan) and rows
    for r in rows:
        assert r["version"] == version
        assert (r["title"], r["rationale"], r["gate"]) == (
            plan[r["wave"]]["title"],
            plan[r["wave"]]["rationale"],
            plan[r["wave"]]["gate"],
        )
        assert json.loads(r["engines"]) == plan[r["wave"]]["engines"]
        assert r["migrating"] == facts[r["wave"]]["migrating"]
        assert r["qc"] == plan[r["wave"]]["query_count"]
        assert r["cutover"] == (
            overlays.cutover is not None and overlays.cutover["wave"] == r["wave"]
        )
    engines = _pairs(
        store,
        "MATCH (w:Wave {effective: true})-[:INCLUDES]->(e:Engine) RETURN w.wave AS w, e.id AS e",
    )
    assert engines == sorted((w, e) for w, p in plan.items() for e in p["engines"])


def test_moves_cached_and_cutover_follow_the_overlays(job):
    store, overlays, _, _ = job
    moves = _pairs(
        store,
        "MATCH (q:Query)-[:MOVES_IN]->(w:Wave {effective: true}) RETURN q.id AS q, w.wave AS w",
    )
    cached = _pairs(
        store,
        "MATCH (q:Query)-[:CACHED_IN]->(w:Wave {effective: true}) RETURN q.id AS q, w.wave AS w",
    )
    cutover = _pairs(
        store,
        "MATCH (q:Query)-[:CUTS_OVER_IN]->(w:Wave {effective: true}) RETURN q.id AS q, w.wave AS w",
    )
    assert moves == sorted(overlays.query_move_wave.items()) and moves
    assert cached == sorted(overlays.query_cached_in.items())
    assert cutover == sorted(
        (q, s["wave"])
        for q in overlays.query_move_wave
        for s in query_path(overlays, q)
        if s["kind"] == "cutover"
    )


def test_move_units_are_the_placement_units(job):
    store, overlays, _, _ = job
    members = _pairs(
        store,
        "MATCH (q:Query)-[:MEMBER_OF]->(u:MoveUnit {effective: true}) "
        "RETURN q.id AS q, u.unit AS unit, u.size AS size",
    )
    assert members == sorted((q, unit, size) for q, (unit, size) in overlays.query_units.items())
    assert members


def test_sources_of_truth_and_replica_reads_are_the_table_sources(job):
    store, overlays, _, _ = job
    sot = _pairs(
        store,
        "MATCH (st:SourceTable)-[:SOURCE_OF_TRUTH {effective: true}]->(e:Engine) "
        "RETURN st.id AS t, e.id AS e",
    )
    assert sot == sorted((t, s["source_of_truth"]) for t, s in overlays.table_sources.items())
    replica = _pairs(
        store,
        "MATCH (q:Query)-[r:READS_REPLICA {effective: true}]->(st:SourceTable) "
        "RETURN q.id AS q, st.id AS t, r.engine AS e",
    )
    assert replica == sorted(
        (q, t, e)
        for t, s in overlays.table_sources.items()
        for e, qs in s["replica_reads_by_engine"].items()
        for q in qs
    )
    # The writers (WRITES, #1816) are the table sources' writers.
    writers = {}
    for q, t in _pairs(store, "MATCH (q:Query)-[:WRITES]->(st:SourceTable) RETURN q.id, st.id"):
        writers.setdefault(t, set()).add(q)
    assert writers == {t: s["writers"] for t, s in overlays.table_sources.items()}


def test_the_cache_layer_is_each_querys_cache_engine(job):
    store, _, version, path = job
    assignment = json.loads((path / f"assignment/v{version}/assignment.json").read_text())
    expected = sorted(
        (
            qa["query_id"],
            qa["cache_engine"],
            qa.get("cache_pattern"),
            qa.get("cache_reason"),
            bool(qa.get("cache_dropped")),
        )
        for qa in assignment["query_assignments"]
        if qa.get("cache_engine")
    )
    found = _pairs(
        store,
        "MATCH (q:Query)-[c:CACHED_BY {effective: true}]->(e:Engine) "
        "RETURN q.id AS q, e.id AS e, c.pattern AS p, c.reason AS r, c.dropped AS d",
    )
    assert found == expected and found


def test_engine_cost_is_the_reports(job):
    store, overlays, version, _ = job
    rows = store.query(
        "MATCH (e:Engine) WHERE e.monthly_cost_usd IS NOT NULL "
        "RETURN e.id AS e, e.monthly_cost_usd AS cost, e.cost_version AS v"
    )
    assert {r["e"]: r["cost"] for r in rows} == overlays.engine_costs and rows
    # The version the report used was built from.
    assert {r["v"] for r in rows} == {overlays.report_version}


def _write(base: Path, files: dict):
    for path, data in files.items():
        full = base / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))


def _qa(qid, engine, cache=None):
    qa = {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": ["orders"],
        "assignment_reason": "r",
    }
    if cache:
        qa.update(cache_engine=cache, cache_pattern="point_lookup", cache_reason="hot")
    return qa


def _report(version, waves):
    return {
        "assignment_summary": {"version": version},
        "facts": {
            "waves": [{"wave": w, "engines": [e], "migrating": True} for w, e in waves],
            "engines": [{"engine": e, "monthly_cost_usd": 10.0 * version} for _, e in waves],
        },
        "migration_waves": [
            {"wave": w, "title": f"v{version} wave {w}", "engines": [e], "query_count": 1}
            for w, e in waves
        ],
    }


def test_each_version_has_its_own_overlays_and_the_effective_one_is_marked(
    tmp_path, sample_collector_output
):
    """v1 had a report (synthesis/v1) and v2 has its own: each version's waves come
    from its own report, edges carry their version, and only v2's are effective.
    Engine cost is the effective report's."""
    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [_qa("q1", "dynamodb"), _qa("q2", "documentdb")],
        "table_assignments": [],
    }
    v2 = {
        **v1,
        "version": 2,
        "previous_version": 1,
        "source": "reality_check",
        "query_assignments": [_qa("q1", "dynamodb", cache="elasticache"), _qa("q2", "dynamodb")],
    }
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "assignment/v2/assignment.json": v2,
            "synthesis/v1/report.json": _report(1, [(1, "documentdb"), (2, "dynamodb")]),
            "synthesis/v2/report.json": _report(2, [(1, "elasticache"), (2, "dynamodb")]),
        },
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    waves = _pairs(
        store, "MATCH (w:Wave) RETURN w.version AS v, w.wave AS w, w.title AS t, w.effective AS e"
    )
    assert waves == [
        (1, 1, "v1 wave 1", False),
        (1, 2, "v1 wave 2", False),
        (2, 1, "v2 wave 1", True),
        (2, 2, "v2 wave 2", True),
    ]
    moves = _pairs(
        store,
        "MATCH (q:Query)-[m:MOVES_IN]->(w:Wave) RETURN q.id AS q, w.version AS v, w.wave AS w, "
        "m.effective AS e",
    )
    assert moves == [
        ("q1", 1, 2, False),
        ("q1", 2, 2, True),
        ("q2", 1, 1, False),
        ("q2", 2, 2, True),
    ]
    cached = _pairs(
        store,
        "MATCH (q:Query)-[c:CACHED_BY]->(e:Engine) RETURN q.id AS q, c.version AS v, "
        "e.id AS e, c.effective AS eff",
    )
    assert cached == [("q1", 2, "elasticache", True)]
    assert (
        store.query("MATCH (e:Engine {id: 'dynamodb'}) RETURN e.monthly_cost_usd AS c")[0]["c"]
        == 20.0
    )
    store.close()


def test_a_version_without_its_own_report_has_no_waves(tmp_path, sample_collector_output):
    """Only the effective version falls back to the legacy report keys (as
    ``read_overlays`` does); an older version without ``synthesis/v<N>`` has no waves,
    but its assignment's cache layer is still there."""
    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [_qa("q1", "dynamodb", cache="elasticache")],
        "table_assignments": [],
    }
    v2 = {**v1, "version": 2, "previous_version": 1, "source": "reality_check"}
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "assignment/v2/assignment.json": v2,
            "synthesis/v2/report.json": _report(2, [(1, "dynamodb")]),
        },
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    assert _pairs(store, "MATCH (w:Wave) RETURN w.version AS v, w.wave AS w") == [(2, 1)]
    assert _pairs(store, "MATCH (v:AssignmentVersion) RETURN v.version, v.has_report") == [
        (1, False),
        (2, True),
    ]
    assert _pairs(store, "MATCH (q:Query)-[c:CACHED_BY]->(:Engine) RETURN q.id, c.version") == [
        ("q1", 1),
        ("q1", 2),
    ]
    store.close()


def test_a_job_without_a_report_has_no_waves_sources_or_cost(tmp_path, sample_collector_output):
    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [_qa("q1", "dynamodb")],
        "table_assignments": [],
    }
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {"collector/output.json": sample_collector_output, "assignment/v1/assignment.json": v1},
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    for cypher in (
        "MATCH (w:Wave) RETURN count(w) AS n",
        "MATCH ()-[r:SOURCE_OF_TRUTH]->() RETURN count(r) AS n",
        "MATCH (e:Engine) WHERE e.monthly_cost_usd IS NOT NULL RETURN count(e) AS n",
    ):
        assert store.query(cypher)[0]["n"] == 0
    store.close()


def test_catalog_wave_queries(job):
    """O is the explorer's wave list (``wave_query_ids``): the queries that move in the
    wave, first, then the reads it caches; never the cutover passengers. Each row is
    projected as its own relation."""
    from src.graph import catalog
    from src.graph.explorer_overlays import wave_query_ids

    store, overlays, version, _ = job
    entry = catalog.get_entry("wave_queries")
    for wave in sorted({w["wave"] for w in overlays.plan}):
        rows = catalog.run_entry(store, entry, {"wave_id": f"v{version}:wave-{wave}"}).rows
        assert {r["query_id"] for r in rows} == set(wave_query_ids(overlays, wave)), wave
        moves = [r["query_id"] for r in rows if r["relation"] == "MOVES_IN"]
        assert moves == sorted(q for q, w in overlays.query_move_wave.items() if w == wave)
        assert [r["relation"] for r in rows] == sorted(
            (r["relation"] for r in rows), key=lambda x: x != "MOVES_IN"
        )
        edges = catalog.project(entry, rows)["edges"]
        assert {(e["label"], e["source"], e["target"]) for e in edges} == {
            (r["relation"], r["query_id"], r["wave_id"]) for r in rows
        }


def test_catalog_wave_cutover(job):
    from src.graph import catalog

    store, overlays, version, _ = job
    assert overlays.cutover, "both fixtures' plans have a cutover first wave"
    wave = overlays.cutover["wave"]
    rows = catalog.run_entry(
        store, catalog.get_entry("wave_cutover"), {"wave_id": f"v{version}:wave-{wave}"}
    ).rows
    expected = sorted(
        q
        for q in overlays.query_move_wave
        for s in query_path(overlays, q)
        if s["kind"] == "cutover" and s["wave"] == wave
    )
    assert [r["query_id"] for r in rows] == expected[: catalog.MAX_ROWS]
    later = {r["query_id"]: r["moves_later_in"] for r in rows}
    assert all(later[q] == f"v{version}:wave-{overlays.query_move_wave[q]}" for q in later)


def test_table_groups_engine_facts_and_query_classes(job):
    """The rest the explorer reads from the job's files is in the graph too: each
    wave's table groups, each engine's role, rationale and workload share, and each
    query's access-pattern class and reason."""
    store, overlays, version, _ = job
    groups = {
        r["w"]: json.loads(r["g"])
        for r in store.query(
            "MATCH (w:Wave {effective: true}) RETURN w.wave AS w, w.table_groups_json AS g"
        )
    }
    assert groups == {w["wave"]: w["table_groups"] for w in overlays.plan}
    facts = {
        r["e"]: {
            "role": r["role"],
            "rationale": r["rationale"],
            "workload_percent": r["share"],
            "monthly_cost_usd": r["cost"],
        }
        for r in store.query(
            "MATCH (e:Engine) WHERE e.cost_version IS NOT NULL RETURN e.id AS e, "
            "e.role AS role, e.rationale AS rationale, e.workload_percent AS share, "
            "e.monthly_cost_usd AS cost"
        )
    }
    assert facts == overlays.engine_facts
    classes = {
        r["q"]: (r["c"], r["why"])
        for r in store.query(
            "MATCH (q:Query) WHERE q.access_pattern_class IS NOT NULL "
            "RETURN q.id AS q, q.access_pattern_class AS c, q.access_pattern_reason AS why"
        )
    }
    assert classes == overlays.query_classes


def test_catalog_table_writers(job):
    from src.graph import catalog

    store, overlays, _, _ = job
    table, source = next(iter(sorted(overlays.table_sources.items())))
    rows = catalog.run_entry(store, catalog.get_entry("table_writers"), {"table_id": table}).rows
    assert {r["query_id"] for r in rows} == source["writers"]
    assert {r["source_of_truth"] for r in rows} == {source["source_of_truth"]}


def test_catalog_query_moves_with(job):
    from src.graph import catalog

    store, overlays, _, _ = job
    query, (unit, _) = next(iter(sorted(overlays.query_units.items())))
    rows = catalog.run_entry(store, catalog.get_entry("query_moves_with"), {"query_id": query}).rows
    assert {r["query_id"] for r in rows} == {
        q for q, (u, _) in overlays.query_units.items() if u == unit
    }


def test_catalog_cache_queries(job):
    from src.graph import catalog

    store, overlays, _, _ = job
    rows = catalog.run_entry(store, catalog.get_entry("cache_queries"), {}).rows
    assert {(r["query_id"], r["engine"], r["pattern"], r["dropped"]) for r in rows} == {
        (q, c["engine"], c["pattern"], c["dropped"]) for q, c in overlays.query_cache.items()
    }


def test_cost_version_is_the_version_the_used_report_was_built_from(
    tmp_path, sample_collector_output
):
    """After a reroute (v2) with synthesis not run again, the effective version reads
    the legacy report, built from v1: the cost is stamped v1, not v2."""
    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [_qa("q1", "dynamodb")],
        "table_assignments": [],
    }
    v2 = {**v1, "version": 2, "previous_version": 1, "source": "post_schema_routing"}
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "assignment/v2/assignment.json": v2,
            "referee-synthesis/report.json": _report(1, [(1, "dynamodb")]),
        },
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    assert store.query(
        "MATCH (e:Engine {id: 'dynamodb'}) RETURN e.monthly_cost_usd AS c, e.cost_version AS v"
    ) == [{"c": 10.0, "v": 1}]
    store.close()


def test_the_needs_count_only_the_effective_version(tmp_path, sample_collector_output):
    """A graph whose only waves, units and cache layer belong to an older version
    (v1 had a report; the effective v2 has none and no cache) has no choices for O, and
    its needs say so: they count the effective version only."""
    from src.graph import catalog

    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [_qa("q1", "dynamodb", cache="elasticache")],
        "table_assignments": [],
    }
    v2 = {
        **v1,
        "version": 2,
        "previous_version": 1,
        "source": "customer_gate",
        "query_assignments": [_qa("q1", "dynamodb")],
    }
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "assignment/v2/assignment.json": v2,
            "synthesis/v1/report.json": _report(1, [(1, "dynamodb")]),
        },
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    assert _pairs(store, "MATCH (w:Wave) RETURN w.version, w.effective") == [(1, False)]
    assert catalog.param_choices(store, catalog.get_entry("wave_queries").params[0]) == []
    for entry_id, need in (("wave_queries", "waves"), ("cache_queries", "cache_layer")):
        unmet = [n.id for n in catalog.unmet_needs(store, catalog.get_entry(entry_id))]
        assert need in unmet, entry_id
    store.close()


def test_a_wave_lists_its_moves_before_its_cached_reads(tmp_path, sample_collector_output):
    """A wave of DynamoDB and ElastiCache moves q1 and q3 to DynamoDB and puts a cache in
    front of q2, which moves to DocumentDB later: O lists q1 and q3 first, then q2."""
    from src.graph import catalog

    v1 = {
        "version": 1,
        "previous_version": None,
        "source": "assignment_resolution",
        "query_assignments": [
            _qa("q1", "dynamodb"),
            _qa("q2", "documentdb", cache="elasticache"),
            _qa("q3", "dynamodb"),
        ],
        "table_assignments": [],
    }
    report = {
        "assignment_summary": {"version": 1},
        "facts": {
            "waves": [
                {"wave": 1, "engines": ["dynamodb", "elasticache"], "migrating": True},
                {"wave": 2, "engines": ["documentdb"], "migrating": True},
            ],
            "engines": [],
        },
        "migration_waves": [
            {"wave": 1, "title": "w1", "engines": ["dynamodb", "elasticache"], "query_count": 3},
            {"wave": 2, "title": "w2", "engines": ["documentdb"], "query_count": 1},
        ],
    }
    base = tmp_path / "artifacts"
    _write(
        base / "db/job",
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "synthesis/v1/report.json": report,
        },
    )
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("db", "job", LocalArtifactStore(str(base)), store)
    rows = catalog.run_entry(
        store, catalog.get_entry("wave_queries"), {"wave_id": "v1:wave-1"}
    ).rows
    assert [(r["relation"], r["query_id"]) for r in rows] == [
        ("MOVES_IN", "q1"),
        ("MOVES_IN", "q3"),
        ("CACHED_IN", "q2"),
    ]
    store.close()


def test_the_cutover_entry_stays_in_its_wave_version(tmp_path):
    """With an older version that has its own report (the real run's report copied to
    synthesis/v1 as well), each cutover query also moves in a v1 wave: P joins only
    the later wave of the same version, so v2:wave-1 lists each of its 39 cutover
    queries once (wave 3's 39 moves; wave 2 is the cache wave), with the row bound and
    without."""
    from src.graph import catalog

    base = tmp_path / "artifacts"
    shutil.copytree(REAL_RUN / "job", base / "wordpress" / "job")
    for v in (1, 2):
        dest = base / f"wordpress/job/synthesis/v{v}/report.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_RUN / "report.json", dest)
    artifacts = LocalArtifactStore(str(base))
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", artifacts, store)
    assert _pairs(store, "MATCH (w:Wave {wave: 1}) RETURN w.id, w.effective") == [
        ("v1:wave-1", False),
        ("v2:wave-1", True),
    ]
    overlays = read_overlays(artifacts, "wordpress", "job", 2)
    expected = sorted(
        q
        for q in overlays.query_move_wave
        for s in query_path(overlays, q)
        if s["kind"] == "cutover" and s["wave"] == 1
    )
    assert len(expected) == 39
    entry = catalog.get_entry("wave_cutover")
    bounded = catalog.run_entry(store, entry, {"wave_id": "v2:wave-1"}).rows
    unbounded = store.query(entry.cypher, {"wave_id": "v2:wave-1"})
    assert len(bounded) == len(unbounded) == 39
    assert [r["query_id"] for r in bounded] == expected
    assert all(r["wave_id"] == "v2:wave-1" for r in bounded)
    assert all(r["moves_later_in"].startswith("v2:") for r in unbounded)
    store.close()
