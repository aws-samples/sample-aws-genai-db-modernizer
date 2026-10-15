"""The reality check's records are derived from the move ledger (#616).

``consolidations``, the sanity sweep's ``removed`` verdicts and the model flags'
``moved`` verdicts used to be built beside the ledger, each pass keeping its
own count of what it moved, with an agreement test checking they matched. Now
each is a function of the Moves: the tests below hand-build Moves, with no
pass run and no "after" assignment list, and check that the record follows
from them alone. What a Move does not carry stays computed where it is, and
these tests feed it in explicitly: the pass's own reason text, an engine's
analysed cost (the saving, counted once per removed engine), its
first-assignment ``workload_justified`` verdict, and the queries a partial
consolidation retained (a retained query has no Move, it did not change).
"""

from __future__ import annotations

from src.agents.referee.move_ledger import apply, ledger_for
from src.agents.referee.reality_check_records import (
    consolidations_from_moves,
    engine_changes,
    flag_moves_by_query,
    sweep_outcome,
)
from src.contracts.move_ledger import MoveActor, MovePass, ReasonCode


def _qa(qid: str, engine: str, **extra) -> dict:
    return {"query_id": qid, "assigned_engine": engine, **extra}


def _ledger(before: list[dict], *moves: tuple[str, str, MovePass]):
    ledger = ledger_for(before)
    for qid, to, pass_ in moves:
        apply(
            ledger,
            qid,
            to,
            pass_=pass_,
            reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
            score_before=50.0,
            score_after=80.0,
        )
    return ledger


