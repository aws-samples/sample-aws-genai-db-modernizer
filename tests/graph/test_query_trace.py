"""The per-query decision trace (#339): MOVED edges from the move ledger, the trace, the API.

The fixture is the wordpress sample's own move ledger (deterministic pipeline,
assignment v1 and the reality check's v2), trimmed to seven queries: two the
reality check moved off DocumentDB, a tie the reality check settled, a utility
pin over a scored pick, a co-dependency group member, a decided tie, and a
query that never moved after its first assignment.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agents.referee.move_ledger import replay
from src.api.models.graph_responses import QueryTraceResponse
from src.api.routes import graph as graph_routes
from src.contracts.move_ledger import Move
from src.graph.populators import (
    assignment_lineage,
    populate_from_assignment,
    populate_from_collector,
    populate_from_moves,
    rebuild_graph,
)
from src.graph.queries import query_journey, query_journeys, query_trace
from src.graph.schema import GRAPH_SCHEMA_REVISION, NODE_TABLES, REL_TABLES, initialize_schema
from src.graph.store import GraphStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wordpress_move_trace.json"


@pytest.fixture(scope="module")
def sample() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def _v2_move(sample: dict, **match: object) -> str:
    """The query id of the reality check's (v2) Move matching every field in ``match``."""
    qid: str = next(
        m["query_id"] for m in sample["v2"]["moves"] if all(m.get(k) == v for k, v in match.items())
    )
    return qid


def _settled_with_runner_up(sample: dict) -> str:
    # The REPLACE INTO wp_wc_product_meta_lookup query: DocumentDB 77 -> DynamoDB 69.
    return _v2_move(sample, reason_code="consolidate.no_unique_value", score_before=77.0)


def _settled_without_runner_up(sample: dict) -> str:
    # The wp_actionscheduler_actions LEFT JOIN: DocumentDB 87 -> Aurora MySQL 82.
    return _v2_move(sample, reason_code="consolidate.no_unique_value", score_before=87.0)


def _tie_cleared(sample: dict) -> str:
    return _v2_move(sample, reason_code="tie.cleared")


def _utility_pin(sample: dict) -> str:
    qid: str = next(m["query_id"] for m in sample["v1"]["moves"] if m["pass"] == "utility_pin")
    return qid


def _never_moved(sample: dict) -> str:
    moved = {m["query_id"] for m in sample["v2"]["moves"]}
    counts: dict[str, int] = {}
    for m in sample["v1"]["moves"]:
        counts[m["query_id"]] = counts.get(m["query_id"], 0) + 1
    qid: str = next(
        m["query_id"]
        for m in sample["v1"]["moves"]
        if m["reason_code"] == "highest_score"
        and counts[m["query_id"]] == 1
        and m["query_id"] not in moved
    )
    return qid


@pytest.fixture
def traced(graph_store, sample):
    """A graph with the collector, the effective assignment (v2) and both versions' moves."""
    populate_from_collector(sample["collector"], graph_store)
    populate_from_assignment(sample["v2"], graph_store)
    for version in (sample["v1"], sample["v2"]):
        populate_from_moves(version, graph_store)
    return graph_store


def _moved_edges(store: GraphStore) -> list[dict]:
    return store.query(
        "MATCH (q:Query)-[m:MOVED]->(e:Engine) RETURN q.id AS qid, m.version AS v, "
        "m.seq AS seq ORDER BY v, seq"
    )


