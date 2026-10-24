"""A query's ``confidence`` is its score on the engine it ends on (#1373).

The move ledger sets it with the engine: ``move_ledger.apply`` and
``apply_to_assignment`` give the query the score its Move records for the
engine it lands on (the query's own rung of the candidate ladder, else the
Move's ``score_after``), the figure the report's query trace shows. An
assignment saved before that keeps the engine it left's score; readers correct
it from the Moves of its lineage (``move_ledger.confidence_from_moves``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agents.referee import move_ledger
from src.agents.referee.assignment_overrides import load_assignment_for_edit
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.contracts.move_ledger import CandidateScore, MoveEvidence, MovePass, ReasonCode
from src.shared.query_trace import build_query_trace
from src.storage.local_store import LocalArtifactStore

LADDER = [
    CandidateScore(engine="aurora_mysql", score=70.0),
    CandidateScore(engine="documentdb", score=79.0),
    CandidateScore(engine="dynamodb", score=78.0),
]


def _settled(qa: dict | QueryAssignment) -> move_ledger.MoveLedger:
    """A ledger with the query on documentdb (its first assignment, scored 79)."""
    ledger = move_ledger.MoveLedger()
    move_ledger.apply(
        ledger,
        "q1",
        "documentdb",
        pass_=MovePass.SCORE,
        reason_code=ReasonCode.HIGHEST_SCORE,
        score_after=79.0,
        evidence=MoveEvidence(candidates=LADDER),
    )
    return ledger


def _qa_dict() -> dict:
    return {"query_id": "q1", "assigned_engine": "documentdb", "confidence": 79, "tied_engines": []}


def _qa_model() -> QueryAssignment:
    return QueryAssignment(
        query_id="q1",
        assigned_engine="documentdb",
        confidence=79,
        source_tables=["wp.wp_options"],
        assignment_reason="highest fit for DocumentDB",
    )


@pytest.mark.parametrize("make", [_qa_dict, _qa_model], ids=["dict", "model"])
def test_a_consolidation_gives_the_query_its_new_engines_score(make) -> None:
    qa = make()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "dynamodb",
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        score_before=79.0,
        score_after=78.0,
        evidence=MoveEvidence(candidates=LADDER),
        tied_engines=[],
    )
    confidence = qa["confidence"] if isinstance(qa, dict) else qa.confidence
    assert confidence == 78
    assert ledger.confidence_of("q1") == 78


def test_the_querys_own_rung_wins_over_a_group_mean_score_after() -> None:
    """A co-dependency Move may record the group's mean as ``score_after``; the
    query's confidence is its own score on the engine (its ladder rung)."""
    qa = _qa_dict()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "dynamodb",
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        score_after=74.5,
        evidence=MoveEvidence(candidates=LADDER, group_scores={"dynamodb": 74.5}),
        tied_engines=[],
    )
    assert qa["confidence"] == 78


def test_without_a_ladder_the_moves_score_after_is_the_score() -> None:
    qa = _qa_dict()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "aurora_mysql",
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        score_after=70.0,
        tied_engines=[],
    )
    assert qa["confidence"] == 70


def test_a_gated_rung_falls_back_to_score_after() -> None:
    qa = _qa_dict()
    ledger = _settled(qa)
    gated = [*LADDER[:2], CandidateScore(engine="dynamodb", score=None, gate="capability")]
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "dynamodb",
        pass_=MovePass.SWEEP,
        reason_code=ReasonCode.SWEEP_ORPHAN,
        score_after=61.0,
        evidence=MoveEvidence(candidates=gated),
        tied_engines=[],
    )
    assert qa["confidence"] == 61


def test_an_engine_change_with_no_score_has_confidence_zero() -> None:
    """Never the engine it left's score: an unscored engine reads 0, as the first
    assignment's no-score fallback does."""
    qa = _qa_dict()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "aurora_mysql",
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        tied_engines=[],
    )
    assert qa["confidence"] == 0


