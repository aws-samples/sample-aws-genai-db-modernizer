# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Versioned target objects in the context graph (#306).

Each query links (TARGETS) to its design's target objects per assignment version
and schema version: a DynamoDB table or GSI, a DocumentDB collection, an
OpenSearch index or stream, an ElastiCache key pattern, an Aurora table. The
effective version is marked. With no design for an engine at a version, or no
access pattern for the query in it, the query links to a labelled fallback,
never a guessed object. MIGRATES_TO stays one edge per query, to the effective
engine's Destination.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore

REAL_WORDPRESS = Path(__file__).parents[1] / "fixtures/real_runs/wordpress-both/job"


def _qa(qid, engine, cache=None):
    qa = {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": ["orders"],
        "assignment_reason": "r",
    }
    if cache:
        qa["cache_engine"] = cache
    return qa


V1 = {
    "version": 1,
    "previous_version": None,
    "source": "assignment_resolution",
    "status": "auto_generated",
    "query_assignments": [_qa("q1", "dynamodb"), _qa("q2", "documentdb"), _qa("q3", "dynamodb")],
    "table_assignments": [],
}
V2 = {
    "version": 2,
    "previous_version": 1,
    "source": "reality_check",
    "status": "auto_generated",
    "query_assignments": [
        _qa("q1", "dynamodb", cache="elasticache"),
        _qa("q2", "dynamodb"),
        _qa("q3", "aurora_mysql"),
    ],
    "table_assignments": [],
}


def _ddb(pattern_id, table, query_ids, gsi=None):
    return {
        "pattern_id": pattern_id,
        "description": pattern_id,
        "operation": "Query",
        "table_name": table,
        "gsi_name": gsi,
        "query_ids": list(query_ids),
    }


DDB_V1 = {
    "table_definitions": [{"table_name": "OrdersOld"}],
    "access_patterns": [_ddb("DDB-AP-1", "OrdersOld", ["q1"])],
}
# q1 reads the Orders table and its ByCustomer GSI; q2 the GSI and the Customers table.
DDB_V2 = {
    "table_definitions": [
        {"table_name": "Orders", "gsis": [{"gsi_name": "ByCustomer"}]},
        {"table_name": "Customers"},
    ],
    "access_patterns": [
        _ddb("DDB-AP-1", "Orders", ["q1"]),
        _ddb("DDB-AP-2", "Orders", ["q1", "q2"], gsi="ByCustomer"),
        _ddb("DDB-AP-3", "Customers", ["q2"]),
    ],
}
AURORA_V2 = {
    "table_definitions": [{"table_name": "orders"}, {"table_name": "customers"}],
    "access_patterns": [
        {"pattern_id": "q3", "source_tables": ["shop.Orders"], "source_query_ids": ["q3"]}
    ],
}
CACHE_V2 = {
    "key_designs": [{"key_pattern": "order:{id}"}],
    "access_patterns": [
        {"pattern_id": "EC-AP-1", "key_pattern": "order:{id}", "source_query_ids": ["q1"]}
    ],
}


def _build(tmp_path, files, db="testdb", job="job-1") -> GraphStore:
    base = tmp_path / "artifacts"
    for path, data in files.items():
        full = base / db / job / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(data))
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph(db, job, LocalArtifactStore(str(base)), store)
    return store


@pytest.fixture
def lineage(tmp_path, sample_collector_output):
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": V1,
            "assignment/v2/assignment.json": V2,
            "schema-dynamodb/v1/schema_output.json": DDB_V1,
            "schema-dynamodb/v2/schema_output.json": DDB_V2,
            "schema-aurora_mysql/v2/schema_output.json": AURORA_V2,
            "schema-elasticache/v2/schema_output.json": CACHE_V2,
        },
    )
    yield store
    store.close()


def _targets(store):
    rows = store.query(
        "MATCH (q:Query)-[t:TARGETS]->(o:TargetObject) RETURN q.id AS q, t.version AS v, "
        "t.schema_version AS sv, t.effective AS eff, o.id AS id, o.engine AS engine, "
        "o.kind AS kind, o.name AS name, o.parent AS parent, o.fallback AS fallback"
    )
    return rows


