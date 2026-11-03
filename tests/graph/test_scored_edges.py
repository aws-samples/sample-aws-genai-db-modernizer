# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SCORED edges in the context graph (#306): each query's candidate ladder, per
assignment version, as routing recorded it in the Moves' evidence, with the score
dimensions of the version's access-pattern record where it holds them (#1471).
Nothing is rescored."""

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


def _rung(engine, score, gate=None):
    return {"engine": engine, "score": score, "gate": gate}


def _move(seq, qid, pass_, to, candidates, frm=None, reason="highest_score"):
    return {
        "seq": seq,
        "query_id": qid,
        "pass": pass_,
        "from_engine": frm,
        "to_engine": to,
        "reason_code": reason,
        "actor": "rule",
        "evidence": {"candidates": candidates},
    }


def _qa(qid, engine):
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": ["orders"],
        "assignment_reason": "r",
    }


V1 = {
    "version": 1,
    "previous_version": None,
    "source": "assignment_resolution",
    "status": "auto_generated",
    "query_assignments": [_qa("q1", "dynamodb"), _qa("q2", "dynamodb"), _qa("q3", "dynamodb")],
    "table_assignments": [],
    "moves": [
        _move(
            1,
            "q1",
            "score",
            "dynamodb",
            [
                _rung("dynamodb", 80.0),
                _rung("documentdb", 70.0),
                _rung("opensearch", None, "capability"),
            ],
        ),
        # q2 moves twice in v1: its ladder in v1 is the one its last Move recorded.
        _move(2, "q2", "score", "documentdb", [_rung("documentdb", 75.0), _rung("dynamodb", 60.0)]),
        _move(
            3,
            "q2",
            "co_dependency",
            "dynamodb",
            [_rung("dynamodb", 65.0), _rung("documentdb", 75.0)],
            frm="documentdb",
            reason="group.mean",
        ),
        _move(4, "q3", "score", "dynamodb", [_rung("dynamodb", 90.0)]),
    ],
}
V2 = {
    "version": 2,
    "previous_version": 1,
    "source": "reality_check",
    "status": "auto_generated",
    "query_assignments": [_qa("q1", "documentdb"), _qa("q2", "dynamodb"), _qa("q3", "dynamodb")],
    "table_assignments": [],
    "moves": [
        _move(
            1,
            "q1",
            "settle",
            "documentdb",
            [_rung("dynamodb", 80.0), _rung("documentdb", 70.0), _rung("aurora_mysql", 50.0)],
            frm="dynamodb",
            reason="consolidate.no_unique_value",
        )
    ],
}
# v1's record predates the dimensions (schema version 3); v2's holds them (version 4).
RECORD_V1 = {"schema_version": 3, "queries": [{"query_id": "q1", "tables": ["orders"]}]}
COMMON = {"pattern_match": 0.4, "complexity": 0.2, "performance": 0.2, "cost": 0.2}
OWN = {"pattern_match": 0.5, "complexity": 0.2, "performance": 0.2, "cost": 0.1}
RECORD_V2 = {
    "schema_version": 4,
    "queries": [
        {
            "query_id": "q1",
            "tables": ["orders"],
            "score_dimensions": {
                "dynamodb": {"pattern": 90.0, "cost": 60.0, "base": 80.0},
                "documentdb": {
                    "pattern": 70.0,
                    "cost": 80.0,
                    "base": 72.0,
                    "penalty": 2.0,
                    "fit": 70.0,
                },
            },
        }
    ],
    "table_dimensions": {
        "dynamodb": {
            "weights": COMMON,
            "tables": {"orders": {"complexity": 70.0, "performance": 85.0}},
        },
        "documentdb": {
            "weights": COMMON,
            "tables": {"orders": {"complexity": 60.0, "performance": 75.0, "weights": OWN}},
        },
    },
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


def _scored(store):
    rows = store.query(
        "MATCH (q:Query)-[s:SCORED]->(e:Engine) RETURN q.id AS q, e.id AS e, "
        "s.version AS v, s.score AS score, s.gate AS gate, s.assigned AS assigned, "
        "s.current AS current, s.ladder_version AS lv, s.move_seq AS seq, s.pass AS pass, "
        "s.ladder_copied_from AS copied, s.dimensions_json AS dims"
    )
    return {(r["q"], r["v"], r["e"]): r for r in rows}


def _chain(tmp_path, sample_collector_output, v2=None, record_v2=None):
    return _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": V1,
            "assignment/v1/access_patterns.json": RECORD_V1,
            "assignment/v2/assignment.json": v2 or V2,
            "assignment/v2/access_patterns.json": record_v2 or RECORD_V2,
        },
    )


def test_every_rung_of_each_versions_ladder_with_its_score(tmp_path, sample_collector_output):
    """v1: every query (each has a Move). v2, the effective version: every query; q1
    with the ladder its v2 Move recorded, q2 and q3 with their v1 ladders, saying so."""
    store = _chain(tmp_path, sample_collector_output)
    scored = _scored(store)
    v1 = {
        ("q1", 1, "dynamodb"): 80.0,
        ("q1", 1, "documentdb"): 70.0,
        ("q1", 1, "opensearch"): None,
        # q2's last Move in v1, not its first.
        ("q2", 1, "dynamodb"): 65.0,
        ("q2", 1, "documentdb"): 75.0,
        ("q3", 1, "dynamodb"): 90.0,
    }
    v2 = {
        ("q1", 2, "dynamodb"): 80.0,
        ("q1", 2, "documentdb"): 70.0,
        ("q1", 2, "aurora_mysql"): 50.0,
        ("q2", 2, "dynamodb"): 65.0,
        ("q2", 2, "documentdb"): 75.0,
        ("q3", 2, "dynamodb"): 90.0,
    }
    assert {k: r["score"] for k, r in scored.items()} == {**v1, **v2}
    provenance = {(q, v): (r["lv"], r["seq"], r["pass"]) for (q, v, _), r in scored.items()}
    assert provenance == {
        ("q1", 1): (1, 1, "score"),
        ("q2", 1): (1, 3, "co_dependency"),
        ("q3", 1): (1, 4, "score"),
        ("q1", 2): (2, 1, "settle"),
        ("q2", 2): (1, 3, "co_dependency"),
        ("q3", 2): (1, 4, "score"),
    }
    store.close()


def test_gates(tmp_path, sample_collector_output):
    store = _chain(tmp_path, sample_collector_output)
    gated = {k: r["gate"] for k, r in _scored(store).items() if r["gate"]}
    assert gated == {("q1", 1, "opensearch"): "capability"}
    store.close()


def test_assigned_and_current_flags(tmp_path, sample_collector_output):
    """``assigned``: the version's own assigned_engine. ``current``: the edges of the
    effective version, one set per query."""
    store = _chain(tmp_path, sample_collector_output)
    scored = _scored(store)
    assert sorted(k for k, r in scored.items() if r["assigned"]) == [
        ("q1", 1, "dynamodb"),
        ("q1", 2, "documentdb"),
        ("q2", 1, "dynamodb"),
        ("q2", 2, "dynamodb"),
        ("q3", 1, "dynamodb"),
        ("q3", 2, "dynamodb"),
    ]
    assert {v for (_, v, _), r in scored.items() if r["current"]} == {2}
    assert {v for (_, v, _), r in scored.items() if not r["current"]} == {1}
    store.close()


# Moves that record no candidate ladder: the query keeps its earlier ladder.
CANDIDATE_LESS = [
    ("customer_pick", "customer.pick", "customer"),
    ("customer_group_propagation", "group.customer_propagation", "customer"),
    ("auto_pick", "tie.decided_for_you", "rule"),
    ("co_dependency", "group.write", "rule"),
]


@pytest.mark.parametrize("pass_, reason, actor", CANDIDATE_LESS)
def test_a_move_without_a_ladder_keeps_the_earlier_ladder_and_the_new_engine(
    tmp_path, sample_collector_output, pass_, reason, actor
):
    """v1 puts q1 on DynamoDB; v2 moves it to DocumentDB by a Move that records no
    ladder. Its score on every engine now is v1's ladder, and DocumentDB is assigned."""
    move = {**_move(1, "q1", pass_, "documentdb", [], frm="dynamodb", reason=reason)}
    move["actor"] = actor
    move["evidence"] = {}
    v2 = {
        **V2,
        "source": "customer_gate",
        "status": "customer_modified",
        "moves": [move],
    }
    store = _chain(tmp_path, sample_collector_output, v2=v2, record_v2=RECORD_V1)
    now = {
        e: (r["score"], r["assigned"], r["lv"], r["seq"], r["pass"])
        for (q, v, e), r in _scored(store).items()
        if q == "q1" and r["current"]
    }
    assert now == {
        "dynamodb": (80.0, False, 1, 1, "score"),
        "documentdb": (70.0, True, 1, 1, "score"),
        "opensearch": (None, False, 1, 1, "score"),
    }
    store.close()


