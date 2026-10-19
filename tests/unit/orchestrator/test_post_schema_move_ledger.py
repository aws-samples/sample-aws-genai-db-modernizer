"""The post-schema router writes its versions through the move ledger (#616, #770).

#489 Track L PR4: a reroute version (``source = post_schema_routing``) holds
exactly the Moves that produced it, one ``post_schema_flag`` Move per moved
query (rule ``flag.capability_gap``, actor ``model_flag``, the shared scorer's
scores on both engines, the runner-up and the candidate ladder). A rollback
version (status ``reroute_rolled_back``) holds the reverse Moves: one per Move of
the reroute, undone in reverse order, rule ``rollback.redesign_failed``, actor
``rule`` (#770 option (a)). So v1 plus each later version's own Moves replays
to that version, and both writers refuse a version that does not replay.

The schema designer is patched (``run_schema_design_auto``); no test calls a model.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.referee import move_ledger
from src.contracts.move_ledger import Move
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.unit.tools.schema import test_pe_routing_flags as pe_routing

CASE = pe_routing.TestTheOrchestratorRoutesAFlagByScore()
DB, JOB = CASE.DB, CASE.JOB
PREFIX = f"{DB}/{JOB}"


def _version(store: LocalArtifactStore, n: int) -> dict:
    return store.read_json(f"{PREFIX}/assignment/v{n}/assignment.json")


def _moves(version: dict) -> list[Move]:
    return [Move.model_validate(m) for m in version["moves"]]


def _seed(store: LocalArtifactStore, *, tied: dict[str, list[str]] | None = None) -> None:
    """The #629 seed, with v1 carrying its own first-assignment Moves (and ties)."""
    CASE._seed(store)
    key = f"{PREFIX}/assignment/v1/assignment.json"
    v1 = store.read_json(key)
    ledger = move_ledger.MoveLedger()
    qas = []
    for qa in v1["query_assignments"]:
        ties = (tied or {}).get(qa["query_id"], [])
        move_ledger.apply(
            ledger,
            qa["query_id"],
            qa["assigned_engine"],
            pass_=move_ledger.MovePass.SCORE,
            reason_code=move_ledger.ReasonCode.HIGHEST_SCORE,
            tied_engines=ties,
        )
        qas.append({**qa, "tied_engines": ties})
    store.write_json(
        key,
        {
            **v1,
            "query_assignments": qas,
            "moves": [m.model_dump(mode="json") for m in ledger.moves],
        },
    )


def _replayed(store: LocalArtifactStore, upto: int) -> dict:
    """v1's Moves, then each later version's own Moves, folded in order."""
    state = move_ledger.replay(_moves(_version(store, 1)))
    for n in range(2, upto + 1):
        state = move_ledger.replay(_moves(_version(store, n)), start=state)
    return state


def _rollback_run(store: LocalArtifactStore) -> None:
    orch = LocalOrchestrator(store, llm_mode="bedrock")
    with (
        patch(
            "src.agents.schema_design.handler.run_schema_design_auto",
            side_effect=RuntimeError("designer down"),
        ),
        pytest.raises(RuntimeError, match="designer down"),
    ):
        orch._run_post_schema_routing(JOB, DB)