class TestMovedEdges:
    def test_one_edge_per_move_of_every_version(self, traced, sample):
        edges = _moved_edges(traced)
        expected = [(1, m["seq"]) for m in sample["v1"]["moves"]] + [
            (2, m["seq"]) for m in sample["v2"]["moves"]
        ]
        assert [(e["v"], e["seq"]) for e in edges] == expected

    def test_history_is_kept_not_overwritten(self, traced, sample):
        qid = _settled_with_runner_up(sample)
        rows = traced.query(
            "MATCH (q:Query {id: $q})-[m:MOVED]->(e:Engine) "
            "RETURN m.version AS v, e.id AS engine ORDER BY v, m.seq",
            {"q": qid},
        )
        assert [(r["v"], r["engine"]) for r in rows] == [(1, "documentdb"), (2, "dynamodb")]

    def test_repopulating_a_version_adds_nothing(self, traced, sample):
        before = len(_moved_edges(traced))
        populate_from_moves(sample["v2"], traced)
        assert len(_moved_edges(traced)) == before

    def test_a_version_before_the_ledger_writes_nothing(self, graph_store, sample):
        populate_from_collector(sample["collector"], graph_store)
        legacy = {k: v for k, v in sample["v1"].items() if k != "moves"}
        assert populate_from_moves(legacy, graph_store) == 0
        assert _moved_edges(graph_store) == []

    def test_an_unreadable_move_is_skipped_not_fatal(self, graph_store, sample):
        populate_from_collector(sample["collector"], graph_store)
        broken = copy.deepcopy(sample["v2"])
        broken["moves"].append({"seq": 99, "query_id": "x"})
        assert populate_from_moves(broken, graph_store) == len(sample["v2"]["moves"])

    def test_engine_nodes_use_display_names(self, traced):
        names = {
            r["id"]: r["name"]
            for r in traced.query("MATCH (e:Engine) RETURN e.id AS id, e.display_name AS name")
        }
        assert names["documentdb"] == "DocumentDB"
        assert names["aurora_mysql"] == "Aurora MySQL"