def test_a_rollback_ladder_is_marked_copied_from_the_reroute(tmp_path, sample_collector_output):
    """A rollback's Move copies the abandoned reroute's candidates: the edge keeps that
    Move's own pass (as MOVED does) and names the reroute version it copied from; the
    restored engine is assigned."""
    flagged = [_rung("opensearch", 70.0), _rung("dynamodb", None, "model_flag")]
    reroute = {
        **V2,
        "source": "post_schema_routing",
        "status": "rerouted_after_approval",
        "query_assignments": [
            _qa("q1", "dynamodb"),
            _qa("q2", "dynamodb"),
            _qa("q3", "opensearch"),
        ],
        "moves": [_move(1, "q3", "post_schema_flag", "opensearch", flagged, frm="dynamodb")],
    }
    rollback = {
        **reroute,
        "version": 3,
        "previous_version": 2,
        "status": "reroute_rolled_back",
        "rolled_back_version": 2,
        "query_assignments": V1["query_assignments"],
        "moves": [
            _move(
                1,
                "q3",
                "post_schema_flag",
                "dynamodb",
                flagged,
                frm="opensearch",
                reason="rollback.redesign_failed",
            )
        ],
    }
    store = _build(
        tmp_path,
        {
            "collector/output.json": sample_collector_output,
            "assignment/v1/assignment.json": V1,
            "assignment/v2/assignment.json": reroute,
            "assignment/v3/assignment.json": rollback,
        },
    )
    now = {
        e: (r["score"], r["gate"], r["assigned"], r["pass"], r["copied"], r["lv"])
        for (q, v, e), r in _scored(store).items()
        if q == "q3" and r["current"]
    }
    assert now == {
        "opensearch": (70.0, None, False, "post_schema_flag", 2, 3),
        "dynamodb": (None, "model_flag", True, "post_schema_flag", 2, 3),
    }
    moved = store.query(
        "MATCH (q:Query {id: 'q3'})-[m:MOVED {version: 3}]->(:Engine) RETURN m.pass AS pass"
    )
    assert moved == [{"pass": "post_schema_flag"}]
    assert all(r["copied"] is None for (q, v, _), r in _scored(store).items() if v < 3)
    store.close()