def _by_query_version(store):
    out: dict[tuple[str, int], set[tuple]] = {}
    for r in _targets(store):
        out.setdefault((r["q"], r["v"]), set()).add((r["engine"], r["kind"], r["name"]))
    return out


def test_each_query_links_to_its_designs_objects_per_version(lineage):
    assert _by_query_version(lineage) == {
        # v1: only DynamoDB was designed, and only q1 has a pattern in it.
        ("q1", 1): {("dynamodb", "table", "OrdersOld")},
        ("q2", 1): {("documentdb", "not_designed", "not designed yet")},
        ("q3", 1): {("dynamodb", "not_in_design", "no target in the design")},
        # v2: several objects for q1 (its table, its GSI and its cache key) and q2
        # (a GSI and a second table); q3's Aurora table, matched by name.
        ("q1", 2): {
            ("dynamodb", "table", "Orders"),
            ("dynamodb", "gsi", "ByCustomer"),
            ("elasticache", "key_pattern", "order:{id}"),
        },
        ("q2", 2): {("dynamodb", "gsi", "ByCustomer"), ("dynamodb", "table", "Customers")},
        ("q3", 2): {("aurora_mysql", "table", "orders")},
    }


def test_objects_are_versioned_and_the_effective_version_is_marked(lineage):
    rows = _targets(lineage)
    assert {r["v"] for r in rows if r["eff"]} == {2}
    assert {r["v"] for r in rows if not r["eff"]} == {1}
    designed = {r["id"]: r for r in rows if not r["fallback"]}
    assert designed["dynamodb:table:OrdersOld@v1"]["sv"] == 1
    assert designed["dynamodb:gsi:Orders.ByCustomer@v2"]["parent"] == "Orders"
    assert designed["dynamodb:gsi:Orders.ByCustomer@v2"]["sv"] == 2
    # A fallback is no design object: no schema version.
    assert all(r["sv"] is None for r in rows if r["fallback"])
    assert {r["id"] for r in rows if r["fallback"]} == {
        "documentdb:not_designed",
        "dynamodb:not_in_design",
    }


def test_every_access_pattern_is_named_on_its_edge(lineage):
    rows = lineage.query(
        "MATCH (q:Query {id: 'q1'})-[t:TARGETS {version: 2}]->(o:TargetObject {kind: 'table'}) "
        "RETURN t.pattern_ids_json AS ids"
    )
    assert [json.loads(r["ids"]) for r in rows] == [["DDB-AP-1"]]


def test_migrates_to_stays_one_edge_per_query_to_the_engine(lineage):
    rows = lineage.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination)-[:HOSTED_ON]->(e:Engine) "
        "RETURN q.id AS q, d.id AS d, d.engine AS de, d.artifact_type AS kind, e.id AS e"
    )
    assert sorted((r["q"], r["d"], r["de"], r["kind"], r["e"]) for r in rows) == [
        ("q1", "dynamodb", "dynamodb", "engine", "dynamodb"),
        ("q2", "dynamodb", "dynamodb", "engine", "dynamodb"),
        ("q3", "aurora_mysql", "aurora_mysql", "engine", "aurora_mysql"),
    ]


ENGINE_SHAPES = {
    "dynamodb": (
        {
            "table_definitions": [{"table_name": "Orders"}],
            "access_patterns": [_ddb("DDB-AP-1", "Orders", ["q1"])],
        },
        ("table", "Orders"),
    ),
    "documentdb": (
        {
            "collections": [{"collection_name": "orders"}],
            "access_patterns": [
                {"pattern_id": "DOC-AP-1", "collection_name": "orders", "source_query_ids": ["q1"]}
            ],
        },
        ("collection", "orders"),
    ),
    "opensearch": (
        {
            "index_designs": [{"index_name": "orders-idx"}],
            "access_patterns": [
                {"pattern_id": "OS-AP-1", "index_or_stream": "orders-idx", "query_ids": ["q1"]}
            ],
        },
        ("index", "orders-idx"),
    ),
    "elasticache": (CACHE_V2, ("key_pattern", "order:{id}")),
    "aurora_postgresql": (
        {
            "table_definitions": [{"table_name": "orders"}],
            "access_patterns": [
                {"pattern_id": "q1", "source_tables": ["orders"], "source_query_ids": ["q1"]}
            ],
        },
        ("table", "orders"),
    ),
}