class TestQueryTrace:
    def test_unknown_query(self, traced):
        assert query_trace(traced, "nope") is None

    def test_final_engine_replays_from_the_moves(self, traced, sample):
        # The trace's chain ends where the ledger's replay of v1 + v2 ends,
        # which is the effective assignment's engine, for every query.
        start = replay(Move.model_validate(m) for m in sample["v1"]["moves"])
        end = replay((Move.model_validate(m) for m in sample["v2"]["moves"]), start=start)
        for qa in sample["v2"]["query_assignments"]:
            trace = query_trace(traced, qa["query_id"])
            assert trace is not None and trace.has_ledger
            assert trace.final_engine == qa["assigned_engine"] == end[qa["query_id"]][0]
            steps = [trace.first_assignment, *trace.moves]
            assert steps[-1].to_engine == trace.final_engine
            assert [(s.version, s.seq) for s in steps] == sorted((s.version, s.seq) for s in steps)

    def test_reality_check_move_with_runner_up(self, traced, sample):
        trace = query_trace(traced, _settled_with_runner_up(sample))
        first = trace.first_assignment
        assert first.version == 1 and first.from_engine is None
        assert first.to_engine == "documentdb" and first.score_after == 77.0
        # The first assignment's candidate ladder: every engine's score or gate.
        ladder = {c.engine: (c.score, c.gate) for c in first.candidates}
        assert ladder["documentdb"] == (77.0, None)
        assert any(gate for _, gate in ladder.values())
        assert all(c.gate_text for c in first.candidates if c.gate)
        [rc] = trace.moves
        assert (rc.version, rc.pass_.value, rc.reason_code.value) == (
            2,
            "settle",
            "consolidate.no_unique_value",
        )
        assert (rc.from_engine, rc.to_engine, rc.score_before, rc.score_after) == (
            "documentdb",
            "dynamodb",
            77.0,
            69.0,
        )
        assert rc.explanation.startswith("Moved from DocumentDB to DynamoDB (77 to 69)")
        assert trace.final_engine == "dynamodb" and trace.final_score == 69.0
        ru = trace.runner_up
        assert (ru.engine, ru.score, ru.gap, ru.gate) == ("aurora_mysql", 54.0, 15.0, None)
        assert ru.explanation == "Aurora MySQL was next at 54, 15 points behind DynamoDB (69)."
        assert trace.summary.startswith("Ends on DynamoDB (69) after 2 routing steps.")

    def test_a_higher_scoring_removed_engine_is_outscored_not_runner_up(self, traced, sample):
        # The reality check left Aurora MySQL as the only engine for this query
        # (DocumentDB, 87, removed); the plan's other engine, DynamoDB, is gated.
        trace = query_trace(traced, _settled_without_runner_up(sample))
        ru = trace.runner_up
        assert (ru.engine, ru.gate, ru.gap) == ("dynamodb", "capability", None)
        assert ru.explanation == (
            "DynamoDB could not take the query: missing a capability the query needs."
        )
        [better] = trace.outscored_by
        assert (better.engine, better.score, better.final_score) == ("documentdb", 87.0, 82.0)
        assert (better.decided_by_version, better.decided_by_rule.value, better.left_it) == (
            2,
            "consolidate.no_unique_value",
            True,
        )
        assert better.in_plan is False
        assert "the query left it in version 2" in better.explanation

    def test_runner_up_is_in_the_plan_and_never_behind(self, traced, sample):
        plan = {qa["assigned_engine"] for qa in sample["v2"]["query_assignments"]}
        for qa in sample["v2"]["query_assignments"]:
            trace = query_trace(traced, qa["query_id"])
            if trace.runner_up is not None:
                assert trace.runner_up.engine in plan
                assert trace.runner_up.gap is None or trace.runner_up.gap >= 0
            for better in trace.outscored_by:
                assert better.score > better.final_score and better.explanation

    def test_tie_then_cleared(self, traced, sample):
        trace = query_trace(traced, _tie_cleared(sample))
        assert trace.first_assignment.tied_engines == ["documentdb", "dynamodb"]
        assert trace.first_assignment.tied_engine_names == ["DocumentDB", "DynamoDB"]
        assert trace.moves[-1].reason_code.value == "tie.cleared"
        assert trace.tied_engines == [] and trace.tied_engine_names == []
        # DocumentDB, its tie partner, left the plan: the runner-up is the plan's next engine.
        assert (trace.runner_up.engine, trace.runner_up.gap) == ("aurora_mysql", 5.0)

    def test_a_decided_tie_names_its_rule(self, traced, sample):
        qid = next(
            m["query_id"]
            for m in sample["v1"]["moves"]
            if m["reason_code"] == "tie.source_compatible"
        )
        ru = query_trace(traced, qid).runner_up
        assert ru.gap == 0.0
        assert ru.explanation.endswith(
            "the tie went to the engine compatible with the source database."
        )

    def test_a_group_member_compares_group_means(self, traced, sample):
        qid = next(m["query_id"] for m in sample["v1"]["moves"] if m["reason_code"] == "group.mean")
        move = next(m for m in sample["v1"]["moves"] if m["query_id"] == qid)
        trace = query_trace(traced, qid)
        assert trace.runner_up.basis == "group_mean"
        assert trace.runner_up.final_score == move["evidence"]["group_scores"]["aurora_mysql"]

    def test_a_group_no_other_plan_engine_can_own_names_the_group_gate(self, graph_store, sample):
        # Like discourse's groups that only one engine can own: the plan's other
        # engine scores the query but is missing from the group's means.
        v1 = copy.deepcopy(sample["v1"])
        move = next(m for m in v1["moves"] if m["reason_code"] == "group.mean")
        move["evidence"]["group_scores"] = {"aurora_mysql": 80.0}
        for rung in move["evidence"]["candidates"]:
            if rung["engine"] == "dynamodb":
                rung.update(score=70.0, gate=None)
        populate_from_collector(sample["collector"], graph_store)
        populate_from_assignment(sample["v2"], graph_store)
        for version in (v1, sample["v2"]):
            populate_from_moves(version, graph_store)
        ru = query_trace(graph_store, move["query_id"]).runner_up
        assert (ru.engine, ru.gate, ru.gap, ru.basis) == (
            "dynamodb",
            "group_owner",
            None,
            "group_mean",
        )
        assert ru.explanation == (
            "DynamoDB could not take the query: cannot own every query in its co-dependency group."
        )

    def test_customer_pick_below_the_best_engine(self, graph_store, sample):
        # A customer version (v3) puts the REPLACE INTO query on Aurora MySQL (54),
        # below DynamoDB (69): DynamoDB is outscoring, decided by the customer's pick.
        qid = _settled_with_runner_up(sample)
        v3 = copy.deepcopy(sample["v2"])
        v3.update(version=3, previous_version=2, source="customer_gate")
        for qa in v3["query_assignments"]:
            if qa["query_id"] == qid:
                qa["assigned_engine"] = "aurora_mysql"
        rc = next(m for m in sample["v2"]["moves"] if m["query_id"] == qid)
        v3["moves"] = [
            {
                **rc,
                "seq": 1,
                "pass": "customer_pick",
                "reason_code": "customer.pick",
                "actor": "customer",
                "from_engine": "dynamodb",
                "to_engine": "aurora_mysql",
                "score_before": 69.0,
                "score_after": 54.0,
                "runner_up": {"engine": "dynamodb", "score": 69.0},
            }
        ]
        populate_from_collector(sample["collector"], graph_store)
        populate_from_assignment(v3, graph_store)
        for version in (sample["v1"], sample["v2"], v3):
            populate_from_moves(version, graph_store)
        trace = query_trace(graph_store, qid)
        assert trace.final_engine == "aurora_mysql" and trace.final_version == 3
        assert trace.runner_up is None or trace.runner_up.engine != "dynamodb"
        by_engine = {b.engine: b for b in trace.outscored_by}
        assert by_engine["dynamodb"].in_plan is True
        assert (
            by_engine["dynamodb"].decided_by_version,
            by_engine["dynamodb"].decided_by_rule.value,
        ) == (3, "customer.pick")
        assert by_engine["documentdb"].decided_by_version == 2
        assert [b.engine for b in trace.outscored_by] == ["documentdb", "dynamodb"]

    def test_v1_override_is_a_later_step_of_the_first_version(self, traced, sample):
        trace = query_trace(traced, _utility_pin(sample))
        assert trace.first_assignment.reason_code.value == "highest_score"
        [pin] = trace.moves
        assert (pin.version, pin.pass_.value) == (1, "utility_pin")
        assert pin.explanation.startswith("Pinned to Aurora MySQL")

    def test_query_that_never_moved(self, traced, sample):
        trace = query_trace(traced, _never_moved(sample))
        assert trace.moves == []
        assert trace.final_engine == trace.first_assignment.to_engine

    def test_no_ledger_job(self, graph_store, sample):
        populate_from_collector(sample["collector"], graph_store)
        populate_from_assignment(sample["v2"], graph_store)
        qa = sample["v2"]["query_assignments"][0]
        trace = query_trace(graph_store, qa["query_id"])
        assert trace.has_ledger is False and trace.first_assignment is None
        assert trace.final_engine == qa["assigned_engine"]

    def test_explanations_are_public_wording(self, traced, sample):
        for qa in sample["v2"]["query_assignments"]:
            trace = query_trace(traced, qa["query_id"])
            texts = [s.explanation for s in [trace.first_assignment, *trace.moves]]
            texts += [trace.summary, trace.runner_up.explanation if trace.runner_up else ""]
            for text in texts:
                for raw in ("aurora_mysql", "documentdb", "dynamodb", "consolidate.", "tie."):
                    assert raw not in text, text