@pytest.mark.passes_on_main
def test_a_tie_only_move_with_no_score_keeps_the_confidence() -> None:
    qa = {**_qa_dict(), "tied_engines": ["documentdb", "dynamodb"]}
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "documentdb",
        pass_=MovePass.PLACE,
        reason_code=ReasonCode.TIE_CLEARED,
        tied_engines=[],
    )
    assert qa["confidence"] == 79


def test_a_score_is_rounded_and_kept_in_range() -> None:
    qa = _qa_dict()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "aurora_mysql",
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        score_after=101.6,
        tied_engines=[],
    )
    assert qa["confidence"] == 100
    assert move_ledger.confidence_after(qa_move(score=70.6)) == 71
    assert move_ledger.confidence_after(qa_move(score=-3.0)) == 0


def qa_move(score: float | None, to_engine: str = "aurora_mysql"):
    ledger = move_ledger.MoveLedger({"q1": "documentdb"})
    return move_ledger.apply(
        ledger,
        "q1",
        to_engine,
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        score_after=score,
    )


def test_the_report_trace_shows_the_same_score() -> None:
    """The report's ``final_score`` and the confidence are one figure."""
    qa = _qa_dict()
    ledger = _settled(qa)
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "dynamodb",
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        score_after=74.5,
        evidence=MoveEvidence(candidates=LADDER, group_scores={"dynamodb": 74.5}),
        tied_engines=[],
    )
    trace = build_query_trace("q1", [(1, m) for m in ledger.moves], "dynamodb", {"dynamodb"})
    assert trace.final_score == qa["confidence"] == 78


# --- old assignments: corrected on read -------------------------------------


def _version(version: int, qas: list[QueryAssignment], moves, previous: int | None) -> dict:
    data = Assignment(
        job_id="job",
        version=version,
        previous_version=previous,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
        moves=moves,
    ).model_dump(mode="json")
    return data


def _stale_lineage() -> tuple[dict, dict]:
    """v1 (q1 on documentdb, 79) and a v2 written before #1373: q1 consolidated
    onto dynamodb (78) but still carrying 79."""
    ledger = move_ledger.MoveLedger()
    for qid in ("q1", "q2"):
        move_ledger.apply(
            ledger,
            qid,
            "documentdb",
            pass_=MovePass.SCORE,
            reason_code=ReasonCode.HIGHEST_SCORE,
            score_after=79.0,
            evidence=MoveEvidence(candidates=LADDER),
        )
    v1 = _version(
        1, [_qa_model(), _qa_model().model_copy(update={"query_id": "q2"})], ledger.moves, None
    )
    v2_ledger = move_ledger.MoveLedger({"q1": "documentdb", "q2": "documentdb"})
    move_ledger.apply(
        v2_ledger,
        "q1",
        "dynamodb",
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        score_before=79.0,
        score_after=78.0,
        evidence=MoveEvidence(candidates=LADDER),
    )
    stale_q1 = QueryAssignment(
        query_id="q1",
        assigned_engine="dynamodb",
        confidence=79,
        source_tables=["wp.wp_options"],
        assignment_reason="reality check: consolidated from DocumentDB → DynamoDB",
    )
    v2 = _version(
        2,
        [stale_q1, _qa_model().model_copy(update={"query_id": "q2"})],
        v2_ledger.moves,
        1,
    )
    return v1, v2


def _confidences(assignment: dict) -> dict[str, int]:
    return {qa["query_id"]: qa["confidence"] for qa in assignment["query_assignments"]}


def test_a_stale_saved_version_is_corrected_from_its_lineages_moves() -> None:
    v1, v2 = _stale_lineage()
    corrected = move_ledger.confidence_from_moves(v2, [v1, v2])
    assert corrected == ["q1"]
    assert _confidences(v2) == {"q1": 78, "q2": 79}


def test_a_later_version_with_no_move_for_the_query_is_corrected_too() -> None:
    """A customer edit (v3) made on the stale v2 holds no Move for q1: the lineage's
    last Move for q1 (v2's) still gives its score."""
    v1, v2 = _stale_lineage()
    v3 = {**v2, "version": 3, "previous_version": 2, "moves": []}
    assert move_ledger.confidence_from_moves(v3, [v1, v2, v3]) == ["q1"]
    assert _confidences(v3)["q1"] == 78