@pytest.mark.parametrize("engine", sorted(ENGINE_SHAPES))
def test_each_engines_contract_field(tmp_path, sample_collector_output, engine):
    design, (kind, name) = ENGINE_SHAPES[engine]
    v1 = {**V1, "query_assignments": [_qa("q1", engine)]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            f"schema-{engine}/v1/schema_output.json": design,
        },
    )
    assert _by_query_version(store) == {("q1", 1): {(engine, kind, name)}}
    store.close()


def test_an_aurora_pattern_on_an_undesigned_table_is_not_guessed(tmp_path, sample_collector_output):
    """An Aurora pattern whose source table is no table of the design (the collector's
    'unknown' placeholder) links to the fallback, not to a guessed table."""
    design = {
        "table_definitions": [{"table_name": "orders"}],
        "access_patterns": [
            {"pattern_id": "q1", "source_tables": ["unknown"], "source_query_ids": ["q1"]}
        ],
    }
    v1 = {**V1, "query_assignments": [_qa("q1", "aurora_mysql")]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "schema-aurora_mysql/v1/schema_output.json": design,
        },
    )
    assert _by_query_version(store) == {
        ("q1", 1): {("aurora_mysql", "not_in_design", "no target in the design")}
    }
    store.close()


def test_without_any_design_every_query_has_the_labelled_fallback(
    tmp_path, sample_collector_output
):
    """A deterministic run (no schema design): each query links to its engine's
    'not designed yet' target, and nothing else."""
    store = _build(
        tmp_path,
        {"collector/output.json": sample_collector_output, "assignment/v1/assignment.json": V1},
    )
    rows = _targets(store)
    assert sorted((r["q"], r["id"], r["fallback"], r["eff"]) for r in rows) == [
        ("q1", "dynamodb:not_designed", True, True),
        ("q2", "documentdb:not_designed", True, True),
        ("q3", "dynamodb:not_designed", True, True),
    ]
    store.close()


def _expected_real(job: Path) -> dict:
    """Independent of the populator: each version's assigned and cache engines, the
    designs of that version, and the objects each access pattern names."""
    expected: dict[tuple[str, int], set[tuple]] = {}
    for v in (1, 2):
        assignment = json.loads((job / f"assignment/v{v}/assignment.json").read_text())
        designs = {}
        for engine in ("dynamodb", "elasticache", "aurora_mysql"):
            path = job / f"schema-{engine}/v{v}/schema_output.json"
            if path.exists():
                designs[engine] = json.loads(path.read_text())
        objects: dict[tuple[str, str], set[tuple]] = {}
        unserved: dict[str, set[str]] = {}
        for engine, design in designs.items():
            names = {t["table_name"].lower() for t in design.get("table_definitions") or []}
            gsis = {
                g["gsi_name"]
                for t in design.get("table_definitions") or []
                for g in t.get("gsis") or []
            }
            keys = {k["key_pattern"] for k in design.get("key_designs") or []}
            for up in design.get("unsupported_patterns") or []:
                unserved.setdefault(engine, set()).update(
                    up.get("query_ids") or up.get("source_query_ids") or []
                )
            for ap in design["access_patterns"]:
                qids = ap.get("query_ids") or ap.get("source_query_ids") or []
                if ap.get("in_scope") is False:
                    unserved.setdefault(engine, set()).update(qids)
                    continue
                if engine == "dynamodb":
                    if ap.get("gsi_name"):
                        found = {(engine, "gsi", ap["gsi_name"])} & {
                            (engine, "gsi", g) for g in gsis
                        }
                    else:
                        found = (
                            {(engine, "table", ap["table_name"])}
                            if ap["table_name"].lower() in names
                            else set()
                        )
                elif engine == "elasticache":
                    found = (
                        {(engine, "key_pattern", ap["key_pattern"])}
                        if ap["key_pattern"] in keys
                        else set()
                    )
                else:
                    found = {
                        (engine, "table", t.split(".")[-1].lower())
                        for t in ap.get("source_tables") or []
                        if t.split(".")[-1].lower() in names
                    }
                for q in qids:
                    objects.setdefault((q, engine), set()).update(found)
        for qa in assignment["query_assignments"]:
            for engine in {qa["assigned_engine"], qa.get("cache_engine")} - {None, "unservable"}:
                got = objects.get((qa["query_id"], engine))
                if not got:
                    if engine not in designs:
                        got = {(engine, "not_designed", "not designed yet")}
                    elif qa["query_id"] in unserved.get(engine, set()):
                        got = {(engine, "out_of_scope", "the design does not serve it")}
                    else:
                        got = {(engine, "not_in_design", "no target in the design")}
                expected.setdefault((qa["query_id"], v), set()).update(got)
    return expected


