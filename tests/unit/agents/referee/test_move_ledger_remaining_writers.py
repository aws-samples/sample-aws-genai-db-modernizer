"""The remaining writers of ``assigned_engine`` record Moves (#616, #489 Track L PR3).

The cache owner normalisation (``cache_overlay.normalize_cache_owners``: the
reality check handler's, the customer gate's and synthesis's), the customer
gate (``assignment_overrides.apply_assignment_overrides``: picks, a cache
request's owner, co-dependency propagation) and the review surfaces that feed
it (``assignment_review``) set every engine through ``move_ledger``. Every
version they write holds exactly its own Moves, and replaying the stored input
version plus those Moves rebuilds it; a write that bypassed the ledger fails
loudly instead of reaching a version.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

import pytest

from src.agents.referee import assignment_overrides
from src.agents.referee.assignment_overrides import (
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.assignment_review import diff_review_items
from src.agents.referee.cache_overlay import normalize_cache_owners
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply,
    check_replay,
    group_key,
    ledger_for,
    replay,
    unexplained,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.synthesis_data import load_synthesis_data
from src.contracts.assignment_models import Assignment, QueryAssignment
from src.contracts.move_ledger import Move, MoveActor, MovePass, ReasonCode
from src.storage.local_store import LocalArtifactStore
from tests.unit import test_assignment_overrides as overrides_tests
from tests.unit.test_assignment_overrides import DB, JOB, _MemStore, _qa, _seed


def _state(qas: Iterable[Any]) -> dict[str, tuple[str, list[str]]]:
    out: dict[str, tuple[str, list[str]]] = {}
    for qa in qas:
        if isinstance(qa, QueryAssignment):
            out[qa.query_id] = (qa.assigned_engine, list(qa.tied_engines))
        else:
            out[qa["query_id"]] = (qa["assigned_engine"], list(qa.get("tied_engines") or []))
    return out


def _moves(raw: Iterable[Any]) -> list[Move]:
    return [m if isinstance(m, Move) else Move.model_validate(m) for m in raw]


def _q(qid: str, text: str = "SELECT * FROM items WHERE id = ?", cps: float = 0.01) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": ["items"],
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "filter_tables": [],
    }


def _analysis(conf: int) -> dict:
    return {
        "table_recommendations": [{"table_id": "items", "confidence_score": conf}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


def _legacy_assignment() -> dict:
    return {
        "query_assignments": [
            {"query_id": "q", "assigned_engine": "elasticache", "assignment_reason": "legacy"},
            {"query_id": "r", "assigned_engine": "dynamodb", "assignment_reason": "scored"},
        ]
    }


# ---------------------------------------------------------------------------
# normalize_cache_owners
# ---------------------------------------------------------------------------


class TestNormalizeCacheOwners:
    def test_each_legacy_owner_is_one_move_with_the_shared_scores(self) -> None:
        assignment = _legacy_assignment()
        stored = _state(assignment["query_assignments"])
        queries = [_q("q"), _q("r")]
        analysis = {"dynamodb": _analysis(90), "documentdb": _analysis(10)}
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment, queries, analysis, triage=None, collector=None, ledger=ledger
        )

        assert len(ledger.moves) == 1
        move = ledger.moves[0]
        assert move.pass_ is MovePass.CACHE_OWNER_NORMALIZE
        assert move.reason_code is ReasonCode.CACHE_LEGACY_OWNER
        assert move.actor is MoveActor.RULE
        assert (move.query_id, move.from_engine, move.to_engine) == ("q", "elasticache", "dynamodb")
        ctx = ScoringContext.from_pipeline(analysis, None, None)
        assert move.score_after == score_query_for_engine(queries[0], "dynamodb", ctx).score
        assert move.runner_up is not None and move.runner_up.engine == "documentdb"
        assert {c.engine for c in move.evidence.candidates} == {"documentdb", "dynamodb"}
        # With a ledger passed, the version's own ``moves`` are the caller's to write.
        assert "moves" not in assignment
        assert replay(ledger.moves, start=stored) == _state(assignment["query_assignments"])

    def test_a_tied_owner_records_the_tie_and_its_rule(self) -> None:
        assignment = _legacy_assignment()
        analysis = {"dynamodb": _analysis(70), "documentdb": _analysis(70)}
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment, [_q("q"), _q("r")], analysis, triage=None, collector=None, ledger=ledger
        )

        (move,) = ledger.moves
        assert move.tied_engines == ["documentdb", "dynamodb"]
        assert move.evidence.tie_rule in {
            ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD,
            ReasonCode.TIE_PLACEHOLDER_HASH,
        }
        qa = assignment["query_assignments"][0]
        assert (qa["assigned_engine"], qa["tied_engines"]) == (move.to_engine, move.tied_engines)

    def test_a_gated_candidate_is_in_the_ladder_with_its_gate(self) -> None:
        assignment = _legacy_assignment()
        aggregation = _q("q", text="SELECT category, COUNT(*) FROM items GROUP BY category")
        analysis = {"dynamodb": _analysis(90), "documentdb": _analysis(10)}
        triage = {"query_capabilities": {"q": ["aggregation"]}}
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment,
            [aggregation, _q("r")],
            analysis,
            triage=triage,
            collector=None,
            ledger=ledger,
        )

        (move,) = ledger.moves
        assert move.to_engine == "documentdb"
        ladder = {c.engine: c for c in move.evidence.candidates}
        assert ladder["dynamodb"].score is None and ladder["dynamodb"].gate
        assert move.runner_up is None  # the only other candidate is gated

    def test_without_a_ledger_the_moves_extend_the_versions_own(self) -> None:
        """An in-memory normalisation (synthesis, the edit surface) appends its
        Moves to the version's own, after them in ``seq``, so the in-memory
        version still replays from its input."""
        assignment = _legacy_assignment()
        assignment["moves"] = [
            {
                "seq": 1,
                "query_id": "r",
                "pass": "score",
                "from_engine": None,
                "to_engine": "dynamodb",
                "reason_code": "highest_score",
            },
            {
                "seq": 2,
                "query_id": "q",
                "pass": "score",
                "from_engine": None,
                "to_engine": "elasticache",
                "reason_code": "highest_score",
            },
        ]
        analysis = {"dynamodb": _analysis(90), "documentdb": _analysis(10)}

        normalize_cache_owners(
            assignment, [_q("q"), _q("r")], analysis, triage=None, collector=None
        )

        moves = _moves(assignment["moves"])
        assert [m.seq for m in moves] == [1, 2, 3]
        assert moves[-1].pass_ is MovePass.CACHE_OWNER_NORMALIZE
        assert replay(moves) == _state(assignment["query_assignments"])

    def test_nothing_legacy_records_nothing(self) -> None:
        assignment = {"query_assignments": [{"query_id": "r", "assigned_engine": "dynamodb"}]}
        ledger = ledger_for(assignment["query_assignments"])
        assert (
            normalize_cache_owners(
                assignment,
                [_q("r")],
                {"dynamodb": _analysis(90)},
                triage=None,
                collector=None,
                ledger=ledger,
            )
            == []
        )
        assert ledger.moves == []


# ---------------------------------------------------------------------------
# Synthesis-time normalisation (#786, point 2)
# ---------------------------------------------------------------------------


class TestSynthesisTimeNormalization:
    @staticmethod
    def _seed(store) -> dict:
        prefix = "shop/job-legacy"
        store.write_json(
            f"{prefix}/referee-triage/triage.json",
            {
                "selected_agents": [
                    {"agent_type": e} for e in ("dynamodb", "documentdb", "elasticache")
                ],
                "signals": [],
                "query_capabilities": {"q": ["aggregation"]},
            },
        )
        store.write_json(
            f"{prefix}/collector/output.json",
            {
                "database_schema": {"tables": [{"table_id": "items"}]},
                "queries": {
                    "query_patterns": [
                        _q("q", text="SELECT category, COUNT(*) FROM items GROUP BY category"),
                        _q("r"),
                    ]
                },
            },
        )
        conf = {"dynamodb": 90, "documentdb": 10, "elasticache": 90}
        for engine, c in conf.items():
            store.write_json(f"{prefix}/analysis-{engine}/analysis.json", _analysis(c))
        stored = {"job_id": "job-legacy", "version": 1, **_legacy_assignment(), "moves": []}
        store.write_json(f"{prefix}/assignment/v1/assignment.json", stored)
        return stored

    def test_the_legacy_caller_applies_the_capability_gate(self, tmp_path) -> None:
        """Synthesis used to pass engine names only, so every owner candidate
        tied and the gate was skipped: DynamoDB, which cannot aggregate, could
        win the placeholder. It now scores with the analysis it loads."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)

        data = load_synthesis_data(store, "job-legacy", "shop", assignment_version=1)

        assert data.assignment is not None
        q = next(qa for qa in data.assignment["query_assignments"] if qa["query_id"] == "q")
        assert (q["assigned_engine"], q["tied_engines"]) == ("documentdb", [])

    def test_the_in_memory_moves_explain_the_normalization(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        stored = self._seed(store)

        data = load_synthesis_data(store, "job-legacy", "shop", assignment_version=1)

        assert data.assignment is not None
        moves = _moves(data.assignment["moves"])
        assert [(m.pass_, m.query_id, m.from_engine) for m in moves] == [
            (MovePass.CACHE_OWNER_NORMALIZE, "q", "elasticache")
        ]
        start = _state(stored["query_assignments"])
        assert replay(moves, start=start) == _state(data.assignment["query_assignments"])
        # In memory only: the stored version is unchanged.
        on_disk = store.read_json("shop/job-legacy/assignment/v1/assignment.json")
        assert on_disk["moves"] == [] and on_disk["query_assignments"][0]["assigned_engine"] == (
            "elasticache"
        )


# ---------------------------------------------------------------------------
# The customer gate
# ---------------------------------------------------------------------------


def _v1_state(store) -> dict[str, tuple[str, list[str]]]:
    return _state(store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")["query_assignments"])


def _ctx(store, engines: list[str]) -> ScoringContext:
    """The shared scorer's context the customer gate builds for the seeded job."""
    return ScoringContext.from_pipeline(
        {e: store.read_json(f"{DB}/{JOB}/analysis-{e}/analysis.json") for e in engines},
        {},
        store.read_json(f"{DB}/{JOB}/collector/output.json"),
    )


def _assert_replays(store, result) -> list[Move]:
    """The written version holds its own Moves, and they replay from v1."""
    written = store.read_json(result.written_path)
    moves = _moves(written["moves"])
    assert moves == list(result.assignment.moves)
    assert [m.seq for m in moves] == list(range(1, len(moves) + 1))
    assert replay(moves, start=_v1_state(store)) == _state(written["query_assignments"])
    return moves


class TestCustomerGate:
    def test_a_pick_is_one_customer_move_with_the_shared_scores(self) -> None:
        store = _MemStore()
        _seed(store, [_qa("q1", "dynamodb", ["t1"])], ["dynamodb", "aurora_postgresql"])

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )

        (move,) = _assert_replays(store, result)
        assert move.pass_ is MovePass.CUSTOMER_PICK
        assert move.reason_code is ReasonCode.CUSTOMER_PICK
        assert move.actor is MoveActor.CUSTOMER
        assert (move.from_engine, move.to_engine) == ("dynamodb", "aurora_postgresql")
        collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
        ctx = ScoringContext.from_pipeline(
            {
                e: store.read_json(f"{DB}/{JOB}/analysis-{e}/analysis.json")
                for e in ("dynamodb", "aurora_postgresql")
            },
            {},
            collector,
        )
        query = collector["queries"]["query_patterns"][0]
        assert move.score_before == score_query_for_engine(query, "dynamodb", ctx).score
        assert move.score_after == score_query_for_engine(query, "aurora_postgresql", ctx).score
        # The runner-up is the best other engine that may own the query.
        assert move.runner_up is not None
        assert move.runner_up.engine == "dynamodb"
        assert move.runner_up.score == move.score_before

    def test_a_pick_of_the_current_engine_records_no_move(self) -> None:
        store = _MemStore()
        _seed(store, [_qa("q1", "dynamodb", ["t1"])], ["dynamodb", "aurora_postgresql"])

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
        )

        assert _assert_replays(store, result) == []

    def test_a_pick_that_settles_a_tie_is_a_move(self) -> None:
        store = _MemStore()
        tied = _qa("q1", "dynamodb", ["t1"])
        tied.tied_engines = ["aurora_postgresql", "dynamodb"]
        _seed(store, [tied], ["dynamodb", "aurora_postgresql"])

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
        )

        (move,) = _assert_replays(store, result)
        assert (move.from_engine, move.to_engine, move.tied_engines) == ("dynamodb", "dynamodb", [])
        assert move.evidence.previous_tied_engines == ["aurora_postgresql", "dynamodb"]
        assert move.actor is MoveActor.CUSTOMER

    def test_group_propagation_moves_carry_the_group(self) -> None:
        store = _MemStore()
        seed = overrides_tests.TestCoDependencyPropagation._seed_join_group
        seed(store, engines=["dynamodb", "aurora_postgresql"])

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )

        moves = _assert_replays(store, result)
        by_query = {m.query_id: m for m in moves}
        assert by_query["q1"].pass_ is MovePass.CUSTOMER_PICK
        assert by_query["q1"].group_key is None
        key = group_key(["q1", "q2", "q3"])
        ctx = _ctx(store, ["dynamodb", "aurora_postgresql"])
        queries = {
            q["query_id"]: q
            for q in store.read_json(f"{DB}/{JOB}/collector/output.json")["queries"][
                "query_patterns"
            ]
        }
        for qid in ("q2", "q3"):
            move = by_query[qid]
            assert move.pass_ is MovePass.CUSTOMER_GROUP_PROPAGATION
            assert move.reason_code is ReasonCode.GROUP_CUSTOMER_PROPAGATION
            assert move.actor is MoveActor.CUSTOMER
            assert move.group_key == key
            assert (move.from_engine, move.to_engine) == ("dynamodb", "aurora_postgresql")
            # The shared scorer's scores on both engines, as for a pick.
            assert move.score_before is not None and move.score_after is not None
            assert move.score_before == score_query_for_engine(queries[qid], "dynamodb", ctx).score
            assert (
                move.score_after
                == score_query_for_engine(queries[qid], "aurora_postgresql", ctx).score
            )
            assert move.runner_up is not None and move.runner_up.engine == "dynamodb"
        # The customer's own pick comes first, then the group follows it.
        assert [m.query_id for m in moves] == ["q1", "q2", "q3"]

    def test_a_legacy_cache_owner_is_normalized_in_the_customer_version(self) -> None:
        store = _MemStore()
        _seed(
            store,
            [_qa("q1", "dynamodb", ["t1"]), _qa("q2", "elasticache", ["t2"])],
            ["dynamodb", "aurora_postgresql", "elasticache"],
        )

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )

        moves = _assert_replays(store, result)
        assert [(m.pass_, m.query_id) for m in moves] == [
            (MovePass.CACHE_OWNER_NORMALIZE, "q2"),
            (MovePass.CUSTOMER_PICK, "q1"),
        ]
        assert moves[0].from_engine == "elasticache"
        assert moves[0].actor is MoveActor.RULE

    def test_a_stray_write_fails_loudly_and_writes_nothing(self, monkeypatch) -> None:
        store = _MemStore()
        _seed(store, [_qa("q1", "dynamodb", ["t1"])], ["dynamodb", "aurora_postgresql"])

        def stray(qa_map, overrides, query_by_id, collector_output, **kwargs):
            qa_map["q1"].__dict__["assigned_engine"] = "aurora_postgresql"  # no Move
            return [], []

        monkeypatch.setattr(assignment_overrides, "_propagate_codependent_overrides", stray)

        with pytest.raises(RuntimeError, match="without a Move"):
            apply_assignment_overrides(store, DB, JOB, [QueryOverrideInput("q1", in_scope=False)])
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestAssignmentReview:
    def test_a_review_edit_reaches_the_ledger_as_a_customer_pick(self) -> None:
        store = _MemStore()
        _seed(store, [_qa("q1", "dynamodb", ["t1"])], ["dynamodb", "aurora_postgresql"])
        current = Assignment.model_validate(
            deepcopy(store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json"))
        )

        overrides = diff_review_items(
            current, [{"query_id": "q1", "new_engine": "aurora_postgresql", "in_scope": "yes"}]
        )
        result = apply_assignment_overrides(store, DB, JOB, overrides)

        (move,) = _assert_replays(store, result)
        assert (move.pass_, move.actor, move.to_engine) == (
            MovePass.CUSTOMER_PICK,
            MoveActor.CUSTOMER,
            "aurora_postgresql",
        )


# ---------------------------------------------------------------------------
# tied_engines is ledger-owned too, and v1 passes the same replay check
# ---------------------------------------------------------------------------


class TestTiesAndTheFirstAssignment:
    def test_a_tie_only_change_is_unexplained_without_a_move(self) -> None:
        start = {"q": ("dynamodb", ["documentdb", "dynamodb"])}
        end: dict[str, tuple[str, list[str]]] = {"q": ("dynamodb", [])}
        assert unexplained([], start, end) == ["q"]
        with pytest.raises(RuntimeError, match="without a Move"):
            check_replay([], start, end, "test")

    def test_the_ledger_tracks_the_tie_its_moves_leave(self) -> None:
        ledger = MoveLedger()
        apply(
            ledger,
            "q",
            "dynamodb",
            pass_=MovePass.TIE,
            reason_code=ReasonCode.TIE_PLACEHOLDER_HASH,
            tied_engines=["dynamodb", "documentdb"],
        )
        assert ledger.tied_of("q") == ["documentdb", "dynamodb"]
        apply(
            ledger,
            "q",
            "dynamodb",
            pass_=MovePass.UTILITY_PIN,
            reason_code=ReasonCode.OVERRIDE_UTILITY,
        )
        assert ledger.tied_of("q") == []

    def test_a_customer_gate_tie_cleared_without_a_move_fails_loudly(self, monkeypatch) -> None:
        store = _MemStore()
        tied = _qa("q1", "dynamodb", ["t1"])
        tied.tied_engines = ["aurora_postgresql", "dynamodb"]
        _seed(store, [tied], ["dynamodb", "aurora_postgresql"])

        def stray(qa_map, overrides, query_by_id, collector_output, **kwargs):
            qa_map["q1"].__dict__["tied_engines"] = []  # the engine stays, no Move
            return [], []

        monkeypatch.setattr(assignment_overrides, "_propagate_codependent_overrides", stray)

        with pytest.raises(RuntimeError, match="customer gate: 1 queries"):
            apply_assignment_overrides(store, DB, JOB, [QueryOverrideInput("q1", in_scope=False)])
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    @staticmethod
    def _resolve() -> Assignment:
        from tests.unit.agents.referee import test_move_ledger as base

        return AssignmentResolver().resolve(
            base._triage(["dynamodb", "documentdb"]),
            {"dynamodb": base._analysis({"db.t": 70}), "documentdb": base._analysis({"db.t": 70})},
            base._collector([base._query("q1", ["db.t"])], ["db.t"]),
        )

    def test_v1_ties_come_from_the_ledger_and_replay(self) -> None:
        assignment = self._resolve()
        (qa,) = assignment.query_assignments
        assert qa.tied_engines == ["documentdb", "dynamodb"]  # an unresolved tie
        assert replay(assignment.moves) == _state(assignment.query_assignments)

    def test_v1_that_does_not_replay_fails_loudly(self, monkeypatch) -> None:
        """A tie on v1 that no Move recorded (here: the ledger's tie read is
        tampered with) raises before the resolver returns v1."""
        monkeypatch.setattr(MoveLedger, "tied_of", lambda self, qid: ["aurora_mysql"])
        with pytest.raises(RuntimeError, match="first assignment: 1 queries"):
            self._resolve()
