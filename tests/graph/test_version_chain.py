# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The assignment version chain in the context graph (#306).

One AssignmentVersion node per version of the effective lineage (ADR-028), with
the pass that made it; SUPERSEDES to the version before; RESTORES from a rollback
to the version it restates; AFFECTS to the queries its Moves changed and the
engines they left or landed on. Consolidation and reroute Decisions link to
their queries and engines.
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


def _move(seq, qid, pass_, reason, to, frm=None, actor="rule"):
    return {
        "seq": seq,
        "query_id": qid,
        "pass": pass_,
        "from_engine": frm,
        "to_engine": to,
        "reason_code": reason,
        "actor": actor,
    }


def _qa(qid, engine):
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": ["orders"],
        "assignment_reason": "r",
    }


def _version(v, prev, source, status, moves, engines, **extra):
    return {
        "version": v,
        "previous_version": prev,
        "source": source,
        "status": status,
        "timestamp": f"2026-11-0{v}T00:00:00Z",
        "query_assignments": [_qa(q, e) for q, e in engines.items()],
        "table_assignments": [],
        "moves": moves,
        **extra,
    }


# v1 first assignment; v2 reality check: q2 consolidated documentdb -> dynamodb,
# q3 consolidated documentdb -> dynamodb then flagged on to aurora_mysql (net
# documentdb -> aurora_mysql), q1's tie cleared (no engine change); v3 customer
# pick; v4 post-schema reroute; v5 rolls v4 back, restating v3.
LINEAGE = {
    1: _version(
        1,
        None,
        "assignment_resolution",
        "auto_generated",
        [
            _move(1, "q1", "score", "highest_score", "dynamodb"),
            _move(2, "q2", "score", "highest_score", "documentdb"),
            _move(3, "q3", "score", "highest_score", "documentdb"),
        ],
        {"q1": "dynamodb", "q2": "documentdb", "q3": "documentdb"},
    ),
    2: _version(
        2,
        1,
        "reality_check",
        "auto_generated",
        [
            _move(1, "q2", "settle", "consolidate.no_unique_value", "dynamodb", frm="documentdb"),
            _move(2, "q3", "settle", "consolidate.no_unique_value", "dynamodb", frm="documentdb"),
            _move(3, "q1", "place", "tie.cleared", "dynamodb", frm="dynamodb"),
            _move(
                4,
                "q3",
                "model_flag",
                "flag.capability_gap",
                "aurora_mysql",
                frm="dynamodb",
                actor="model_flag",
            ),
        ],
        {"q1": "dynamodb", "q2": "dynamodb", "q3": "aurora_mysql"},
    ),
    3: _version(
        3,
        2,
        "customer_gate",
        "customer_modified",
        [
            _move(
                1,
                "q1",
                "customer_pick",
                "customer.pick",
                "aurora_mysql",
                frm="dynamodb",
                actor="customer",
            )
        ],
        {"q1": "aurora_mysql", "q2": "dynamodb", "q3": "aurora_mysql"},
    ),
    4: _version(
        4,
        3,
        "post_schema_routing",
        "rerouted_after_approval",
        [
            _move(
                1,
                "q3",
                "post_schema_flag",
                "flag.capability_gap",
                "opensearch",
                frm="aurora_mysql",
                actor="model_flag",
            )
        ],
        {"q1": "aurora_mysql", "q2": "dynamodb", "q3": "opensearch"},
    ),
    5: _version(
        5,
        4,
        "post_schema_routing",
        "reroute_rolled_back",
        [
            _move(
                1,
                "q3",
                "post_schema_flag",
                "rollback.redesign_failed",
                "aurora_mysql",
                frm="opensearch",
            )
        ],
        {"q1": "aurora_mysql", "q2": "dynamodb", "q3": "aurora_mysql"},
        rolled_back_version=4,
    ),
}

REALITY_CHECK = {
    "output_assignment_version": 2,
    "consolidations": [
        {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 1, "reason": "r"},
        {"from_engine": "documentdb", "to_engine": "aurora_mysql", "query_count": 1, "reason": "r"},
        {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 0, "reason": "r"},
    ],
}


def _build(tmp_path, files: dict, db="testdb", job="job-1") -> GraphStore:
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
def chain(tmp_path, sample_collector_output):
    files = {"collector/output.json": sample_collector_output}
    files.update({f"assignment/v{v}/assignment.json": a for v, a in LINEAGE.items()})
    files["reality-check/v2/output.json"] = REALITY_CHECK
    store = _build(tmp_path, files)
    yield store
    store.close()