def _main_schema_graph(path: str, sample: dict) -> GraphStore:
    """A graph as main (before #339) built and saved it: no GraphMeta, no MOVED table."""
    store = GraphStore(path)
    for ddl in NODE_TABLES:
        if "GraphMeta" not in ddl:
            store.execute(ddl)
    for ddl in REL_TABLES:
        if "MOVED" not in ddl:
            store.execute(ddl)
    populate_from_collector(sample["collector"], store)
    populate_from_assignment(sample["v2"], store)
    return store


class TestGraphSavedBeforeThisChange:
    def test_trace_and_journey_still_answer(self, tmp_path, sample):
        store = _main_schema_graph(str(tmp_path / "old.lbug"), sample)
        try:
            qa = sample["v2"]["query_assignments"][0]
            trace = query_trace(store, qa["query_id"])
            assert trace.has_ledger is False and trace.final_engine == qa["assigned_engine"]
            assert trace.effective_version is None
            journey = query_journey(store, qa["query_id"])
            assert journey["trace"]["has_ledger"] is False
        finally:
            store.close()

    def test_a_saved_graph_of_an_older_schema_is_rebuilt(self, tmp_path, sample):
        path = str(tmp_path / "old.lbug")
        _main_schema_graph(path, sample).close()
        assert graph_routes._is_populated_file(path) is False

    def test_a_graph_of_this_revision_is_reused(self, tmp_path, sample):
        path = str(tmp_path / "new.lbug")
        store = GraphStore(path)
        initialize_schema(store)
        rebuild_graph("wordpress", "job", _DirStore(TestRebuild()._files(sample)), store)
        meta = store.graph_meta()
        store.close()
        assert meta == {"schema_revision": GRAPH_SCHEMA_REVISION, "effective_version": 2}
        assert graph_routes._is_populated_file(path) is True

    def test_trace_endpoint_on_an_old_graph(self, tmp_path, sample):
        store = _main_schema_graph(str(tmp_path / "old.lbug"), sample)
        app = FastAPI()
        app.include_router(graph_routes.router)
        app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (store, "wordpress")
        try:
            qid = sample["v2"]["query_assignments"][0]["query_id"]
            resp = TestClient(app).get(f"/api/v1/assessments/job-1/graph/queries/{qid}/trace")
            assert resp.status_code == 200 and resp.json()["has_ledger"] is False
        finally:
            store.close()