def test_real_wordpress_run(tmp_path):
    """The real WordPress run: v1 was never designed (every query 'not designed
    yet'); v2, the effective version, has DynamoDB, ElastiCache and Aurora MySQL
    designs, and each query links to the objects its access patterns name."""
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_WORDPRESS, base / "wordpress" / "job")
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    found = _by_query_version(store)
    expected = _expected_real(REAL_WORDPRESS)
    assert found == expected
    v2 = {k: o for k, o in found.items() if k[1] == 2}
    multi = sum(1 for o in v2.values() if len(o) > 1)
    designed = sum(
        1
        for o in v2.values()
        if any(kind not in ("not_designed", "not_in_design") for _, kind, _ in o)
    )
    # 107 queries; 105 reach a design object (two only the fallback), 39 several
    # objects (a table and a GSI, a join, or a table and its cache key).
    assert (len(v2), designed, multi) == (107, 105, 39)
    store.close()


@pytest.fixture
def real_graph(tmp_path):
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_WORDPRESS, base / "wordpress" / "job")
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    yield store
    store.close()


def test_catalog_where_a_query_lands(real_graph):
    from src.graph import catalog

    multi = real_graph.query(
        "MATCH (q:Query)-[t:TARGETS {effective: true}]->(o:TargetObject {fallback: false}) "
        "WITH q.id AS qid, COUNT(o) AS n WHERE n > 1 RETURN qid AS q ORDER BY q LIMIT 1"
    )[0]["q"]
    rows = catalog.run_entry(
        real_graph, catalog.get_entry("query_targets"), {"query_id": multi}
    ).rows
    direct = real_graph.query(
        "MATCH (q:Query {id: $q})-[t:TARGETS]->(o:TargetObject) "
        "RETURN t.version AS v, o.id AS id",
        {"q": multi},
    )
    assert sorted((r["version"], r["target_id"]) for r in rows) == sorted(
        (r["v"], r["id"]) for r in direct
    )
    assert [r["version"] for r in rows] == sorted((r["version"] for r in rows), reverse=True)
    assert sum(1 for r in rows if r["effective"] and not r["fallback"]) > 1


def test_catalog_which_queries_a_table_serves(real_graph):
    from src.graph import catalog

    rows = catalog.run_entry(
        real_graph,
        catalog.get_entry("target_queries"),
        {"target_id": "aurora_mysql:table:wp_posts@v2"},
    ).rows
    design = json.loads((REAL_WORDPRESS / "schema-aurora_mysql/v2/schema_output.json").read_text())
    expected = {
        q
        for ap in design["access_patterns"]
        if any(t.split(".")[-1].lower() == "wp_posts" for t in ap.get("source_tables") or [])
        for q in ap["source_query_ids"]
    }
    assert {r["query_id"] for r in rows} == expected
    assert len(rows) == 28
    assert all(r["version"] == 2 and r["effective"] for r in rows)


def test_catalog_score_on_every_engine_now(real_graph):
    from src.graph import catalog

    qid = real_graph.query("MATCH (q:Query) RETURN q.id AS qid ORDER BY qid LIMIT 1")[0]["qid"]
    rows = catalog.run_entry(
        real_graph, catalog.get_entry("query_scores_now"), {"query_id": qid}
    ).rows
    direct = real_graph.query(
        "MATCH (q:Query {id: $q})-[s:SCORED {current: true}]->(e:Engine) "
        "RETURN e.id AS e, s.score AS score, s.assigned AS assigned",
        {"q": qid},
    )
    assert sorted((r["engine"], r["score"], r["assigned"]) for r in rows) == sorted(
        (r["e"], r["score"], r["assigned"]) for r in direct
    )
    assert sum(r["assigned"] for r in rows) == 1


