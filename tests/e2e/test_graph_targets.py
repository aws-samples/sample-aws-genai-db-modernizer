# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Versioned target objects on real runs (#306).

The expectations are built here from the job's own artifacts, independently of the
populator: for each lineage version, each query's engines (assigned and cache),
the schema output of each engine at that version, and the object each access
pattern names under the engine's contract field; a labelled fallback when there
is no design or no pattern for the query. Read from the run's artifacts, so routing
drift does not break it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

ENGINES = (
    "aurora_mysql",
    "aurora_postgresql",
    "documentdb",
    "dynamodb",
    "elasticache",
    "opensearch",
)


def _bare(name: str) -> str:
    return "".join(c for c in name if c not in '`"[]').strip().lower().rsplit(".", 1)[-1].strip()


def _defined(engine: str, design: dict) -> set[tuple[str, str]]:
    tables = design.get("table_definitions") or []
    if engine == "dynamodb":
        return {("table", t["table_name"]) for t in tables} | {
            ("gsi", g["gsi_name"]) for t in tables for g in t.get("gsis") or []
        }
    if engine == "documentdb":
        return {("collection", c["collection_name"]) for c in design.get("collections") or []}
    if engine == "opensearch":
        return {("index", i["index_name"]) for i in design.get("index_designs") or []} | {
            ("index", d["data_stream_name"]) for d in design.get("data_stream_designs") or []
        }
    if engine == "elasticache":
        return {("key_pattern", k["key_pattern"]) for k in design.get("key_designs") or []}
    return {("table", t["table_name"]) for t in tables}


def _objects(engine: str, design: dict) -> tuple[dict[str, set[tuple[str, str]]], set[str]]:
    """``(served, unserved)``: the defined objects each query's patterns name, and the
    queries only out-of-scope or unsupported patterns name."""
    tables = {
        _bare(t["table_name"]): t["table_name"] for t in design.get("table_definitions") or []
    }
    defined = _defined(engine, design)
    out: dict[str, set[tuple[str, str]]] = {}
    unserved: set[str] = set()
    for up in design.get("unsupported_patterns") or []:
        unserved.update(up.get("query_ids") or up.get("source_query_ids") or [])
    for ap in design.get("access_patterns") or []:
        qids = ap.get("query_ids") or ap.get("source_query_ids") or []
        if ap.get("in_scope") is False:
            unserved.update(qids)
            continue
        if engine == "dynamodb":
            found = (
                {("gsi", ap["gsi_name"])} if ap.get("gsi_name") else {("table", ap["table_name"])}
            )
        elif engine == "documentdb":
            found = {("collection", ap["collection_name"])}
        elif engine == "opensearch":
            found = {("index", ap["index_or_stream"])}
        elif engine == "elasticache":
            found = {("key_pattern", ap["key_pattern"])}
        else:
            found = {
                ("table", tables[_bare(t)])
                for t in ap.get("source_tables") or []
                if _bare(t) in tables
            }
        found &= defined
        for q in qids:
            if found:
                out.setdefault(q, set()).update(found)
    return out, unserved - set(out)


def _expected(job: Path, effective: int) -> dict[tuple[str, int], set[tuple]]:
    chain, version = [], effective
    while isinstance(version, int) and version >= 1:
        path = job / f"assignment/v{version}/assignment.json"
        if not path.exists():
            break
        chain.append(json.loads(path.read_text()))
        version = chain[-1].get("previous_version")
    expected: dict[tuple[str, int], set[tuple]] = {}
    for assignment in chain:
        v = assignment["version"]
        # A skip record is no design (#2154), as the graph build reads it.
        designs = {
            e: d
            for e in ENGINES
            if (p := job / f"schema-{e}/v{v}/schema_output.json").exists()
            and (d := json.loads(p.read_text())).get("status") != "skipped"
        }
        objects = {e: _objects(e, d) for e, d in designs.items()}
        for qa in assignment["query_assignments"]:
            for engine in {qa["assigned_engine"], qa.get("cache_engine")} & set(ENGINES):
                served, unserved = objects.get(engine, ({}, set()))
                got = {(engine, *o) for o in served.get(qa["query_id"], set())}
                if not got:
                    if engine not in designs:
                        kind = "not_designed"
                    elif qa["query_id"] in unserved:
                        kind = "out_of_scope"
                    else:
                        kind = "not_in_design"
                    got = {(engine, kind, None)}
                expected.setdefault((qa["query_id"], v), set()).update(got)
    return expected


def _found(run: PipelineResult, tmp_path: Path) -> tuple[dict, dict, int]:
    root = tmp_path / "artifacts"
    job = root / run.db / run.job_id
    shutil.copytree(run.job_dir(), job)
    store = LocalArtifactStore(base_dir=str(root))
    graph = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(graph)
    rebuild_graph(run.db, run.job_id, store, graph)
    effective = resolve_effective_assignment_version(store, run.db, run.job_id)
    rows = graph.query(
        "MATCH (q:Query)-[t:TARGETS]->(o:TargetObject) RETURN q.id AS q, t.version AS v, "
        "t.effective AS eff, o.engine AS e, o.kind AS k, o.name AS n, o.fallback AS fb"
    )
    graph.close()
    found: dict[tuple[str, int], set[tuple]] = {}
    for r in rows:
        assert r["eff"] == (r["v"] == effective)
        found.setdefault((r["q"], r["v"]), set()).add((r["e"], r["k"], None if r["fb"] else r["n"]))
    return found, _expected(job, effective), effective


def test_target_objects_match_each_runs_designs(run: PipelineResult, tmp_path: Path) -> None:
    """Every sample run, deterministic (no design: every query the 'not designed yet'
    fallback) or a headless run's job (its designs)."""
    found, expected, _ = _found(run, tmp_path)
    assert found == expected
    if not run.external:
        assert {k for objs in found.values() for (_, k, _) in objs} == {"not_designed"}


@pytest.mark.deterministic
def test_target_objects_on_the_designed_run(designed_run: PipelineResult, tmp_path: Path) -> None:
    """The designed WordPress job (DynamoDB, ElastiCache and Aurora MySQL designs):
    the effective version's queries land on real design objects, several of them on
    more than one."""
    found, expected, effective = _found(designed_run, tmp_path)
    assert found == expected
    now = {q: objs for (q, v), objs in found.items() if v == effective}
    designed = {
        q
        for q, objs in now.items()
        if any(k not in ("not_designed", "not_in_design", "out_of_scope") for _, k, _ in objs)
    }
    assert len(designed) > len(now) // 2
    assert any(len(objs) > 1 for objs in now.values())