class TestJourney:
    def test_single_journey_carries_the_trace(self, traced, sample):
        journey = query_journey(traced, _settled_with_runner_up(sample))
        trace = QueryTraceResponse.model_validate(journey["trace"])
        assert trace.final_engine == "dynamodb"
        assert journey["trace"]["moves"][0]["pass"] == "settle"

    def test_journey_list_is_unchanged(self, traced):
        # The report embeds query_journeys: it must not grow a trace (#339 part A).
        assert all("trace" not in j for j in query_journeys(traced))


class _DirStore:
    """The ArtifactStore surface rebuild_graph reads, over a dict of artifacts."""

    def __init__(self, files: dict[str, dict]):
        self.files = files

    def read_json(self, path: str) -> dict:
        return self.files[path]

    def exists(self, path: str) -> bool:
        return path in self.files

    def list_prefix(self, prefix: str):
        return [k for k in self.files if k.startswith(prefix)]


class TestRebuild:
    def _files(self, sample: dict, extra: dict | None = None) -> dict:
        base = "wordpress/job"
        files = {
            f"{base}/collector/output.json": sample["collector"],
            f"{base}/assignment/v1/assignment.json": sample["v1"],
            f"{base}/assignment/v2/assignment.json": sample["v2"],
        }
        files.update(extra or {})
        return files

    def test_rebuild_ingests_every_version_of_the_lineage(self, graph_store, sample):
        rebuild_graph("wordpress", "job", _DirStore(self._files(sample)), graph_store)
        trace = query_trace(graph_store, _settled_with_runner_up(sample))
        assert [s.version for s in [trace.first_assignment, *trace.moves]] == [1, 2]
        assert trace.effective_version == 2

    def test_lineage_skips_a_version_the_effective_one_does_not_descend_from(self, sample):
        stale = {**sample["v2"], "version": 2}
        fresh = {**sample["v1"], "version": 3, "previous_version": None}
        versions = {1: sample["v1"], 2: stale, 3: fresh}
        assert [a["version"] for a in assignment_lineage(versions.get, 3)] == [3]
        assert [a["version"] for a in assignment_lineage(versions.get, 2)] == [1, 2]

    def test_lineage_stops_on_a_cycle_or_a_missing_version(self, sample):
        loop = {1: {**sample["v1"], "previous_version": 2}, 2: sample["v2"]}
        assert [a["version"] for a in assignment_lineage(loop.get, 2)] == [1, 2]
        assert assignment_lineage({}.get, 4) == []


class TestTraceEndpoint:
    @pytest.fixture
    def client(self, traced):
        app = FastAPI()
        app.include_router(graph_routes.router)
        app.dependency_overrides[graph_routes.get_graph_for_job] = lambda: (traced, "wordpress")
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_trace_endpoint(self, client, sample):
        qid = _settled_with_runner_up(sample)
        resp = client.get(f"/api/v1/assessments/job-1/graph/queries/{qid}/trace")
        assert resp.status_code == 200
        body = resp.json()
        assert QueryTraceResponse.model_validate(body).final_engine == "dynamodb"
        assert body["first_assignment"]["pass"] == "score"
        assert body["runner_up"]["gap"] == 15.0

    def test_trace_endpoint_unknown_query(self, client):
        resp = client.get("/api/v1/assessments/job-1/graph/queries/nope/trace")
        assert resp.status_code == 404