class TestEngineChanges:
    def test_groups_engine_changes_by_pair_in_seq_order(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb"), _qa("c", "opensearch")]
        ledger = _ledger(
            before,
            ("b", "dynamodb", MovePass.SETTLE),
            ("c", "aurora_mysql", MovePass.SETTLE),
            ("a", "dynamodb", MovePass.SETTLE),
        )

        assert engine_changes(ledger.moves) == {
            ("documentdb", "dynamodb"): ["b", "a"],
            ("opensearch", "aurora_mysql"): ["c"],
        }

    def test_a_tie_only_move_or_a_first_assignment_changes_no_engine(self) -> None:
        ledger = ledger_for([])
        apply(ledger, "a", "dynamodb", pass_=MovePass.SCORE, reason_code=ReasonCode.HIGHEST_SCORE)
        apply(ledger, "a", "dynamodb", pass_=MovePass.PLACE, reason_code=ReasonCode.TIE_CLEARED)

        assert engine_changes(ledger.moves) == {}


class TestConsolidationsFromMoves:
    def test_records_follow_the_moves_with_no_after_assignment(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb"), _qa("c", "aurora_mysql")]
        ledger = _ledger(
            before, ("a", "dynamodb", MovePass.SETTLE), ("b", "aurora_mysql", MovePass.SETTLE)
        )

        records = consolidations_from_moves(
            before, ledger.moves, [], engine_costs={"documentdb": 300.0}
        )

        assert [(c["from_engine"], c["to_engine"], c["query_count"]) for c in records] == [
            ("documentdb", "dynamodb", 1),
            ("documentdb", "aurora_mysql", 1),
        ]
        # The saving is the engine's analysed cost (not on a Move), counted once.
        assert [c["saved_cost_estimate"] for c in records] == [300.0, 0.0]
        assert all(c["action"] == "full" and c["removal_saving_claimed"] for c in records)

    def test_a_stale_pass_count_is_replaced_by_the_ledgers(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb")]
        ledger = _ledger(before, ("a", "dynamodb", MovePass.SETTLE))
        stale = {
            "from_engine": "documentdb",
            "to_engine": "dynamodb",
            "query_count": 2,
            "reason": "DocumentDB provides no unique capabilities",
            "saved_cost_estimate": 0,
            "action": "full",
            "queries_retained": [],
            "retention_reason": None,
        }

        (record,) = consolidations_from_moves(before, ledger.moves, [stale])

        assert (record["query_count"], record["action"]) == (1, "partial")
        assert record["reason"] == (
            "Partial consolidation: 1 DocumentDB query moved to DynamoDB; "
            "DocumentDB stays for 1 query"
        )

    def test_a_two_hop_move_is_one_record_from_where_it_started(self) -> None:
        before = [_qa("a", "aurora_mysql"), _qa("b", "aurora_mysql")]
        ledger = _ledger(
            before,
            ("a", "opensearch", MovePass.SETTLE),
            ("a", "dynamodb", MovePass.FLOOR),
        )

        (record,) = consolidations_from_moves(before, ledger.moves, [])

        assert (record["from_engine"], record["to_engine"], record["query_count"]) == (
            "aurora_mysql",
            "dynamodb",
            1,
        )

    def test_a_move_there_and_back_leaves_no_record(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "dynamodb")]
        ledger = _ledger(
            before,
            ("a", "dynamodb", MovePass.SETTLE),
            ("a", "documentdb", MovePass.MODEL_FLAG),
        )

        assert consolidations_from_moves(before, ledger.moves, []) == []

    def test_an_out_of_scope_leftover_still_makes_the_removal_full(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb", in_scope=False)]
        ledger = _ledger(before, ("a", "dynamodb", MovePass.SETTLE))

        (record,) = consolidations_from_moves(before, ledger.moves, [])

        assert record["action"] == "full"


class TestSweepOutcome:
    def test_records_and_the_removed_verdict_come_from_the_sweep_moves(self) -> None:
        before = [_qa("d0", "documentdb"), _qa("d1", "documentdb"), _qa("d2", "documentdb")]
        ledger = _ledger(
            before,
            ("d0", "dynamodb", MovePass.SWEEP),
            ("d1", "aurora_mysql", MovePass.SWEEP),
            ("d2", "aurora_mysql", MovePass.SWEEP),
        )

        records, decision = sweep_outcome("documentdb", 3, ledger.moves)

        assert [(c["to_engine"], c["query_count"], c["action"]) for c in records] == [
            ("aurora_mysql", 2, "partial"),
            ("dynamodb", 1, "partial"),
        ]
        assert records[0]["reason"] == (
            "Sanity sweep: DocumentDB had only 3 queries, each with a serviceable "
            "committed engine; 2 scored best on Aurora MySQL"
        )
        assert decision == {
            "engine": "documentdb",
            "decision": "removed",
            "reason": (
                "3 queries, each with a serviceable committed engine: "
                "2 to Aurora MySQL; 1 to DynamoDB"
            ),
            "blocking_query_ids": [],
            "blocking_rule": None,
        }

    def test_only_moves_off_the_swept_engine_count(self) -> None:
        before = [_qa("d0", "documentdb"), _qa("o0", "opensearch")]
        ledger = _ledger(
            before,
            ("o0", "aurora_mysql", MovePass.SWEEP),
            ("d0", "dynamodb", MovePass.SWEEP),
        )

        records, decision = sweep_outcome("documentdb", 1, ledger.moves)

        assert [(c["to_engine"], c["query_count"], c["action"]) for c in records] == [
            ("dynamodb", 1, "full")
        ]
        assert decision["reason"].endswith(": 1 to DynamoDB")


class TestFlagMovesByQuery:
    def test_maps_each_flag_move_to_the_engine_it_left_and_where_it_went(self) -> None:
        before = [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "documentdb")]
        ledger = _ledger(before, ("c", "dynamodb", MovePass.SETTLE))
        for qid in ("a", "c"):
            apply(
                ledger,
                qid,
                "aurora_mysql",
                pass_=MovePass.MODEL_FLAG,
                reason_code=ReasonCode.FLAG_CAPABILITY_GAP,
                actor=MoveActor.MODEL_FLAG,
            )

        assert flag_moves_by_query(ledger.moves) == {
            "a": ("dynamodb", "aurora_mysql"),
            "c": ("dynamodb", "aurora_mysql"),
        }


class TestPlacementTieCount:
    """Phase 2's "N of them tied on score" counts the Moves an exact tie decided."""

    def test_a_score_decided_group_member_with_an_old_tie_is_not_counted(self) -> None:
        """Review of #836: a co-dependency group whose trigger member has no tie
        moves by score; a member that already carried a tie has it cleared on
        its Move (#845), and no tie decided that move. Only ``x1``, placed by an
        unresolved tie (``evidence.tie_rule`` a placeholder rule), counts."""
        from src.agents.referee.reality_check_place import _place_settled_queries

        qas: list[dict] = [
            {"query_id": "m1", "assigned_engine": "documentdb"},
            {
                "query_id": "m2",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
            {"query_id": "x1", "assigned_engine": "documentdb"},
        ]
        group = {"key": "abc123abc123", "scores": {"documentdb": 60, "dynamodb": 80}}
        trace: dict[str, dict] = {
            "m1": {"engines": {"documentdb": 60, "dynamodb": 80}, "group": group},
            "m2": {"engines": {"documentdb": 60, "dynamodb": 80}, "group": group},
            "x1": {"engines": {"documentdb": 50, "dynamodb": 80, "aurora_mysql": 80}},
        }
        ledger = ledger_for(qas)

        _, records = _place_settled_queries(
            qas,
            {"dynamodb", "aurora_mysql"},
            set(),
            {qa["query_id"]: qa["assigned_engine"] for qa in qas},
            {},
            {qa["query_id"]: {"query_id": qa["query_id"], "query_type": "SELECT"} for qa in qas},
            None,
            trace,
            None,
            {},
            {"m2"},
            {},
            {"m2": ["documentdb", "dynamodb"]},
            ledger=ledger,
        )

        moves = {m.query_id: m for m in ledger.moves}
        assert moves["m2"].tied_engines == []  # the group's score decided, #845
        assert moves["m2"].evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        assert moves["m2"].evidence.tie_rule is None
        tied_notes = [r["reason"] for r in records if "tied on score" in r["reason"]]
        assert len(tied_notes) == 1, [r["reason"] for r in records]
        assert "(1 of them tied on score" in tied_notes[0]


class TestFlagMovesIgnoreOtherPasses:
    def test_a_non_flag_move_after_a_flag_is_not_a_flag_move(self) -> None:
        before = [_qa("a", "dynamodb"), _qa("b", "documentdb")]
        ledger = ledger_for(before)
        apply(
            ledger,
            "a",
            "aurora_mysql",
            pass_=MovePass.MODEL_FLAG,
            reason_code=ReasonCode.FLAG_CAPABILITY_GAP,
            actor=MoveActor.MODEL_FLAG,
        )
        apply(ledger, "b", "dynamodb", pass_=MovePass.SWEEP, reason_code=ReasonCode.SWEEP_ORPHAN)

        assert flag_moves_by_query(ledger.moves) == {"a": ("dynamodb", "aurora_mysql")}


class TestOneWayToFitRecords:
    def test_records_are_fitted_only_from_moves(self) -> None:
        """#846: the before/after builder ``reconcile_consolidations`` is gone, so
        consolidation records have one entry point, the ledger path."""
        from src.agents.referee import reality_check, reality_check_records

        assert not hasattr(reality_check_records, "reconcile_consolidations")
        assert "reconcile_consolidations" not in reality_check.__all__