@pytest.mark.parametrize("engine", ["documentdb", "opensearch", "dynamodb", "aurora_mysql"])
def test_an_out_of_scope_pattern_serves_nothing(tmp_path, sample_collector_output, engine):
    """``in_scope: false`` means the design does not serve the pattern (the scope
    check's rule): its query gets the ``out_of_scope`` fallback, not the object."""
    design, _ = ENGINE_SHAPES[{"aurora_mysql": "aurora_postgresql"}.get(engine, engine)]
    design = json.loads(json.dumps(design))
    for ap in design["access_patterns"]:
        ap["in_scope"] = False
        ap["out_of_scope_reason"] = "the engine cannot serve it"
    v1 = {**V1, "query_assignments": [_qa("q1", engine)]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            f"schema-{engine}/v1/schema_output.json": design,
        },
    )
    assert _by_query_version(store) == {
        ("q1", 1): {(engine, "out_of_scope", "the design does not serve it")}
    }
    assert store.query("MATCH (o:TargetObject {fallback: false}) RETURN o.id AS id") == []
    store.close()


def test_an_unsupported_pattern_serves_nothing(tmp_path, sample_collector_output):
    design = {
        "collections": [{"collection_name": "orders"}],
        "access_patterns": [],
        "unsupported_patterns": [{"source_query_ids": ["q1"], "reason": "full-text search"}],
    }
    v1 = {**V1, "query_assignments": [_qa("q1", "documentdb")]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "schema-documentdb/v1/schema_output.json": design,
        },
    )
    assert _by_query_version(store) == {
        ("q1", 1): {("documentdb", "out_of_scope", "the design does not serve it")}
    }
    store.close()


UNDEFINED = {
    # A GSI the table does not have, and a GSI name on another table.
    "dynamodb": {
        "table_definitions": [
            {"table_name": "Orders", "gsis": [{"gsi_name": "ByDate"}]},
            {"table_name": "Customers", "gsis": [{"gsi_name": "ByCustomer"}]},
        ],
        "access_patterns": [
            _ddb("DDB-AP-1", "Orders", ["q1"], gsi="ByCustomer"),
            _ddb("DDB-AP-2", "Missing", ["q1"]),
        ],
    },
    "documentdb": {
        "collections": [{"collection_name": "customers"}],
        "access_patterns": [
            {"pattern_id": "DOC-AP-1", "collection_name": "orders", "source_query_ids": ["q1"]}
        ],
    },
    "opensearch": {
        "index_designs": [{"index_name": "customers-idx"}],
        "access_patterns": [
            {"pattern_id": "OS-AP-1", "index_or_stream": "orders-idx", "query_ids": ["q1"]}
        ],
    },
    "elasticache": {
        "key_designs": [{"key_pattern": "customer:{id}"}],
        "access_patterns": [
            {"pattern_id": "EC-AP-1", "key_pattern": "order:{id}", "source_query_ids": ["q1"]}
        ],
    },
}


@pytest.mark.parametrize("engine", sorted(UNDEFINED))
def test_a_name_the_design_does_not_define_is_no_object(tmp_path, sample_collector_output, engine):
    """A pattern naming a table, GSI, collection, index or key pattern the design does
    not define creates no node: the query gets ``not_in_design``."""
    v1 = {**V1, "query_assignments": [_qa("q1", engine)]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            f"schema-{engine}/v1/schema_output.json": UNDEFINED[engine],
        },
    )
    assert _by_query_version(store) == {
        ("q1", 1): {(engine, "not_in_design", "no target in the design")}
    }
    assert store.query("MATCH (o:TargetObject {fallback: false}) RETURN o.id AS id") == []
    store.close()


def test_a_data_stream_is_an_opensearch_target(tmp_path, sample_collector_output):
    design = {
        "data_stream_designs": [{"data_stream_name": "order-events"}],
        "access_patterns": [
            {"pattern_id": "OS-AP-1", "index_or_stream": "order-events", "query_ids": ["q1"]}
        ],
    }
    v1 = {**V1, "query_assignments": [_qa("q1", "opensearch")]}
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "schema-opensearch/v1/schema_output.json": design,
        },
    )
    assert _by_query_version(store) == {("q1", 1): {("opensearch", "index", "order-events")}}
    store.close()