def _versions(store):
    rows = store.query(
        "MATCH (v:AssignmentVersion) RETURN v.version AS version, v.kind AS kind, "
        "v.source AS source, v.effective AS effective, v.created_at AS created_at, "
        "v.passes_json AS passes, v.actors_json AS actors, v.move_count AS moves "
        "ORDER BY version"
    )
    return {r["version"]: r for r in rows}


def _pairs(store, rel, label_from="AssignmentVersion", label_to="AssignmentVersion"):
    return sorted(
        (r["a"], r["b"])
        for r in store.query(
            f"MATCH (a:{label_from})-[:{rel}]->(b:{label_to}) RETURN a.version AS a, b.version AS b"
        )
    )


def test_one_node_per_lineage_version_with_the_pass_that_made_it(chain):
    versions = _versions(chain)
    assert {v: r["kind"] for v, r in versions.items()} == {
        1: "first_assignment",
        2: "reality_check",
        3: "customer_pick",
        4: "post_schema_reroute",
        5: "rollback",
    }
    assert [v for v, r in versions.items() if r["effective"]] == [5]
    assert versions[3]["created_at"] == "2026-11-03T00:00:00Z"
    assert json.loads(versions[2]["passes"]) == ["model_flag", "place", "settle"]
    assert json.loads(versions[3]["actors"]) == ["customer"]
    assert json.loads(versions[4]["actors"]) == ["model_flag"]
    assert {v: r["moves"] for v, r in versions.items()} == {1: 3, 2: 4, 3: 1, 4: 1, 5: 1}


def test_supersedes_and_restores(chain):
    assert _pairs(chain, "SUPERSEDES") == [(2, 1), (3, 2), (4, 3), (5, 4)]
    # v5 rolls back v4 (rolled_back_version) and restates v3, the version v4 came from.
    assert _pairs(chain, "RESTORES") == [(5, 3)]


def test_affects_matches_each_versions_moves_exactly(chain):
    """A query whose Moves changed its engine is AFFECTS role 'moved'; one whose
    Moves changed no engine (a cleared tie) is 'tie' and adds no engine edge."""
    queries = sorted(
        (r["v"], r["q"], r["role"])
        for r in chain.query(
            "MATCH (v:AssignmentVersion)-[a:AFFECTS]->(q:Query) "
            "RETURN v.version AS v, q.id AS q, a.role AS role"
        )
    )
    engines = sorted(
        (r["v"], r["e"], r["role"])
        for r in chain.query(
            "MATCH (v:AssignmentVersion)-[a:AFFECTS]->(e:Engine) "
            "RETURN v.version AS v, e.id AS e, a.role AS role"
        )
    )
    expected_q, expected_e = {}, set()
    for v, assignment in LINEAGE.items():
        for m in assignment["moves"]:
            if m["from_engine"] == m["to_engine"]:
                expected_q.setdefault((v, m["query_id"]), "tie")
                continue
            expected_q[(v, m["query_id"])] = "moved"
            expected_e.add((v, m["to_engine"], "to"))
            if m["from_engine"]:
                expected_e.add((v, m["from_engine"], "from"))
    assert queries == sorted((v, q, role) for (v, q), role in expected_q.items())
    assert (2, "q1", "tie") in queries
    assert engines == sorted(expected_e)


def test_only_the_effective_lineage_is_chained(tmp_path, sample_collector_output):
    """A version outside the effective lineage (an abandoned resolution) gets no node."""
    files = {"collector/output.json": sample_collector_output}
    files["assignment/v1/assignment.json"] = LINEAGE[1]
    files["assignment/v2/assignment.json"] = {**LINEAGE[1], "version": 2}
    store = _build(tmp_path, files)
    assert sorted(_versions(store)) == [2]
    assert _pairs(store, "SUPERSEDES") == []
    store.close()


def _legacy(version, previous, rc_applied, engines, scope=None, status="auto_generated"):
    """A version written before ``source`` (contract 1.1) and Moves (1.10) existed:
    the old Reality Check writer left ``previous_version`` null and set
    ``reality_check_applied``; an override edit set an int ``previous_version``."""
    qas = [_qa(q, e) for q, e in engines.items()]
    for qa in qas:
        qa["in_scope"] = (scope or {}).get(qa["query_id"], True)
    return {
        "version": version,
        "previous_version": previous,
        "status": status,
        "timestamp": f"2026-01-0{version}T00:00:00Z",
        "reality_check_applied": rc_applied,
        "query_assignments": qas,
        "table_assignments": [],
    }