class TestTheRerouteVersionHoldsItsOwnMoves:
    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    @pytest.mark.usefixtures("schema_contract_off")
    def test_one_post_schema_flag_move_per_moved_query(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        CASE._run(store, [])

        (move,) = _moves(_version(store, 2))
        assert move.seq == 1
        assert (move.query_id, move.from_engine, move.to_engine) == (
            "q-1",
            "dynamodb",
            "aurora_mysql",
        )
        assert move.pass_ == move_ledger.MovePass.POST_SCHEMA_FLAG
        assert move.reason_code == move_ledger.ReasonCode.FLAG_CAPABILITY_GAP
        assert move.actor.value == "model_flag"
        # The same scores the router compared and post_schema_reroutes records.
        assert (move.score_before, move.score_after) == (90, 70)
        assert move.runner_up is not None
        assert (move.runner_up.engine, move.runner_up.score) == ("documentdb", 55)
        assert move.tied_engines == []
        ladder = {c.engine: c for c in move.evidence.candidates}
        assert sorted(ladder) == ["aurora_mysql", "documentdb", "dynamodb"]
        # The flagged engine is gated by the flag, never scored as a candidate.
        assert ladder["dynamodb"].score is None and ladder["dynamodb"].gate

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_version_replays_from_v1_plus_its_moves(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        CASE._run(store, [])

        v2 = _version(store, 2)
        assert _replayed(store, 2) == move_ledger.state_of(v2["query_assignments"])
        # v1's own Moves are not copied into v2.
        assert len(_version(store, 1)["moves"]) == 4

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_two_way_tie_settles_on_the_other_tied_engine(self, tmp_path) -> None:
        """#811: the other tied engine (DocumentDB, 55) comes before Aurora
        MySQL (70); one engine left is no tie, so the Move records the one it left."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store, tied={"q-1": ["documentdb", "dynamodb"]})
        CASE._run(store, [])

        (move,) = _moves(_version(store, 2))
        assert move.to_engine == "documentdb"
        assert move.tied_engines == []
        assert move.evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        assert _replayed(store, 2) == move_ledger.state_of(_version(store, 2)["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_co_dependency_group_moves_as_one_unit_with_its_key(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        key = f"{PREFIX}/assignment/v1/assignment.json"
        store.write_json(key, {**store.read_json(key), "co_dependency_groups": [["q-1", "q-4"]]})
        CASE._run(store, [])

        moves = _moves(_version(store, 2))
        assert [(m.seq, m.query_id, m.to_engine) for m in moves] == [
            (1, "q-1", "aurora_mysql"),
            (2, "q-4", "aurora_mysql"),
        ]
        assert {m.group_key for m in moves} == {move_ledger.group_key(["q-1", "q-4"])}
        assert all(m.evidence.group_scores for m in moves)
        assert _replayed(store, 2) == move_ledger.state_of(_version(store, 2)["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_every_cascade_version_replays_in_turn(self, tmp_path) -> None:
        """The two-pass cascade of #713 review round 3: v2 and v3 each hold
        only their own pass's Moves, and v1 -> v2 -> v3 replays to v3."""
        cascade = pe_routing.TestKeptEnginesAreReReadEveryPass()
        cascade.test_an_engine_an_earlier_pass_emptied_is_never_a_destination(tmp_path)
        store = LocalArtifactStore(base_dir=str(tmp_path))
        assert [m.query_id for m in _moves(_version(store, 2))] == ["q-3"]
        assert [m.query_id for m in _moves(_version(store, 3))] == ["q-2"]
        v1, v2, v3 = (_version(store, n) for n in (1, 2, 3))
        state = move_ledger.state_of(v1["query_assignments"])
        state = move_ledger.replay(_moves(v2), start=state)
        assert state == move_ledger.state_of(v2["query_assignments"])
        state = move_ledger.replay(_moves(v3), start=state)
        assert state == move_ledger.state_of(v3["query_assignments"])

    def test_a_reroute_that_does_not_replay_is_refused(self, tmp_path) -> None:
        """The write-time replay check: an engine changed without a Move (here a
        derived-view refresh that edits one) fails before v2 is written."""
        from src.agents.referee import assignment_overrides

        real_refresh = assignment_overrides.refresh_consolidated_assignment

        def tampering_refresh(raw, *args, **kwargs):
            out = real_refresh(raw, *args, **kwargs)
            out["query_assignments"][1] = {
                **out["query_assignments"][1],
                "assigned_engine": "documentdb",
            }
            return out

        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        with (
            patch.object(
                assignment_overrides, "refresh_consolidated_assignment", tampering_refresh
            ),
            pytest.raises(RuntimeError, match="post-schema reroute"),
        ):
            CASE._run(store, [])
        assert not store.exists(f"{PREFIX}/assignment/v2/assignment.json")


class TestTheRefreshSeesTheEngineTheRerouteEmptied:
    """Review round 2 (critical): recording the Moves must not edit the input
    records the refresh compares against, so an engine the reroute empties is
    still a dead engine there, exactly as on main."""

    def test_dead_engines_and_pruned_warnings_match_main(self, tmp_path) -> None:
        from src.agents.referee import assignment_overrides

        store = LocalArtifactStore(base_dir=str(tmp_path))
        CASE._seed(store)
        # The removed-engine scenario of test_pe_routing_flags.py: q-1 is
        # DynamoDB's only query, so the reroute empties DynamoDB.
        store.write_json(
            f"{PREFIX}/assignment/v1/assignment.json",
            {
                "version": 1,
                "query_assignments": [
                    {
                        "query_id": "q-1",
                        "assigned_engine": "dynamodb",
                        "warnings": ["table split with dynamodb"],
                    },
                    {
                        "query_id": "q-2",
                        "assigned_engine": "documentdb",
                        "warnings": ["also read by dynamodb"],
                    },
                ],
            },
        )
        real_refresh = assignment_overrides.refresh_consolidated_assignment
        seen: list[tuple[list[str], list[str]]] = []

        def spy(raw, collector, analysis_outputs, *, dead_engines=None):
            seen.append((sorted(analysis_outputs), sorted(dead_engines or [])))
            return real_refresh(raw, collector, analysis_outputs, dead_engines=dead_engines)

        with patch.object(assignment_overrides, "refresh_consolidated_assignment", spy):
            CASE._run(store, [])

        assert seen[0] == (["documentdb", "dynamodb"], ["dynamodb"])
        v2 = _version(store, 2)
        assert {qa["query_id"]: qa["warnings"] for qa in v2["query_assignments"]} == {
            "q-1": [],
            "q-2": [],
        }
        # The input version on disk is untouched.
        v1 = _version(store, 1)
        assert v1["query_assignments"][0]["assigned_engine"] == "dynamodb"


class TestTheRollbackVersionRecordsTheMovesBack:
    """#770 option (a): reverse Moves, so one replay rule covers every version."""

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    @pytest.mark.usefixtures("schema_contract_off")
    def test_one_reverse_move_per_query_whose_engine_changes_back(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        _rollback_run(store)

        v3 = _version(store, 3)
        assert v3["status"] == "reroute_rolled_back"
        (move,) = _moves(v3)
        assert move.seq == 1
        assert (move.query_id, move.from_engine, move.to_engine) == (
            "q-1",
            "aurora_mysql",
            "dynamodb",
        )
        assert move.pass_ == move_ledger.MovePass.POST_SCHEMA_FLAG
        assert move.reason_code == move_ledger.ReasonCode.ROLLBACK_REDESIGN_FAILED
        assert move.reason_code.value == "rollback.redesign_failed"
        assert move.actor.value == "rule"
        # It leaves Aurora MySQL (the undone move's score_after, 70) and lands
        # back on DynamoDB, which the flag still gates: no score, the gate in
        # evidence (the undone move's candidate ladder).
        assert (move.score_before, move.score_after) == (70, None)
        ladder = {c.engine: c for c in move.evidence.candidates}
        assert ladder["dynamodb"].score is None
        assert ladder["dynamodb"].gate == "model_flag"
        assert move.evidence.previous_tied_engines == []

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_rollback_replays_to_the_version_it_restates(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        _rollback_run(store)

        v1, v3 = _version(store, 1), _version(store, 3)
        assert v3["query_assignments"] == v1["query_assignments"]
        assert _replayed(store, 3) == move_ledger.state_of(v3["query_assignments"])
        assert _replayed(store, 3) == move_ledger.state_of(v1["query_assignments"])
        # The abandoned reroute keeps its own Move; the rollback holds only its own.
        assert [m.query_id for m in _moves(_version(store, 2))] == ["q-1"]

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_rollback_restores_the_tie_the_reroute_cleared(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store, tied={"q-1": ["documentdb", "dynamodb"]})
        _rollback_run(store)

        (move,) = _moves(_version(store, 3))
        assert move.tied_engines == ["documentdb", "dynamodb"]
        assert move.evidence.previous_tied_engines == []
        assert _replayed(store, 3) == move_ledger.state_of(_version(store, 1)["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_group_rollback_carries_the_group_key(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        key = f"{PREFIX}/assignment/v1/assignment.json"
        store.write_json(key, {**store.read_json(key), "co_dependency_groups": [["q-1", "q-4"]]})
        _rollback_run(store)

        moves = _moves(_version(store, 3))
        # Undo order: the reroute's Moves reversed.
        assert [(m.seq, m.query_id, m.to_engine) for m in moves] == [
            (1, "q-4", "dynamodb"),
            (2, "q-1", "dynamodb"),
        ]
        assert {m.group_key for m in moves} == {move_ledger.group_key(["q-1", "q-4"])}
        assert _replayed(store, 3) == move_ledger.state_of(_version(store, 1)["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_drifted_untouched_query_is_refused(self, tmp_path) -> None:
        """The rollback check is not circular: the reverse Moves come from the
        abandoned version's own Moves, so a query the reroute never moved that
        differs between the two versions fails instead of getting a Move."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        CASE._run(store, [])
        rerouted = _version(store, 2)
        restated = _version(store, 1)
        drifted = {
            **restated,
            "query_assignments": [
                {**qa, "assigned_engine": "documentdb"} if qa["query_id"] == "q-2" else qa
                for qa in restated["query_assignments"]
            ],
        }
        assert LocalOrchestrator._rollback_moves(rerouted, restated)
        with pytest.raises(RuntimeError, match="post-schema rollback"):
            LocalOrchestrator._rollback_moves(rerouted, drifted)

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_query_the_reroute_left_alone_gets_no_move(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)
        _rollback_run(store)

        assert {m.query_id for m in _moves(_version(store, 3))} == {"q-1"}


def _seed_tie_case(
    store: LocalArtifactStore, scores: dict[str, float], *, tied: list[str] | None = None
) -> None:
    """The #629 seed with q-1 scored ``scores`` and a source with no
    source-compatible engine among them, so an exact tie stays unresolved."""
    _seed(store, tied={"q-1": tied} if tied else None)
    for engine, score in scores.items():
        store.write_json(
            f"{PREFIX}/analysis-{engine}/analysis.json",
            {"table_recommendations": [{"table_id": "t_q1", "confidence_score": score}]},
        )
    key = f"{PREFIX}/collector/output.json"
    collector = store.read_json(key)
    store.write_json(key, {**collector, "metadata": {"source_database": {"engine": "mongodb"}}})


class TestTheRerouteKeepsTheTie:
    """#811: the post-schema reroute keeps a query's tie the way the reality
    check's model-flag path does (ADR-031 Decisions 11 Amendment 2 and 12)."""

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_tied_engine_comes_before_a_higher_scoring_one(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed_tie_case(
            store,
            {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 80},
            tied=["documentdb", "dynamodb"],
        )
        CASE._run(store, [])

        (move,) = _moves(_version(store, 2))
        assert (move.from_engine, move.to_engine) == ("dynamodb", "documentdb")
        assert move.tied_engines == []
        assert move.evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        assert _replayed(store, 2) == move_ledger.state_of(_version(store, 2)["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_tie_minus_the_flagged_engine_is_kept(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed_tie_case(
            store,
            {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 70},
            tied=["aurora_mysql", "documentdb", "dynamodb"],
        )
        CASE._run(store, [])

        v2 = _version(store, 2)
        (move,) = _moves(v2)
        assert move.to_engine in {"aurora_mysql", "documentdb"}
        assert move.tied_engines == ["aurora_mysql", "documentdb"]
        assert move.evidence.previous_tied_engines == ["aurora_mysql", "documentdb", "dynamodb"]
        q1 = next(qa for qa in v2["query_assignments"] if qa["query_id"] == "q-1")
        assert q1["tied_engines"] == ["aurora_mysql", "documentdb"]
        router = store.read_json(f"{PREFIX}/post-schema-router/router_output.json")
        assert router["routings"][0]["tied_engines"] == ["aurora_mysql", "documentdb"]
        assert _replayed(store, 2) == move_ledger.state_of(v2["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_an_untied_query_keeps_the_fresh_tie_the_scorer_leaves(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed_tie_case(store, {"dynamodb": 90, "documentdb": 70, "aurora_mysql": 70})
        CASE._run(store, [])

        (move,) = _moves(_version(store, 2))
        assert move.tied_engines == ["aurora_mysql", "documentdb"]
        assert move.evidence.previous_tied_engines == []
        assert _replayed(store, 2) == move_ledger.state_of(_version(store, 2)["query_assignments"])


class TestTheRollbackAfterATieKeepingReroute:
    """#807 round-2 review note on #811: once a reroute can keep a tie, the
    rollback must tell "the tie was empty" from "the tie is unchanged"."""

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_query_with_no_tie_before_gets_no_tie_back(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed_tie_case(store, {"dynamodb": 90, "documentdb": 70, "aurora_mysql": 70})
        _rollback_run(store)

        assert _moves(_version(store, 2))[0].tied_engines == ["aurora_mysql", "documentdb"]
        (move,) = _moves(_version(store, 3))
        assert move.to_engine == "dynamodb"
        assert move.tied_engines == []
        assert move.evidence.previous_tied_engines == ["aurora_mysql", "documentdb"]
        v1, v3 = _version(store, 1), _version(store, 3)
        assert v3["query_assignments"] == v1["query_assignments"]
        assert _replayed(store, 3) == move_ledger.state_of(v1["query_assignments"])

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_kept_tie_rolls_back_to_the_tie_before(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed_tie_case(
            store,
            {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 70},
            tied=["aurora_mysql", "documentdb", "dynamodb"],
        )
        _rollback_run(store)

        (move,) = _moves(_version(store, 3))
        assert move.to_engine == "dynamodb"
        assert move.tied_engines == ["aurora_mysql", "documentdb", "dynamodb"]
        assert move.evidence.previous_tied_engines == ["aurora_mysql", "documentdb"]
        assert _replayed(store, 3) == move_ledger.state_of(_version(store, 1)["query_assignments"])

    def test_an_unchanged_tie_is_restored_as_it_was(self) -> None:
        """A Move that leaves the tie as it was records no earlier tie
        (``apply_to_assignment``); the rollback still restores that tie, not
        an empty one."""
        restated = {
            "query_assignments": [
                {"query_id": "q-1", "assigned_engine": "aurora_mysql", "tied_engines": ["a", "b"]}
            ]
        }
        qa = dict(restated["query_assignments"][0])
        ledger = move_ledger.ledger_for([qa])
        move_ledger.apply_to_assignment(
            ledger,
            qa,
            "documentdb",
            pass_=move_ledger.MovePass.POST_SCHEMA_FLAG,
            reason_code=move_ledger.ReasonCode.FLAG_CAPABILITY_GAP,
            tied_engines=["a", "b"],
        )
        assert ledger.moves[0].evidence.previous_tied_engines == []
        rerouted = {
            "query_assignments": [qa],
            "moves": [m.model_dump(mode="json") for m in ledger.moves],
        }

        (back,) = [
            Move.model_validate(m) for m in LocalOrchestrator._rollback_moves(rerouted, restated)
        ]

        assert (back.to_engine, back.tied_engines) == ("aurora_mysql", ["a", "b"])
        assert back.evidence.previous_tied_engines == []

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_move_whose_tie_record_disagrees_with_the_input_is_refused(self, tmp_path) -> None:
        """The reverse Moves never trust a tie the input does not have: a Move
        recording an earlier tie the restated version lacks fails loudly."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store, tied={"q-1": ["documentdb", "dynamodb"]})
        CASE._run(store, [])
        rerouted, restated = _version(store, 2), _version(store, 1)
        untied = {
            **restated,
            "query_assignments": [
                {**qa, "tied_engines": []} for qa in restated["query_assignments"]
            ],
        }

        with pytest.raises(RuntimeError, match="records the tie"):
            LocalOrchestrator._rollback_moves(rerouted, untied)