def test_a_rollback_targets_the_restated_design(tmp_path, sample_collector_output):
    """v1 designed; v2 reroutes q1 to OpenSearch and its design; v3 rolls v2 back and
    restates v1 with its design (copied to v3). Each version targets its own design,
    and v3's objects are v1's names at schema version 3."""
    ddb = {
        "table_definitions": [{"table_name": "Orders"}],
        "access_patterns": [_ddb("DDB-AP-1", "Orders", ["q1"])],
    }
    os_design = {
        "index_designs": [{"index_name": "orders-idx"}],
        "access_patterns": [
            {"pattern_id": "OS-AP-1", "index_or_stream": "orders-idx", "query_ids": ["q1"]}
        ],
    }
    v1 = {**V1, "query_assignments": [_qa("q1", "dynamodb")]}
    v2 = {
        **v1,
        "version": 2,
        "previous_version": 1,
        "source": "post_schema_routing",
        "status": "rerouted_after_approval",
        "query_assignments": [_qa("q1", "opensearch")],
    }
    v3 = {
        **v1,
        "version": 3,
        "previous_version": 2,
        "source": "post_schema_routing",
        "status": "reroute_rolled_back",
        "rolled_back_version": 2,
    }
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": v1,
            "assignment/v2/assignment.json": v2,
            "assignment/v3/assignment.json": v3,
            "schema-dynamodb/v1/schema_output.json": ddb,
            "schema-opensearch/v2/schema_output.json": os_design,
            "schema-dynamodb/v3/schema_output.json": ddb,
        },
    )
    rows = sorted((r["v"], r["id"], r["eff"]) for r in _targets(store))
    assert rows == [
        (1, "dynamodb:table:Orders@v1", False),
        (2, "opensearch:index:orders-idx@v2", False),
        (3, "dynamodb:table:Orders@v3", True),
    ]
    store.close()


def test_a_cache_load_test_validates_the_cache_layer(tmp_path, sample_collector_output):
    """An ElastiCache load test on a cached read validates ElastiCache, not the engine
    the query is assigned to."""
    from src.graph.populators import populate_from_load_test

    v1 = {**V1, "query_assignments": [_qa("q1", "dynamodb", cache="elasticache")]}
    store = _build(
        tmp_path,
        {"collector/output.json": sample_collector_output, "assignment/v1/assignment.json": v1},
    )
    run = {
        "pattern_results": [{"query_id": "q1", "source_latency_ms": {}, "target_latency_ms": {}}]
    }
    populate_from_load_test(run, "elasticache", 1, store)
    rows = store.query(
        "MATCH (lt:LoadTestRun)-[:VALIDATES]->(d:Destination)-[:HOSTED_ON]->(e:Engine) "
        "RETURN lt.engine AS lt, d.id AS d, e.id AS e"
    )
    assert rows == [{"lt": "elasticache", "d": "elasticache", "e": "elasticache"}]
    store.close()


def test_catalog_a_tables_queries_include_its_gsi_readers(real_graph):
    """Catalog M on a DynamoDB table lists the queries that read it and the ones that
    read one of its GSIs, each with ``via`` the object it targets."""
    from src.graph import catalog

    design = json.loads((REAL_WORDPRESS / "schema-dynamodb/v2/schema_output.json").read_text())
    table = next(t for t in design["table_definitions"] if t.get("gsis"))
    name, gsis = table["table_name"], {g["gsi_name"] for g in table["gsis"]}
    expected = set()
    for ap in design["access_patterns"]:
        if ap.get("in_scope") is False or ap["table_name"] != name:
            continue
        via = (
            f"dynamodb:gsi:{name}.{ap['gsi_name']}@v2"
            if ap.get("gsi_name") in gsis
            else f"dynamodb:table:{name}@v2"
        )
        if ap.get("gsi_name") and ap["gsi_name"] not in gsis:
            continue
        expected.update((q, via) for q in ap["query_ids"])
    rows = catalog.run_entry(
        real_graph, catalog.get_entry("target_queries"), {"target_id": f"dynamodb:table:{name}@v2"}
    ).rows
    assert {(r["query_id"], r["via"]) for r in rows} == expected
    assert any(":gsi:" in via for _, via in expected)