def test_legacy_versions_take_their_kind_from_the_legacy_shapes(tmp_path, sample_collector_output):
    """A legacy reality-check version is not a first assignment, and a legacy
    customer edit is a pick (an engine changed) or a scope edit (only scope did)."""
    on = {"q1": "dynamodb", "q2": "dynamodb", "q3": "dynamodb"}
    files = {
        "collector/output.json": sample_collector_output,
        "assignment/v1/assignment.json": _legacy(1, None, False, on),
        "assignment/v2/assignment.json": _legacy(2, None, True, on),
        "assignment/v3/assignment.json": _legacy(
            3, 2, True, {**on, "q1": "aurora_mysql"}, status="customer_modified"
        ),
        "assignment/v4/assignment.json": _legacy(
            4, 3, True, {**on, "q1": "aurora_mysql"}, {"q2": False}, "customer_modified"
        ),
    }
    store = _build(tmp_path, files)
    versions = _versions(store)
    # v2 is the lineage root (the old writer left previous_version null); v1 is no part of it.
    assert {v: r["kind"] for v, r in versions.items()} == {
        2: "reality_check",
        3: "customer_pick",
        4: "customer_scope",
    }
    assert {v: r["moves"] for v, r in versions.items()} == {2: 0, 3: 0, 4: 0}
    assert _pairs(store, "SUPERSEDES") == [(3, 2), (4, 3)]
    store.close()


def test_a_scope_only_customer_edit_is_customer_scope(tmp_path, sample_collector_output):
    scoped = {
        **LINEAGE[3],
        "moves": [],
        "query_assignments": [
            {**qa, "in_scope": qa["query_id"] != "q3"} for qa in LINEAGE[2]["query_assignments"]
        ],
    }
    files = {
        "collector/output.json": sample_collector_output,
        "assignment/v1/assignment.json": LINEAGE[1],
        "assignment/v2/assignment.json": LINEAGE[2],
        "assignment/v3/assignment.json": scoped,
    }
    store = _build(tmp_path, files)
    assert _versions(store)[3]["kind"] == "customer_scope"
    store.close()


def test_a_later_version_does_not_carry_a_stale_rolled_back_version(
    tmp_path, sample_collector_output
):
    """A source-of-truth reroute after a rollback copies ``rolled_back_version``
    forward; it is no rollback, so it records none and restores nothing."""
    after = {
        **LINEAGE[5],
        "version": 6,
        "previous_version": 5,
        "source": "source_of_truth_reroute",
        "status": "customer_modified",
        "moves": [],
    }
    files = {"collector/output.json": sample_collector_output}
    files.update({f"assignment/v{v}/assignment.json": a for v, a in LINEAGE.items()})
    files["assignment/v6/assignment.json"] = after
    store = _build(tmp_path, files)
    row = store.query(
        "MATCH (v:AssignmentVersion {version: 6}) RETURN v.kind AS kind, "
        "v.rolled_back_version AS rolled"
    )
    assert row == [{"kind": "source_of_truth_reroute", "rolled": None}]
    assert _pairs(store, "RESTORES") == [(5, 3)]
    store.close()


def _consolidation_links(store):
    rows = store.query(
        "MATCH (d:Decision {category: 'consolidation'})-[:INFORMED_BY]->(q:Query) "
        "RETURN d.id AS d, q.id AS q"
    )
    links: dict[str, set[str]] = {}
    for r in rows:
        links.setdefault(r["d"], set()).add(r["q"])
    return links


def _assert_links_match_the_records(store, reality, before, after):
    """Each consolidation links exactly the queries its record counts: the ones
    that started on its from_engine and ended on its to_engine (``moved_queries``)."""
    from src.agents.referee.reality_check_request import moved_queries

    links = _consolidation_links(store)
    for i, cons in enumerate(reality["consolidations"]):
        expected = {
            qa["query_id"]
            for qa in moved_queries(cons, after["query_assignments"], before["query_assignments"])
        }
        assert links.get(f"consolidation-{i}", set()) == expected, i
        assert len(expected) == cons["query_count"], i


