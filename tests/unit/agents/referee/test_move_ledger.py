"""The move ledger (#616): one function records every first-assignment engine choice.

``move_ledger.apply`` records a Move and sets the engine; the resolver's
first-assignment passes all go through it, so ``Assignment.moves`` replays to
the assignment exactly. These tests use small synthetic workloads (no
pipeline run); ``tests/e2e/test_routing_snapshot.py`` checks the same replay on
the bundled samples.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agents.referee import move_ledger
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import Move, MovePass, ReasonCode


def _query(qid: str, tables: list[str], **extra: object) -> dict:
    q: dict = {
        "query_id": qid,
        "query_text": "SELECT ...",
        "query_type": "SELECT",
        "tables_accessed": tables,
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "filter_tables": [],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }
    q.update(extra)
    return q


def _collector(queries: list[dict], tables: list[str], source: str | None = None) -> dict:
    collector: dict = {
        "job_id": "test",
        "database_schema": {
            "tables": [{"table_id": t, "table_name": t.split(".")[-1]} for t in tables]
        },
        "queries": {"query_patterns": queries},
    }
    if source:
        collector["metadata"] = {"source_database": {"engine": source}}
    return collector


def _triage(engines: list[str], signals: list[dict] | None = None) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": signals or []}


def _analysis(confidence_by_table: dict[str, int]) -> dict:
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": c} for t, c in confidence_by_table.items()
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


def _assert_replays(assignment: Assignment) -> None:
    """The ledger rebuilds the assignment exactly, and every query has one first Move."""
    replayed = move_ledger.replay(assignment.moves)
    expected = {
        qa.query_id: (qa.assigned_engine, list(qa.tied_engines))
        for qa in assignment.query_assignments
    }
    assert replayed == expected
    firsts = [m for m in assignment.moves if m.from_engine is None]
    assert sorted(m.query_id for m in firsts) == sorted(expected)
    assert [m.seq for m in assignment.moves] == list(range(1, len(assignment.moves) + 1))
    for m in firsts:
        assert m.score_before is None
        assert m.evidence.candidates, f"{m.query_id}: no candidate ladder"


def _moves_for(assignment: Assignment, qid: str) -> list[Move]:
    return [m for m in assignment.moves if m.query_id == qid]


class TestApply:
    def test_first_move_has_no_from_engine_and_later_moves_carry_it(self):
        ledger = move_ledger.MoveLedger()
        first = move_ledger.apply(
            ledger, "q1", "dynamodb", pass_=MovePass.SCORE, reason_code=ReasonCode.HIGHEST_SCORE
        )
        second = move_ledger.apply(
            ledger,
            "q1",
            "aurora_mysql",
            pass_=MovePass.UTILITY_PIN,
            reason_code=ReasonCode.OVERRIDE_UTILITY,
        )
        assert (first.seq, first.from_engine, first.to_engine) == (1, None, "dynamodb")
        assert (second.seq, second.from_engine, second.to_engine) == (2, "dynamodb", "aurora_mysql")
        assert ledger.engine_of("q1") == "aurora_mysql"
        assert move_ledger.replay(ledger.moves) == {"q1": ("aurora_mysql", [])}

    def test_a_first_move_has_no_score_before(self):
        with pytest.raises(ValueError, match="no score_before"):
            move_ledger.apply(
                move_ledger.MoveLedger(),
                "q1",
                "dynamodb",
                pass_=MovePass.SCORE,
                reason_code=ReasonCode.HIGHEST_SCORE,
                score_before=50,
            )

    def test_an_empty_engine_is_rejected(self):
        with pytest.raises(ValueError, match="empty to_engine"):
            move_ledger.apply(
                move_ledger.MoveLedger(),
                "q1",
                "",
                pass_=MovePass.SCORE,
                reason_code=ReasonCode.HIGHEST_SCORE,
            )

    def test_replay_follows_seq_not_list_order(self):
        ledger = move_ledger.MoveLedger()
        move_ledger.apply(
            ledger, "q1", "a", pass_=MovePass.SCORE, reason_code=ReasonCode.HIGHEST_SCORE
        )
        move_ledger.apply(
            ledger, "q1", "b", pass_=MovePass.UTILITY_PIN, reason_code=ReasonCode.OVERRIDE_UTILITY
        )
        assert move_ledger.replay(reversed(ledger.moves)) == {"q1": ("b", [])}


class TestMoveContract:
    def test_pass_reads_and_writes_as_pass_in_json(self):
        move = Move(
            seq=1,
            query_id="q1",
            pass_=MovePass.SCORE,
            to_engine="dynamodb",
            reason_code=ReasonCode.HIGHEST_SCORE,
        )
        dumped = move.model_dump(mode="json")
        assert dumped["pass"] == "score"
        assert "pass_" not in dumped
        assert Move.model_validate(dumped) == move

    def test_the_dumped_key_is_pass_in_every_write_path(self):
        import json

        move = Move(
            seq=1,
            query_id="q1",
            pass_=MovePass.SCORE,
            to_engine="dynamodb",
            reason_code=ReasonCode.HIGHEST_SCORE,
        )
        assignment = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 80}),
                "aurora_mysql": _analysis({"db.t": 60}),
            },
            _collector([_query("q1", ["db.t"])], ["db.t"], source="mysql"),
        )
        assert assignment.moves
        for dumped in (
            move.model_dump(),
            json.loads(move.model_dump_json()),
            json.loads(assignment.model_dump_json())["moves"][0],
            assignment.model_dump(mode="json")["moves"][0],
        ):
            assert "pass" in dumped and "pass_" not in dumped

    def test_the_pydantic_floor_supports_serialize_by_alias(self):
        # ``serialize_by_alias`` exists from pydantic 2.11; on an older
        # pydantic, ``Move`` would silently write ``pass_`` to disk.
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[4]
        for path in (root / "pyproject.toml", root / "src/atx_orchestrator/requirements.txt"):
            match = re.search(r"pydantic>=(\d+)\.(\d+)", path.read_text())
            assert match, f"{path}: no pydantic floor"
            assert (int(match[1]), int(match[2])) >= (2, 11), f"{path}: pydantic floor below 2.11"

    def test_an_unknown_key_is_rejected(self):
        with pytest.raises(ValidationError):
            Move.model_validate(
                {
                    "seq": 1,
                    "query_id": "q1",
                    "pass": "score",
                    "to_engine": "dynamodb",
                    "reason_code": "highest_score",
                    "why": "free text",
                }
            )

    def test_assignment_round_trips_its_moves(self):
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 80}),
                "aurora_mysql": _analysis({"db.t": 60}),
            },
            _collector([_query("q1", ["db.t"])], ["db.t"], source="mysql"),
        )
        reloaded = Assignment.model_validate(result.model_dump(mode="json"))
        assert reloaded.moves == result.moves


class TestFirstAssignmentMoves:
    def test_highest_score_records_the_ladder_and_runner_up(self):
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 80}),
                "aurora_mysql": _analysis({"db.t": 60}),
            },
            _collector([_query("q1", ["db.t"])], ["db.t"], source="mysql"),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert (move.pass_, move.reason_code) == (MovePass.SCORE, ReasonCode.HIGHEST_SCORE)
        assert move.to_engine == "dynamodb"
        assert move.score_after == result.query_assignments[0].confidence
        assert move.runner_up is not None and move.runner_up.engine == "aurora_mysql"
        assert {c.engine for c in move.evidence.candidates} == {"dynamodb", "aurora_mysql"}

    def test_a_write_records_the_write_gate_in_the_ladder(self):
        result = AssignmentResolver().resolve(
            _triage(["opensearch", "aurora_mysql"]),
            {
                "opensearch": _analysis({"db.t": 95}),
                "aurora_mysql": _analysis({"db.t": 40}),
            },
            _collector(
                [_query("q1", ["db.t"], query_type="UPDATE", query_text="UPDATE t SET a=1")],
                ["db.t"],
                source="mysql",
            ),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert move.to_engine == "aurora_mysql"
        gates = {c.engine: c.gate for c in move.evidence.candidates}
        assert gates["opensearch"] == "write_gate"
        # The runner-up is among the engines that may own the query.
        assert move.runner_up is None

    def test_an_individual_tie_records_the_tie_rule(self):
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 70}),
                "aurora_mysql": _analysis({"db.t": 70}),
            },
            _collector([_query("q1", ["db.t"])], ["db.t"], source="mysql"),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert (move.pass_, move.reason_code) == (MovePass.TIE, ReasonCode.TIE_SOURCE_COMPATIBLE)
        assert move.to_engine == "aurora_mysql"
        assert move.tied_engines == []

    def test_an_unresolved_tie_records_the_tied_engines_and_placeholder_rule(self):
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "documentdb"]),
            {
                "dynamodb": _analysis({"db.t": 70}),
                "documentdb": _analysis({"db.t": 70}),
            },
            _collector([_query("q1", ["db.t"])], ["db.t"]),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert move.pass_ == MovePass.TIE
        assert move.reason_code == ReasonCode.TIE_PLACEHOLDER_HASH
        assert move.tied_engines == ["documentdb", "dynamodb"]
        assert move.tied_engines == result.query_assignments[0].tied_engines

    def test_a_co_dependency_group_moves_with_its_group_key_and_scores(self):
        joined = {"join_count": 2, "has_joins": True}
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 50}),
                "aurora_mysql": _analysis({"db.t": 75}),
            },
            _collector(
                [_query("qa", ["db.t"], **joined), _query("qb", ["db.t"], **joined)],
                ["db.t"],
                source="mysql",
            ),
        )
        _assert_replays(result)
        moves = [*_moves_for(result, "qa"), *_moves_for(result, "qb")]
        assert {(m.pass_, m.reason_code) for m in moves} == {
            (MovePass.CO_DEPENDENCY, ReasonCode.GROUP_MEAN)
        }
        assert {m.group_key for m in moves} == {move_ledger.group_key(["qa", "qb"])}
        assert moves[0].evidence.group_scores == {"dynamodb": 50.0, "aurora_mysql": 75.0}
        assert moves[0].runner_up is not None and moves[0].runner_up.engine == "dynamodb"

    def test_a_signal_override_records_its_signal(self):
        text = "SELECT * FROM db.t WHERE MATCH(bio) AGAINST (?)"
        result = AssignmentResolver().resolve(
            _triage(
                ["opensearch", "dynamodb"],
                signals=[{"signal": "text_search", "targets": ["opensearch"], "query_ids": ["q1"]}],
            ),
            {
                "opensearch": _analysis({"db.t": 30}),
                "dynamodb": _analysis({"db.t": 80}),
            },
            _collector([_query("q1", ["db.t"], query_text=text, calls_per_second=5.0)], ["db.t"]),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert move.to_engine == "opensearch"
        assert (move.pass_, move.reason_code) == (
            MovePass.SIGNAL_OVERRIDE,
            ReasonCode.OVERRIDE_TEXT_SEARCH,
        )
        assert move.evidence.signal == "text_search"
        assert {c.engine for c in move.evidence.candidates} == {"dynamodb", "opensearch"}

    def test_a_query_no_engine_scores_falls_back_with_a_move(self):
        result = AssignmentResolver().resolve(
            _triage(["opensearch", "aurora_mysql"]),
            {"opensearch": _analysis({"db.t": 90})},
            _collector(
                [_query("q1", ["db.t"], query_type="UPDATE", query_text="UPDATE t SET a=1")],
                ["db.t"],
                source="mysql",
            ),
        )
        _assert_replays(result)
        (move,) = _moves_for(result, "q1")
        assert (move.pass_, move.reason_code) == (
            MovePass.AURORA_FALLBACK,
            ReasonCode.FALLBACK_NO_SCORE,
        )
        assert move.to_engine == "aurora_mysql"
        assert move.score_after is None

    def test_a_utility_statement_records_the_scored_pick_then_the_pin(self):
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 95}),
                "aurora_mysql": _analysis({"db.t": 10}),
            },
            _collector(
                [_query("q1", ["db.t"], query_text="SHOW FULL FIELDS FROM `t`")],
                ["db.t"],
                source="mysql",
            ),
        )
        _assert_replays(result)
        # The scored pick feeds the settled-workload counts later ties read, so
        # it is a real Move; the pin then replaces it.
        scored, pinned = _moves_for(result, "q1")
        assert scored.pass_ == MovePass.SCORE
        assert (pinned.pass_, pinned.reason_code) == (
            MovePass.UTILITY_PIN,
            ReasonCode.OVERRIDE_UTILITY,
        )
        assert pinned.from_engine == scored.to_engine
        assert pinned.to_engine == "aurora_mysql"
        assert pinned.evidence.replaced_engine == scored.to_engine

    def test_a_pin_that_changes_the_engine_records_both_engines(self):
        # Every pin on the bundled samples is a no-op (the scored pick is
        # already the pinned engine); here the scored pick is Aurora
        # PostgreSQL and the source-compatible pin moves it to Aurora MySQL.
        result = AssignmentResolver().resolve(
            _triage(["aurora_postgresql", "aurora_mysql"]),
            {
                "aurora_postgresql": _analysis({"db.t": 95}),
                "aurora_mysql": _analysis({"db.t": 10}),
            },
            _collector(
                [_query("q1", ["db.t"], query_text="SHOW FULL FIELDS FROM `t`")],
                ["db.t"],
                source="mysql",
            ),
        )
        _assert_replays(result)
        scored, pinned = _moves_for(result, "q1")
        assert (scored.pass_, scored.to_engine) == (MovePass.SCORE, "aurora_postgresql")
        assert (pinned.from_engine, pinned.to_engine) == ("aurora_postgresql", "aurora_mysql")
        assert pinned.evidence.replaced_engine == "aurora_postgresql"
        assert pinned.score_after == 10
        assert result.query_assignments[0].assigned_engine == "aurora_mysql"

    @pytest.mark.parametrize("source", [None, "sqlserver"])
    def test_a_gated_engine_never_puts_a_pinned_query_in_a_tie(self, source):
        # DynamoDB and DocumentDB fail the capability gate for SHOW, so they
        # are not candidates (ADR-031 Decision 2 step 2, #766) and never tie
        # with Aurora PostgreSQL at 0: the query is scored onto Aurora
        # PostgreSQL outright, the pin settles it, and no tie is recorded
        # anywhere. Before #766 the gated engines entered an unresolved tie.
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "documentdb", "aurora_postgresql"]),
            {
                "dynamodb": _analysis({"db.t": 80}),
                "documentdb": _analysis({"db.t": 80}),
                "aurora_postgresql": _analysis({"db.t": 0}),
            },
            _collector(
                [_query("q1", ["db.t"], query_text="SHOW FULL FIELDS FROM `t`")],
                ["db.t"],
                source=source,
            ),
        )
        scored, pinned = _moves_for(result, "q1")
        assert (scored.pass_, scored.to_engine) == (MovePass.SCORE, "aurora_postgresql")
        assert scored.tied_engines == []
        assert pinned.pass_ == MovePass.UTILITY_PIN and pinned.tied_engines == []
        (qa,) = result.query_assignments
        assert (qa.assigned_engine, qa.tied_engines) == ("aurora_postgresql", [])
        _assert_replays(result)


class TestLadderAgreesWithTheRoutingTrace:
    def test_every_candidate_score_is_the_score_the_trace_records(self):
        from src.agents.referee.routing_trace import RoutingTrace

        trace = RoutingTrace()
        result = AssignmentResolver().resolve(
            _triage(["dynamodb", "opensearch", "aurora_mysql"]),
            {
                "dynamodb": _analysis({"db.t": 80}),
                "opensearch": _analysis({"db.t": 90}),
                "aurora_mysql": _analysis({"db.t": 60}),
            },
            _collector(
                [
                    _query("q1", ["db.t"]),
                    _query("q2", ["db.t"], query_type="UPDATE", query_text="UPDATE t SET a=1"),
                ],
                ["db.t"],
                source="mysql",
            ),
            routing_trace=trace,
        )
        _assert_replays(result)
        for move in result.moves:
            ladder = trace.by_query[move.query_id]["engines"]
            for c in move.evidence.candidates:
                entry = ladder[c.engine]
                if isinstance(entry, dict):
                    assert c.gate == entry.get("gate")
                    assert c.score == entry.get("used")
                else:
                    assert (c.gate, c.score) == (None, entry)
