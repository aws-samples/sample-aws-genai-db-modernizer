"""The reality check records every engine change in the move ledger (#616, Track L PR2).

Phase 1 settle and Phase 2 placement, the justification floors, the model
flags, the sanity sweep and the customer-override restore all set a query's
engine through ``move_ledger.apply_to_assignment``. Each test checks the Move a
pass records (pass, rule, before/after scores, runner-up) and that replaying
the Moves on top of the input rebuilds the result exactly, ``tied_engines``
included. The sweep tests also cover #755: a sweep move's reason carries the
before and after scores.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from src.agents.referee.consolidation_validator import sanity_sweep
from src.agents.referee.model_flags import apply_consolidation_flags
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    ledger_for,
    replay,
)
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check import run_reality_check
from src.agents.referee.reality_check_handler import _restore_customer_overrides
from src.contracts.model_flag import ModelFlag
from src.contracts.move_ledger import Move, MoveActor, MovePass, ReasonCode


def _state(qas: Any) -> dict[str, tuple[str, list[str]]]:
    return {
        qa["query_id"]: (qa["assigned_engine"], list(qa.get("tied_engines") or [])) for qa in qas
    }


def _assert_replays(before: list[dict], after: list[dict], ledger: MoveLedger) -> None:
    assert replay(ledger.moves, start=_state(before)) == _state(after)
    assert [m.seq for m in ledger.moves] == list(range(1, len(ledger.moves) + 1))


# ---------------------------------------------------------------------------
# The ledger helpers
# ---------------------------------------------------------------------------


class TestApplyToAssignment:
    def test_sets_the_dict_from_the_move(self) -> None:
        qa = {"query_id": "q1", "assigned_engine": "documentdb", "tied_engines": ["a", "b"]}
        ledger = ledger_for([qa])

        move = apply_to_assignment(
            ledger,
            qa,
            "dynamodb",
            pass_=MovePass.SWEEP,
            reason_code=ReasonCode.SWEEP_ORPHAN,
            score_before=40.0,
            score_after=90.0,
            tied_engines=[],
        )

        assert (move.from_engine, move.to_engine) == ("documentdb", "dynamodb")
        assert qa["assigned_engine"] == "dynamodb"
        assert qa["tied_engines"] == []
        assert ledger.engine_of("q1") == "dynamodb"

    def test_no_tied_engines_keeps_the_current_tie_and_adds_no_key(self) -> None:
        qa = {"query_id": "q1", "assigned_engine": "documentdb"}
        ledger = ledger_for([qa])

        move = apply_to_assignment(
            ledger, qa, "dynamodb", pass_=MovePass.PLACE, reason_code=ReasonCode.HIGHEST_SCORE
        )

        assert move.tied_engines == []
        assert "tied_engines" not in qa

    def test_an_engine_set_without_a_move_is_logged_and_replay_stays_consistent(
        self, caplog
    ) -> None:
        qa = {"query_id": "q1", "assigned_engine": "documentdb"}
        ledger = ledger_for([qa])
        qa["assigned_engine"] = "aurora_mysql"  # a write that bypassed the ledger

        move = apply_to_assignment(
            ledger, qa, "dynamodb", pass_=MovePass.PLACE, reason_code=ReasonCode.HIGHEST_SCORE
        )

        assert "an engine set without a Move" in caplog.text
        assert move.from_engine == "documentdb"  # the ledger's engine, never the stray write
        assert qa["assigned_engine"] == ledger.engine_of("q1") == "dynamodb"

    def test_replay_applies_a_later_version_on_top_of_its_input(self) -> None:
        v1 = [
            {"query_id": "q1", "assigned_engine": "documentdb", "tied_engines": []},
            {"query_id": "q2", "assigned_engine": "dynamodb", "tied_engines": ["a", "b"]},
        ]
        v2 = [dict(qa) for qa in v1]
        ledger = ledger_for(v2)
        apply_to_assignment(
            ledger,
            v2[0],
            "aurora_mysql",
            pass_=MovePass.SETTLE,
            reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
            tied_engines=[],
        )

        assert replay(ledger.moves, start=_state(v1)) == _state(v2)
        # Without the start state only the moved query is known.
        assert replay(ledger.moves) == {"q1": ("aurora_mysql", [])}


# ---------------------------------------------------------------------------
# Phase 1 settle and Phase 2 placement
# ---------------------------------------------------------------------------


def _triage() -> dict:
    return {
        "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
        "signals": [],
    }


def _collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }


def _analysis(dynamodb: int, documentdb: int) -> dict:
    return {
        "dynamodb": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": dynamodb}]
        },
        "documentdb": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": documentdb}]
        },
    }


class TestSettleAndPlace:
    def test_a_query_on_a_removed_engine_is_a_settle_move_with_both_scores(self) -> None:
        assignment = {
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {"query_id": "q2", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {"query_id": "q3", "assigned_engine": "documentdb", "assignment_reason": "t"},
            ]
        }

        result = run_reality_check(
            assignment,
            _triage(),
            _analysis(dynamodb=80, documentdb=70),
            _collector(["q1", "q2", "q3"]),
        )

        ledger = result["move_ledger"]
        (move,) = ledger.moves
        assert move.query_id == "q3"
        assert move.pass_ is MovePass.SETTLE
        assert move.reason_code is ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE
        assert (move.from_engine, move.to_engine) == ("documentdb", "dynamodb")
        assert (move.score_before, move.score_after) == (70.0, 80.0)
        assert move.runner_up is None  # documentdb was removed, so nothing else competed
        assert {c.engine for c in move.evidence.candidates} == {"documentdb", "dynamodb"}
        # The reason the customer reads states the same two scores.
        reason = next(
            qa["assignment_reason"]
            for qa in result["revised_assignments"]
            if qa["query_id"] == "q3"
        )
        assert "DocumentDB scores 70.0, DynamoDB scores 80.0" in reason
        _assert_replays(assignment["query_assignments"], result["revised_assignments"], ledger)

    def test_a_tie_that_clears_while_the_engine_stays_is_a_move_too(self) -> None:
        """Replay must rebuild ``tied_engines``, so a cleared tie records a Move
        whose ``from_engine`` equals its ``to_engine``."""
        assignment: dict[str, list[dict[str, Any]]] = {
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {"query_id": "q2", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {
                    "query_id": "q_tied",
                    "assigned_engine": "dynamodb",
                    "assignment_reason": "t",
                    "tied_engines": ["documentdb", "dynamodb"],
                },
                {"query_id": "q3", "assigned_engine": "documentdb", "assignment_reason": "t"},
            ]
        }

        result = run_reality_check(
            assignment,
            _triage(),
            _analysis(dynamodb=70, documentdb=70),
            _collector(["q1", "q2", "q_tied", "q3"]),
        )

        ledger = result["move_ledger"]
        tied_moves = [m for m in ledger.moves if m.query_id == "q_tied"]
        assert len(tied_moves) == 1
        (move,) = tied_moves
        assert move.from_engine == move.to_engine == "dynamodb"
        assert move.tied_engines == []
        assert move.pass_ is MovePass.PLACE
        _assert_replays(assignment["query_assignments"], result["revised_assignments"], ledger)

    def test_an_unchanged_query_records_no_move(self) -> None:
        assignment = {
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "t"},
            ]
        }

        result = run_reality_check(
            assignment, _triage(), _analysis(dynamodb=80, documentdb=40), _collector(["q1"])
        )

        assert result["move_ledger"].moves == []


# ---------------------------------------------------------------------------
# Justification floors
# ---------------------------------------------------------------------------


class TestJustificationFloor:
    def _run(self, *, with_aurora: bool = False) -> dict:
        assignment = {
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search → opensearch",
                    "signal_override": "text_search",
                }
            ]
            + (
                [
                    {
                        "query_id": f"aq{i}",
                        "assigned_engine": "aurora_mysql",
                        "assignment_reason": "t",
                    }
                    for i in range(10)
                ]
                if with_aurora
                else []
            )
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "opensearch"}]
            + ([{"agent_type": "aurora_mysql"}] if with_aurora else []),
            "signals": [
                {"signal": "text_search", "targets": ["opensearch"], "query_ids": ["search"]}
            ],
            "query_capabilities": {},
        }
        patterns = [
            {"query_id": f"dq{i}", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
            for i in range(20)
        ] + [
            {
                "query_id": "search",
                "tables_accessed": ["db.users"],
                "query_type": "SELECT",
                "query_text": "SELECT * FROM wp_users WHERE meta_value LIKE ?",
                "calls_per_second": 0.07,
            }
        ]
        if with_aurora:
            patterns += [
                {"query_id": f"aq{i}", "tables_accessed": ["db.admin"], "query_type": "SELECT"}
                for i in range(10)
            ]
        collector: dict[str, Any] = {"queries": {"query_patterns": patterns}}
        if with_aurora:
            collector["metadata"] = {"source_database": {"engine": "mysql"}}
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.posts", "confidence_score": 80},
                    {"table_id": "db.users", "confidence_score": 50},
                    {"table_id": "db.admin", "confidence_score": 10},
                ]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]
            },
        }
        if with_aurora:
            # Aurora serves the search query best; DynamoDB keeps its own
            # queries (Aurora fits them far worse), so both stay as destinations.
            analysis["aurora_mysql"] = {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 70},
                    {"table_id": "db.posts", "confidence_score": 10},
                    {"table_id": "db.admin", "confidence_score": 90},
                ]
            }
        return run_reality_check(assignment, triage, analysis, collector)

    def _run_with_two_destinations(self) -> dict:
        return self._run(with_aurora=True)

    def test_an_opensearch_floor_move_records_the_floor_rule_and_the_reason_scores(self) -> None:
        result = self._run()

        (move,) = [m for m in result["move_ledger"].moves if m.query_id == "search"]
        assert move.pass_ is MovePass.FLOOR
        assert move.reason_code is ReasonCode.FLOOR_OPENSEARCH
        assert (move.from_engine, move.to_engine) == ("opensearch", "dynamodb")
        reason = next(
            qa["assignment_reason"]
            for qa in result["revised_assignments"]
            if qa["query_id"] == "search"
        )
        # The Move's scores are the ones the reason states (#698).
        assert f"OpenSearch scores {move.score_before}" in reason
        assert f"DynamoDB scores {move.score_after}" in reason


# ---------------------------------------------------------------------------
# Sanity sweep (#755: before and after scores)
# ---------------------------------------------------------------------------


def _query_map(*query_ids: str) -> dict[str, dict]:
    return {qid: {"query_id": qid, "tables_accessed": [f"t_{qid}"]} for qid in query_ids}


def _context(scores: dict[str, dict[str, float]]) -> ScoringContext:
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


def _orphan_documentdb() -> list[dict]:
    """Three queries on a tiny DocumentDB, one on Aurora MySQL, six on DynamoDB."""
    return (
        [{"query_id": f"d{i}", "assigned_engine": "documentdb"} for i in range(3)]
        + [{"query_id": "a1", "assigned_engine": "aurora_mysql"}]
        + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
    )


class TestSanitySweep:
    def test_a_sweep_move_reports_both_scores_in_its_reason_and_its_move(self) -> None:
        assignments = _orphan_documentdb()
        before = [dict(qa) for qa in assignments]
        query_map = _query_map(*(qa["query_id"] for qa in assignments))
        ctx = _context(
            {
                "d0": {"documentdb": 60, "aurora_mysql": 10, "dynamodb": 90},
                "d1": {"documentdb": 60, "aurora_mysql": 95, "dynamodb": 5},
                "d2": {"documentdb": 60, "aurora_mysql": 95, "dynamodb": 5},
            }
        )
        ledger = ledger_for(assignments)

        updated, _, decisions = sanity_sweep(
            assignments, [], ctx, query_map, source_engine="mysql", ledger=ledger
        )

        by_id = {qa["query_id"]: qa for qa in updated}
        assert by_id["d0"]["assignment_reason"] == (
            "sanity sweep: redirected from DocumentDB to DynamoDB (orphan engine with 3 "
            "queries; best-scoring serviceable committed engine; DocumentDB scores 60.0, "
            "DynamoDB scores 90.0)"
        )
        assert (
            "DocumentDB scores 60.0, Aurora MySQL scores 95.0" in by_id["d1"]["assignment_reason"]
        )
        moves = {m.query_id: m for m in ledger.moves}
        assert set(moves) == {"d0", "d1", "d2"}
        d0 = moves["d0"]
        assert d0.pass_ is MovePass.SWEEP
        assert d0.reason_code is ReasonCode.SWEEP_ORPHAN
        assert (d0.from_engine, d0.to_engine) == ("documentdb", "dynamodb")
        assert (d0.score_before, d0.score_after) == (60.0, 90.0)
        assert d0.runner_up is not None
        assert (d0.runner_up.engine, d0.runner_up.score) == ("aurora_mysql", 10.0)
        _assert_replays(before, updated, ledger)
        # sweep_decisions agree with the ledger: the removed engine's Moves
        # are exactly the sweep's, and each destination's count matches.
        (decision,) = decisions
        assert decision["decision"] == "removed"
        assert Counter(m.to_engine for m in ledger.moves if m.from_engine == "documentdb") == {
            "aurora_mysql": 2,
            "dynamodb": 1,
        }

    def test_a_group_sweep_move_reports_the_group_scores(self) -> None:
        assignments = [
            {"query_id": "j1", "assigned_engine": "documentdb"},
            {"query_id": "j2", "assigned_engine": "documentdb"},
            {"query_id": "a0", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        before = [dict(qa) for qa in assignments]
        query_map = _query_map(*(qa["query_id"] for qa in assignments))
        ctx = _context(
            {
                "j1": {"documentdb": 50, "dynamodb": 90, "aurora_mysql": 30},
                "j2": {"documentdb": 40, "dynamodb": 70, "aurora_mysql": 30},
            }
        )
        ledger = ledger_for(assignments)

        updated, _, _ = sanity_sweep(
            assignments, [], ctx, query_map, co_dependency_groups=[["j1", "j2"]], ledger=ledger
        )

        for qid in ("j1", "j2"):
            reason = next(qa["assignment_reason"] for qa in updated if qa["query_id"] == qid)
            assert reason.endswith(
                "co-dependency group moves together; DocumentDB scores 45.0, "
                "DynamoDB scores 80.0)"
            ), reason
        assert {m.group_key for m in ledger.moves} != {None}
        assert len({m.group_key for m in ledger.moves}) == 1
        assert all(
            m.evidence.group_scores == {"aurora_mysql": 30.0, "dynamodb": 80.0}
            for m in ledger.moves
        )
        _assert_replays(before, updated, ledger)

    def test_a_gated_orphan_engine_reads_n_a(self) -> None:
        """The scorer gates the orphan engine for these queries: no score, honestly ``n/a``."""
        assignments = _orphan_documentdb()
        query_map = _query_map(*(qa["query_id"] for qa in assignments))
        ctx = ScoringContext(
            analysis_outputs=_context(
                {f"d{i}": {"documentdb": 60, "aurora_mysql": 95, "dynamodb": 5} for i in range(3)}
            ).analysis_outputs,
            query_capabilities={f"d{i}": ["complex_joins"] for i in range(3)},
        )
        ledger = ledger_for(assignments)

        updated, _, _ = sanity_sweep(assignments, [], ctx, query_map, ledger=ledger)

        reason = next(qa["assignment_reason"] for qa in updated if qa["query_id"] == "d0")
        assert "DocumentDB scores n/a, Aurora MySQL scores 95.0" in reason
        assert all(m.score_before is None for m in ledger.moves)

    def test_a_kept_engine_records_no_move(self) -> None:
        assignments = [
            {"query_id": "g1", "assigned_engine": "documentdb"},
            {"query_id": "g2", "assigned_engine": "documentdb"},
        ] + [{"query_id": f"z{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map(*(qa["query_id"] for qa in assignments))
        ctx = ScoringContext(
            analysis_outputs={"dynamodb": {"table_recommendations": []}},
            query_capabilities={"g1": ["multi_doc_acid"]},
        )
        ledger = ledger_for(assignments)

        _, _, decisions = sanity_sweep(assignments, [], ctx, query_map, ledger=ledger)

        assert [d["decision"] for d in decisions] == ["kept"]
        assert ledger.moves == []


# ---------------------------------------------------------------------------
# Model flags
# ---------------------------------------------------------------------------


def _flag(qids: list[str], engine: str) -> ModelFlag:
    return ModelFlag.model_validate(
        {
            "query_ids": qids,
            "flagged_engine": engine,
            "reason_category": "client_side_join",
            "explanation": "needs a server-side join",
        }
    )


class TestModelFlags:
    def test_a_flag_move_matches_its_flag_decision(self) -> None:
        original = [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "dynamodb"},
            {"query_id": "q3", "assigned_engine": "aurora_mysql"},
        ]
        revised = [
            {"query_id": "q1", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"},
            {"query_id": "q2", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"},
            {"query_id": "q3", "assigned_engine": "aurora_mysql", "assignment_reason": "kept"},
        ]
        before = [dict(qa) for qa in revised]
        consolidation = {
            "from_engine": "dynamodb",
            "to_engine": "aurora_mysql",
            "query_count": 2,
            "reason": "no unique value",
            "saved_cost_estimate": 0,
            "action": "full",
            "queries_retained": [],
            "retention_reason": None,
        }
        ctx = _context({"q1": {"aurora_mysql": 70, "dynamodb": 65}})
        query_map = {
            qid: {
                "query_id": qid,
                "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
                "query_type": "SELECT",
                "tables_accessed": [f"t_{qid}"],
            }
            for qid in ("q1", "q2", "q3")
        }
        ledger = ledger_for(revised)

        updated, _, decisions, _ = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            [consolidation],
            original,
            ctx,
            query_map,
            {"aurora_mysql", "dynamodb"},
            ledger=ledger,
        )

        (decision,) = decisions
        (move,) = ledger.moves
        assert move.pass_ is MovePass.MODEL_FLAG
        assert move.reason_code is ReasonCode.FLAG_CAPABILITY_GAP
        assert move.actor is MoveActor.MODEL_FLAG
        assert (move.from_engine, move.to_engine) == (
            decision["flagged_engine"],
            decision["destination_engine"],
        )
        assert (move.score_before, move.score_after) == (
            decision["score_before"],
            decision["score_after"],
        )
        gates = {c.engine: c.gate for c in move.evidence.candidates}
        assert gates["aurora_mysql"] == "model_flag"
        _assert_replays(before, updated, ledger)

    def test_a_kept_or_ignored_flag_records_no_move(self) -> None:
        revised = [{"query_id": "q1", "assigned_engine": "aurora_mysql"}]
        ledger = ledger_for(revised)

        _, _, decisions, _ = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            [],
            [{"query_id": "q1", "assigned_engine": "aurora_mysql"}],
            _context({"q1": {"aurora_mysql": 70}}),
            _query_map("q1"),
            {"aurora_mysql"},
            ledger=ledger,
        )

        assert [d["outcome"] for d in decisions] == ["ignored"]
        assert ledger.moves == []


# ---------------------------------------------------------------------------
# Customer-override restore
# ---------------------------------------------------------------------------


class TestCustomerRestore:
    def test_a_restored_customer_pick_is_a_customer_move(self) -> None:
        chosen = {
            "query_id": "q1",
            "assigned_engine": "dynamodb",
            "customer_override": True,
            "tied_engines": [],
        }
        moved_off = {**chosen, "assigned_engine": "aurora_mysql"}
        result: dict[str, Any] = {
            "assignment": {"query_assignments": [chosen]},
            "revised_assignments": [moved_off],
            "collector_output": {"queries": {"query_patterns": [_query_map("q1")["q1"]]}},
            "analysis_outputs": _context(
                {"q1": {"dynamodb": 55, "aurora_mysql": 75}}
            ).analysis_outputs,
            "triage": {},
        }
        ledger = ledger_for(result["revised_assignments"])
        result["move_ledger"] = ledger

        _restore_customer_overrides(result)

        (move,) = ledger.moves
        assert move.pass_ is MovePass.CUSTOMER_PICK
        assert move.reason_code is ReasonCode.CUSTOMER_PICK
        assert move.actor is MoveActor.CUSTOMER
        assert (move.from_engine, move.to_engine) == ("aurora_mysql", "dynamodb")
        assert (move.score_before, move.score_after) == (75.0, 55.0)
        assert result["revised_assignments"][0]["assigned_engine"] == "dynamodb"
        assert replay(ledger.moves, start=_state([moved_off])) == _state([chosen])


# ---------------------------------------------------------------------------
# Review round 2: drift fails loudly, the legacy cache-owner exception, and
# the mutations the first round's tests let through
# ---------------------------------------------------------------------------


def _written_version(store: Any, version: int | None) -> dict:
    assert version is not None
    key = next(k for k in store._written if f"assignment/v{version}/" in k)
    written: dict = store._written[key]
    return written


class TestTheHandlerRefusesAnUnexplainedRevision:
    """``write_reality_check_result`` raises when the revision does not replay
    from its input plus its Moves, so an engine set without a Move fails loudly."""

    def test_a_stray_direct_write_fails_loudly(self) -> None:
        from src.agents.referee.reality_check_handler import (
            run_reality_check_deterministic,
            write_reality_check_result,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        det = run_reality_check_deterministic("job-1", "mydb", store, 1)
        stray = next(qa for qa in det["revised_assignments"] if qa["query_id"] == "DDB-AP-1")
        stray["assigned_engine"] = "documentdb"  # bypasses move_ledger.apply

        with pytest.raises(RuntimeError, match="changed engine or tie without a Move"):
            write_reality_check_result(store, "job-1", "mydb", det, 1)

        assert not any("assignment/v2" in k for k in store._written)

    def test_a_tie_set_without_a_move_fails_loudly(self) -> None:
        """A tie-only change (the engine stays) with no Move is drift too: the
        replay check compares ``tied_engines`` as well as the engine."""
        from src.agents.referee.reality_check_handler import (
            run_reality_check_deterministic,
            write_reality_check_result,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        det = run_reality_check_deterministic("job-1", "mydb", store, 1)
        stray = next(qa for qa in det["revised_assignments"] if qa["query_id"] == "DDB-AP-1")
        stray["tied_engines"] = sorted({stray["assigned_engine"], "documentdb"})

        with pytest.raises(RuntimeError, match="changed engine or tie without a Move"):
            write_reality_check_result(store, "job-1", "mydb", det, 1)

        assert not any("assignment/v2" in k for k in store._written)

    def test_a_clean_run_writes_a_version_that_replays(self) -> None:
        from src.agents.referee.reality_check_handler import (
            run_reality_check_deterministic,
            write_reality_check_result,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _ASSIGNMENT, _mock_store

        store = _mock_store()
        det = run_reality_check_deterministic("job-1", "mydb", store, 1)

        version = write_reality_check_result(store, "job-1", "mydb", det, 1)

        written = _written_version(store, version)
        moves = [Move.model_validate(m) for m in written["moves"]]
        assert moves
        start = _state(_ASSIGNMENT["query_assignments"])
        assert replay(moves, start=start) == _state(written["query_assignments"])

    def test_a_pre_296_cache_owner_is_a_move_and_replays_from_the_stored_input(self) -> None:
        """An assignment written before #296 has an ElastiCache owner, which
        ``normalize_cache_owners`` moves to a system-of-record engine. That is a
        ``cache_owner_normalize`` Move (Track L PR3 closed PR2's named
        exception), recorded first, so the revision replays from the stored
        input, not only from the normalized one."""
        from copy import deepcopy

        from src.agents.referee.reality_check_handler import (
            run_reality_check_deterministic,
            write_reality_check_result,
        )
        from tests.unit.agents.referee import test_reality_check_llm_seam as seam

        legacy: Any = deepcopy(seam._ASSIGNMENT)
        legacy["query_assignments"][1]["assigned_engine"] = "elasticache"
        stored = deepcopy(legacy)
        original = seam._ASSIGNMENT
        seam._ASSIGNMENT = legacy
        try:
            store = seam._mock_store()
        finally:
            seam._ASSIGNMENT = original
        det = run_reality_check_deterministic("job-1", "mydb", store, 1)
        normalized = {
            qa["query_id"]: qa["assigned_engine"] for qa in det["assignment"]["query_assignments"]
        }
        assert normalized["DDB-AP-2"] != "elasticache"  # the normalization ran

        version = write_reality_check_result(store, "job-1", "mydb", det, 1)

        written = _written_version(store, version)
        moves = [Move.model_validate(m) for m in written["moves"]]
        first = moves[0]
        assert (first.seq, first.query_id, first.pass_, first.reason_code) == (
            1,
            "DDB-AP-2",
            MovePass.CACHE_OWNER_NORMALIZE,
            ReasonCode.CACHE_LEGACY_OWNER,
        )
        assert first.from_engine == "elasticache"
        assert first.to_engine == normalized["DDB-AP-2"]
        expected = _state(written["query_assignments"])
        assert replay(moves, start=_state(stored["query_assignments"])) == expected


def _trace(scores: dict[str, dict[str, float]], groups: dict[str, dict] | None = None) -> dict:
    trace: dict[str, dict] = {qid: {"engines": dict(s)} for qid, s in scores.items()}
    for qid, group in (groups or {}).items():
        trace[qid]["group"] = group
    return trace


def _place(qas: list[dict], committed: set[str], trace: dict) -> tuple[list[dict], MoveLedger]:
    from src.agents.referee.reality_check_place import _place_settled_queries

    ledger = ledger_for(qas)
    query_map = {qa["query_id"]: {"query_id": qa["query_id"], "query_type": "SELECT"} for qa in qas}
    revised, _ = _place_settled_queries(
        qas,
        committed,
        set(),
        {qa["query_id"]: qa["assigned_engine"] for qa in qas},
        {},
        query_map,
        None,
        trace,
        None,
        {},
        {qa["query_id"] for qa in qas if qa.get("tied_engines")},
        {},
        {qa["query_id"]: list(qa["tied_engines"]) for qa in qas if qa.get("tied_engines")},
        ledger=ledger,
    )
    return revised, ledger


class TestPhase2RecordsTheRunnerUpAndGroup:
    def test_a_settle_move_names_the_next_best_kept_engine(self) -> None:
        qas = [{"query_id": "q1", "assigned_engine": "documentdb"}]
        trace = _trace({"q1": {"documentdb": 70, "dynamodb": 80, "aurora_mysql": 75}})

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        (move,) = ledger.moves
        assert move.to_engine == "dynamodb"
        assert move.runner_up is not None
        assert (move.runner_up.engine, move.runner_up.score) == ("aurora_mysql", 75.0)
        assert move.evidence.tie_rule is None
        _assert_replays(qas, revised, ledger)

    def test_a_group_move_carries_the_groups_key_and_mean_scores(self) -> None:
        qas = [
            {"query_id": "g1", "assigned_engine": "documentdb"},
            {"query_id": "g2", "assigned_engine": "documentdb"},
        ]
        group = {
            "key": "abc123abc123",
            "scores": {"documentdb": 60, "dynamodb": 72, "aurora_mysql": 65},
        }
        trace = _trace(
            {
                "g1": {"documentdb": 60, "dynamodb": 80, "aurora_mysql": 60},
                "g2": {"documentdb": 60, "dynamodb": 64, "aurora_mysql": 70},
            },
            {"g1": group, "g2": group},
        )

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        assert [m.query_id for m in ledger.moves] == ["g1", "g2"]
        for move in ledger.moves:
            assert move.group_key == "abc123abc123"
            assert (move.score_before, move.score_after) == (60, 72)
            assert move.runner_up is not None
            assert (move.runner_up.engine, move.runner_up.score) == ("aurora_mysql", 65)
            assert move.evidence.group_scores == {"dynamodb": 72, "aurora_mysql": 65}
        _assert_replays(qas, revised, ledger)

    def test_a_tie_that_picked_the_destination_is_in_evidence(self) -> None:
        """One reason-code rule: the code says why the query left (settle), the
        tie that picked the destination is ``evidence.tie_rule``."""
        qas = [{"query_id": "q1", "assigned_engine": "documentdb"}]
        trace = _trace({"q1": {"documentdb": 60, "dynamodb": 80, "aurora_mysql": 80}})

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        (move,) = ledger.moves
        assert move.reason_code is ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE
        assert move.evidence.tie_rule in {
            ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD,
            ReasonCode.TIE_PLACEHOLDER_HASH,
        }
        assert move.tied_engines == ["aurora_mysql", "dynamodb"]
        assert move.evidence.previous_tied_engines == []
        _assert_replays(qas, revised, ledger)

    def test_a_cleared_tie_is_coded_and_keeps_the_earlier_tie(self) -> None:
        qas = [
            {
                "query_id": "q1",
                "assigned_engine": "dynamodb",
                "tied_engines": ["documentdb", "dynamodb"],
            }
        ]
        trace = _trace({"q1": {"documentdb": 70, "dynamodb": 70}})

        revised, ledger = _place(qas, {"dynamodb"}, trace)

        (move,) = ledger.moves
        assert move.from_engine == move.to_engine == "dynamodb"
        assert move.reason_code is ReasonCode.TIE_CLEARED
        assert move.evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        assert move.tied_engines == []
        _assert_replays(qas, revised, ledger)


class TestTheFloorRunnerUpUsesTheWinnersRanking:
    def test_a_mandatory_secondary_outranks_an_equal_score_by_name(self) -> None:
        """The absorber ranks on rounded fit, then mandatory-secondary. With
        DocumentDB and DynamoDB both at 70 and only DynamoDB a mandatory
        secondary, the runner-up is DynamoDB -- a plain highest-score pick with
        a name-order tie would have said DocumentDB."""
        from src.agents.referee.reality_check_floors import _find_best_absorber_for_query

        query = {"query_id": "s1", "query_type": "SELECT", "tables_accessed": ["t_s1"]}
        ctx = _context({"s1": {"aurora_mysql": 80, "documentdb": 70, "dynamodb": 70}})

        absorber = _find_best_absorber_for_query(
            {"query_id": "s1", "assigned_engine": "opensearch"},
            {"aurora_mysql", "documentdb", "dynamodb"},
            "opensearch",
            {},
            {"s1": query},
            {},
            {"dynamodb"},
            "",
            ctx,
        )

        assert absorber is not None
        assert absorber["target_engine"] == "aurora_mysql"
        runner_up = absorber["runner_up"]
        assert (runner_up.engine, runner_up.score) == ("dynamodb", 70.0)

    def test_an_individual_floor_move_records_the_absorbers_runner_up(self) -> None:
        result = TestJustificationFloor()._run_with_two_destinations()

        (move,) = [m for m in result["move_ledger"].moves if m.query_id == "search"]
        assert move.pass_ is MovePass.FLOOR
        assert move.runner_up is not None
        assert move.runner_up.engine != move.to_engine
        assert move.runner_up.score is not None and move.score_after is not None
        assert move.runner_up.score <= move.score_after


class TestTheSweepBeforeScoreIgnoresAModelFlag:
    def test_a_query_flagged_on_the_orphan_engine_still_reports_its_score_there(self) -> None:
        """#755, "ignoring a model flag": a query a flag gated on the orphan engine
        reports its own score there (as ``flag_decisions`` does), never ``n/a``."""
        assignments = _orphan_documentdb()
        query_map = _query_map(*(qa["query_id"] for qa in assignments))
        ctx = _context(
            {f"d{i}": {"documentdb": 60, "aurora_mysql": 95, "dynamodb": 5} for i in range(3)}
        ).with_flag_gaps({"d0": ["documentdb"]})
        ledger = ledger_for(assignments)

        updated, _, _ = sanity_sweep(assignments, [], ctx, query_map, ledger=ledger)

        d0 = next(m for m in ledger.moves if m.query_id == "d0")
        assert d0.score_before == 60.0
        reason = next(qa["assignment_reason"] for qa in updated if qa["query_id"] == "d0")
        assert "DocumentDB scores 60.0, Aurora MySQL scores 95.0" in reason
        # The flag still gates the engine in the Move's ladder.
        assert {c.engine: c.gate for c in d0.evidence.candidates}["documentdb"] == "model_flag"


class TestOneQueryMovedTwiceInOneRun:
    def test_a_flag_then_the_sweep_replays_in_order(self) -> None:
        """q1 is flagged off Aurora MySQL onto a small DocumentDB, which the sweep
        then removes: two Moves for one query, in seq order, and replay rebuilds
        the result."""
        original = (
            [{"query_id": "q1", "assigned_engine": "dynamodb"}]
            + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
            + [{"query_id": f"d{i}", "assigned_engine": "documentdb"} for i in range(2)]
        )
        revised = [
            {"query_id": "q1", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"},
            *[dict(qa) for qa in original[1:]],
        ]
        before = [dict(qa) for qa in revised]
        consolidation = {
            "from_engine": "dynamodb",
            "to_engine": "aurora_mysql",
            "query_count": 1,
            "reason": "no unique value",
            "saved_cost_estimate": 0,
            "action": "partial",
            "queries_retained": [],
            "retention_reason": None,
        }
        query_map = _query_map(*(qa["query_id"] for qa in revised))
        ctx = _context(
            {
                "q1": {"aurora_mysql": 70, "documentdb": 90, "dynamodb": 50},
                "d0": {"documentdb": 60, "dynamodb": 70},
                "d1": {"documentdb": 60, "dynamodb": 70},
            }
        )
        ledger = ledger_for(revised)

        revised, records, decisions, gaps = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            [consolidation],
            original,
            ctx,
            query_map,
            {"aurora_mysql", "documentdb", "dynamodb"},
            ledger=ledger,
        )
        assert decisions[0]["destination_engine"] == "documentdb"
        revised, _, _ = sanity_sweep(
            revised, records, ctx.with_flag_gaps(gaps), query_map, ledger=ledger
        )

        q1 = [m for m in ledger.moves if m.query_id == "q1"]
        assert [(m.pass_, m.from_engine, m.to_engine) for m in q1] == [
            (MovePass.MODEL_FLAG, "aurora_mysql", "documentdb"),
            (MovePass.SWEEP, "documentdb", "dynamodb"),
        ]
        assert q1[0].seq < q1[1].seq
        _assert_replays(before, revised, ledger)


class TestAGroupPlacedByScoreClearsEveryMembersTie:
    """#845: a co-dependency group placed by score (no tie at group level)
    leaves no member reading an earlier tie, whichever member triggered the
    group. ``tied_engines`` describes the decision that placed the query, and
    the Move that clears it keeps the earlier tie in evidence."""

    _GROUP_QAS: list[dict[str, Any]] = [
        {"query_id": "m1", "assigned_engine": "documentdb"},
        {
            "query_id": "m2",
            "assigned_engine": "documentdb",
            "tied_engines": ["documentdb", "dynamodb"],
        },
    ]

    def _group_trace(self, scores: dict[str, float]) -> dict:
        group = {"key": "abc123abc123", "scores": dict(scores)}
        return _trace({"m1": dict(scores), "m2": dict(scores)}, {"m1": group, "m2": group})

    def test_a_group_moved_by_score_clears_a_members_old_tie(self) -> None:
        qas = [dict(qa) for qa in self._GROUP_QAS]
        trace = self._group_trace({"documentdb": 60, "dynamodb": 80, "aurora_mysql": 70})

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        by_id = {qa["query_id"]: qa for qa in revised}
        assert {q: qa["assigned_engine"] for q, qa in by_id.items()} == {
            "m1": "dynamodb",
            "m2": "dynamodb",
        }
        assert by_id["m2"]["tied_engines"] == []
        moves = {m.query_id: m for m in ledger.moves}
        assert moves["m2"].tied_engines == []
        assert moves["m2"].evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        assert moves["m2"].evidence.tie_rule is None
        assert moves["m2"].reason_code is ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE
        assert "tied" not in by_id["m2"]["assignment_reason"]
        _assert_replays(qas, revised, ledger)

    def test_a_group_kept_by_score_clears_a_members_old_tie(self) -> None:
        qas = [{**qa, "assigned_engine": "dynamodb"} for qa in self._GROUP_QAS]
        trace = self._group_trace({"documentdb": 60, "dynamodb": 80, "aurora_mysql": 70})

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        (move,) = ledger.moves
        assert move.query_id == "m2"
        assert move.from_engine == move.to_engine == "dynamodb"
        assert move.reason_code is ReasonCode.TIE_CLEARED
        assert move.tied_engines == []
        assert move.evidence.previous_tied_engines == ["documentdb", "dynamodb"]
        m2 = next(qa for qa in revised if qa["query_id"] == "m2")
        assert m2["tied_engines"] == []
        assert "resolved" in m2["assignment_reason"]
        _assert_replays(qas, revised, ledger)

    def test_a_group_tie_left_unresolved_is_every_members_tie(self) -> None:
        # m2's own tie covers both kept engines, so the group's tie (#833,
        # ``tie_break.group_tie``) survives as Aurora MySQL and DynamoDB and
        # the equal scores leave it unresolved. With m2 tied on DocumentDB and
        # DynamoDB only, DynamoDB alone survives and settles it (Decision 12).
        qas = [dict(qa) for qa in self._GROUP_QAS]
        qas[1]["tied_engines"] = ["aurora_mysql", "documentdb", "dynamodb"]
        trace = self._group_trace({"documentdb": 60, "dynamodb": 80, "aurora_mysql": 80})

        revised, ledger = _place(qas, {"dynamodb", "aurora_mysql"}, trace)

        for qa in revised:
            assert qa["tied_engines"] == ["aurora_mysql", "dynamodb"]
        _assert_replays(qas, revised, ledger)