def test_consolidation_decisions_link_to_their_queries_and_engines(chain):
    # q2 consolidated documentdb -> dynamodb; q3 consolidated documentdb -> dynamodb
    # and then flagged on to aurora_mysql: its net move is documentdb -> aurora_mysql.
    assert _consolidation_links(chain) == {
        "consolidation-0": {"q2"},
        "consolidation-1": {"q3"},
    }
    _assert_links_match_the_records(chain, REALITY_CHECK, LINEAGE[1], LINEAGE[2])
    engines = sorted(
        (r["d"], r["e"], r["role"])
        for r in chain.query(
            "MATCH (d:Decision {category: 'consolidation'})-[a:AFFECTS]->(e:Engine) "
            "RETURN d.id AS d, e.id AS e, a.role AS role"
        )
    )
    assert engines == [
        ("consolidation-0", "documentdb", "from"),
        ("consolidation-0", "dynamodb", "to"),
        ("consolidation-1", "aurora_mysql", "to"),
        ("consolidation-1", "documentdb", "from"),
        ("consolidation-2", "aurora_mysql", "to"),
        ("consolidation-2", "opensearch", "from"),
    ]


def test_reroute_decisions_link_to_their_engines(tmp_path, sample_collector_output):
    files = {
        "collector/output.json": sample_collector_output,
        "assignment/v1/assignment.json": LINEAGE[1],
        "post-schema-router/router_output.json": {
            "routings": [
                {
                    "query_id": "q3",
                    "from_engine": "dynamodb",
                    "to_engine": "opensearch",
                    "reason": "r",
                },
                {"query_id": "q1", "from_engine": "dynamodb", "to_engine": None, "reason": "r"},
            ]
        },
    }
    store = _build(tmp_path, files)
    rows = sorted(
        (r["d"], r["e"], r["role"])
        for r in store.query(
            "MATCH (d:Decision {category: 'reroute'})-[a:AFFECTS]->(e:Engine) "
            "RETURN d.id AS d, e.id AS e, a.role AS role"
        )
    )
    assert rows == [
        ("reroute-0", "dynamodb", "from"),
        ("reroute-0", "opensearch", "to"),
        ("reroute-1", "dynamodb", "from"),
    ]
    store.close()


@pytest.mark.passes_on_main
def test_a_job_without_an_assignment_has_no_chain(tmp_path, sample_collector_output):
    """A job with collected queries but no assignment yet builds a graph with no
    version node and no version edge."""
    store = _build(tmp_path, {"collector/output.json": sample_collector_output})
    labels = {r["l"] for r in store.query("MATCH (n) RETURN DISTINCT label(n) AS l")}
    assert "Query" in labels
    assert "AssignmentVersion" not in labels
    store.close()


def test_real_wordpress_run(tmp_path):
    """The real WordPress run: v1 (first assignment) and v2 (the reality check,
    effective); AFFECTS equals each version's Moves; the three consolidations link
    to the 8, 2 and 1 queries their records count (each query's net move)."""
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_WORDPRESS, base / "wordpress" / "job")
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)

    versions = _versions(store)
    assert {v: (r["kind"], r["effective"]) for v, r in versions.items()} == {
        1: ("first_assignment", False),
        2: ("reality_check", True),
    }
    assert _pairs(store, "SUPERSEDES") == [(2, 1)]
    assert _pairs(store, "RESTORES") == []

    for v in (1, 2):
        moves = json.loads((REAL_WORDPRESS / f"assignment/v{v}/assignment.json").read_text())[
            "moves"
        ]
        rows = store.query(
            "MATCH (v:AssignmentVersion {version: $v})-[:AFFECTS]->(q:Query) RETURN q.id AS q",
            {"v": v},
        )
        assert sorted(r["q"] for r in rows) == sorted({m["query_id"] for m in moves})
    assert versions[2]["moves"] == 13

    counts = {
        r["d"]: r["c"]
        for r in store.query(
            "MATCH (d:Decision {category: 'consolidation'})-[:INFORMED_BY]->(q:Query) "
            "RETURN d.id AS d, COUNT(q) AS c"
        )
    }
    assert counts == {"consolidation-0": 8, "consolidation-1": 2, "consolidation-2": 1}
    v1, v2 = (
        json.loads((REAL_WORDPRESS / f"assignment/v{v}/assignment.json").read_text())
        for v in (1, 2)
    )
    reality = json.loads((REAL_WORDPRESS / "reality-check/v2/output.json").read_text())
    _assert_links_match_the_records(store, reality, v1, v2)
    store.close()