def test_dimensions_when_they_explain_the_score(tmp_path, sample_collector_output):
    store = _chain(tmp_path, sample_collector_output)
    scored = _scored(store)
    assert json.loads(scored[("q1", 2, "dynamodb")]["dims"]) == {
        "pattern": 90.0,
        "cost": 60.0,
        "base": 80.0,
        "tables": {"orders": {"complexity": 70.0, "performance": 85.0, "weights": COMMON}},
    }
    assert json.loads(scored[("q1", 2, "documentdb")]["dims"]) == {
        "pattern": 70.0,
        "cost": 80.0,
        "base": 72.0,
        "penalty": 2.0,
        "fit": 70.0,
        "tables": {"orders": {"complexity": 60.0, "performance": 75.0, "weights": OWN}},
    }
    # No dimensions recorded for that engine, and none in a version 3 record.
    assert scored[("q1", 2, "aurora_mysql")]["dims"] is None
    assert all(r["dims"] is None for (q, v, e), r in scored.items() if v == 1)
    store.close()


def test_dimensions_of_another_score_are_not_attached(tmp_path, sample_collector_output):
    """A record whose score for the engine is not the edge's (a reroute or rollback
    carries the previous record's dimensions) gives the edge none."""
    other = json.loads(json.dumps(RECORD_V2))
    other["queries"][0]["score_dimensions"]["dynamodb"]["base"] = 79.0
    store = _chain(tmp_path, sample_collector_output, record_v2=other)
    scored = _scored(store)
    assert scored[("q1", 2, "dynamodb")]["dims"] is None
    assert scored[("q1", 2, "documentdb")]["dims"] is not None
    store.close()