def test_a_query_whose_moves_do_not_end_on_its_engine_is_left_alone() -> None:
    """No Move explains the engine (a hand-edited or pre-ledger file): the stored
    confidence is kept rather than guessed."""
    v1, v2 = _stale_lineage()
    v2["query_assignments"][0]["assigned_engine"] = "aurora_mysql"
    assert move_ledger.confidence_from_moves(v2, [v1, v2]) == []
    assert _confidences(v2)["q1"] == 79


def test_an_assignment_with_no_moves_is_left_alone() -> None:
    v1, v2 = _stale_lineage()
    for data in (v1, v2):
        data["moves"] = []
    assert move_ledger.confidence_from_moves(v2, [v1, v2]) == []
    assert _confidences(v2) == {"q1": 79, "q2": 79}


def test_the_edit_surface_loads_a_stale_version_corrected(tmp_path) -> None:
    """The assignment review (API) reads versions through ``load_assignment_for_edit``."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    v1, v2 = _stale_lineage()
    store.write_json("db/job/assignment/v1/assignment.json", v1)
    store.write_json("db/job/assignment/v2/assignment.json", v2)
    data, _, _, _ = load_assignment_for_edit(store, "db", "job", 2)
    assert _confidences(data) == {"q1": 78, "q2": 79}
    # The stored artifact is not rewritten.
    assert _confidences(store.read_json("db/job/assignment/v2/assignment.json"))["q1"] == 79


# --- the customer gate's group propagation ------------------------------------


def _propagated(monkeypatch=None, *, unscored: bool = False):
    """A customer pick of q1 onto Aurora PostgreSQL that pulls its JOIN group (q2, q3)
    along; ``unscored`` drops the propagation's scores callback, as a caller with no
    scorer would."""
    from src.agents.referee import assignment_overrides
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from tests.unit import test_assignment_overrides as overrides_tests
    from tests.unit.test_assignment_overrides import DB, JOB, _MemStore

    if unscored:
        original = assignment_overrides._propagate_codependent_overrides

        def without_scores(*args, **kwargs):
            kwargs["scores"] = None
            return original(*args, **kwargs)

        monkeypatch.setattr(
            assignment_overrides, "_propagate_codependent_overrides", without_scores
        )
    store = _MemStore()
    overrides_tests.TestCoDependencyPropagation._seed_join_group(
        store, engines=["dynamodb", "aurora_postgresql"]
    )
    result = apply_assignment_overrides(
        store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
    )
    moves = {m.query_id: m for m in result.assignment.moves}
    qas = {qa.query_id: qa for qa in result.assignment.query_assignments}
    return moves, qas


def test_a_propagated_group_mate_takes_its_new_engines_score() -> None:
    moves, qas = _propagated()
    for qid in ("q1", "q2", "q3"):
        assert moves[qid].to_engine == qas[qid].assigned_engine == "aurora_postgresql"
        assert moves[qid].score_after is not None
        assert qas[qid].confidence == round(moves[qid].score_after), qid


def test_an_unscored_group_propagation_reads_zero_and_says_so(monkeypatch, caplog) -> None:
    """A Move that changes the engine with no score: the query reads 0 on its new
    engine, consistent with an unscored engine everywhere else in the fix (never the
    score of the engine it left), and the ledger logs a warning naming the query, so
    the 0 is never silent."""
    with caplog.at_level("WARNING", logger="src.agents.referee.move_ledger"):
        moves, qas = _propagated(monkeypatch, unscored=True)
    for qid in ("q2", "q3"):
        assert moves[qid].pass_ is MovePass.CUSTOMER_GROUP_PROPAGATION
        assert moves[qid].score_after is None
        assert qas[qid].confidence == 0, qid
        assert any(
            qid in r.getMessage() and "no score recorded" in r.getMessage() for r in caplog.records
        ), qid
    # The scored pick itself warns about nothing.
    assert qas["q1"].confidence == round(moves["q1"].score_after)
    assert not any("'q1'" in r.getMessage() for r in caplog.records)