def test_a_version_without_a_ladder_has_no_scored_edges(tmp_path, sample_collector_output):
    """A Move written without evidence candidates gives no edge; nothing is rescored."""
    bare = {**V1, "moves": [{**m, "evidence": {}} for m in V1["moves"]]}
    store = _build(
        tmp_path,
        {"collector/output.json": sample_collector_output, "assignment/v1/assignment.json": bare},
    )
    assert _scored(store) == {}
    store.close()


def test_rebuilding_twice_gives_the_same_edges(tmp_path, sample_collector_output):
    from src.graph.populators import populate_scored

    store = _chain(tmp_path, sample_collector_output)
    count = "MATCH ()-[s:SCORED]->() RETURN COUNT(s) AS c"
    before, edges = _scored(store), store.query(count)[0]["c"]
    populate_scored([V1, V2], 2, {1: RECORD_V1, 2: RECORD_V2}, store)
    assert _scored(store) == before
    assert store.query(count)[0]["c"] == edges == len(before)
    store.close()


def _expected_edges(job: Path, versions: list[int], effective: int) -> dict:
    """Independent of the populator: walk the lineage, keep each query's last ladder,
    and list the edges of each version a query moves in and of the effective one."""
    expected, ladder = {}, {}
    for v in versions:
        assignment = json.loads((job / f"assignment/v{v}/assignment.json").read_text())
        moves = sorted(assignment.get("moves") or [], key=lambda m: m["seq"])
        for m in moves:
            if (m.get("evidence") or {}).get("candidates"):
                ladder[m["query_id"]] = (v, m)
        assigned = {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}
        wanted = {m["query_id"] for m in moves} | (set(assigned) if v == effective else set())
        for qid in wanted & set(ladder):
            lv, m = ladder[qid]
            for rung in m["evidence"]["candidates"]:
                expected[(qid, v, rung["engine"])] = (
                    rung["score"],
                    rung["gate"],
                    rung["engine"] == assigned.get(qid),
                    v == effective,
                    lv,
                )
    return expected


def test_real_wordpress_run(tmp_path):
    """The real WordPress run (v1, effective v2; access-pattern records of schema
    version 3): v1's edges for every query it moved, v2's for every query, each with
    the ladder, scores, gates and assigned engine the artifacts record; no dimensions."""
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_WORDPRESS, base / "wordpress" / "job")
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    scored = _scored(store)
    expected = _expected_edges(REAL_WORDPRESS, [1, 2], 2)
    assert {
        k: (r["score"], r["gate"], r["assigned"], r["current"], r["lv"]) for k, r in scored.items()
    } == expected
    assert len(scored) == 1070
    assert all(r["dims"] is None for r in scored.values())
    store.close()


COOKBOOK = Path(__file__).parents[2] / "docs/guides/context-graph-query-cookbook.md"


def test_the_cookbook_score_on_every_engine_query_runs(tmp_path, sample_collector_output):
    """The cookbook's "score on every engine now" query runs as written and reads
    the query's latest ladder (q1's is v2's)."""
    text = COOKBOOK.read_text()
    start = text.index("`MATCH (q:Query {id: $qid})-[s:SCORED {current: true}]")
    cypher = text[start + 1 : text.index("`", start + 1)]
    store = _chain(tmp_path, sample_collector_output)
    rows = store.query(cypher, {"qid": "q1"})
    assert sorted(
        (r["e.id"], r["s.score"], r["s.assigned"], r["s.ladder_version"], r["s.pass"]) for r in rows
    ) == [
        ("aurora_mysql", 50.0, False, 2, "settle"),
        ("documentdb", 70.0, True, 2, "settle"),
        ("dynamodb", 80.0, False, 2, "settle"),
    ]
    store.close()
