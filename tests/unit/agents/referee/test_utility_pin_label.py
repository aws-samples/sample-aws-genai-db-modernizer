"""A utility pin shows "pinned (utility)", not a percentage (#1373, maintainer
decision on #1381, 2026-10-22).

The first assignment's utility pin (Move rule ``override.utility``) is recorded on
the query's assignment as ``pinned = "utility"`` by the move ledger, with the
engine; ``confidence`` keeps the query's real score there. Every view reads the
pin through ``src.shared.workload_fit_labels`` and shows its label, and the
mean-fit figures leave pins out. A version saved before the field existed gets
it on read from its lineage's Moves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.agents.referee import move_ledger
from src.agents.referee.assignment_overrides import load_assignment_for_edit
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.contracts.move_ledger import CandidateScore, MoveEvidence, MovePass, ReasonCode
from src.shared.move_reasons import render_trace_summary
from src.shared.query_trace import build_query_trace
from src.shared.workload_fit_labels import (
    PINNED_UTILITY,
    PINNED_UTILITY_LABEL,
    pinned_label,
    query_fit_text,
)
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]


def _pin(ledger: move_ledger.MoveLedger, qid: str = "q1", score: float = 0.0):
    """The first assignment's utility pin of ``qid`` onto Aurora PostgreSQL."""
    return move_ledger.apply(
        ledger,
        qid,
        "aurora_postgresql",
        pass_=MovePass.UTILITY_PIN,
        reason_code=ReasonCode.OVERRIDE_UTILITY,
        score_after=score,
        evidence=MoveEvidence(candidates=[CandidateScore(engine="aurora_postgresql", score=score)]),
    )


def test_the_label_is_one_wording_in_python_and_the_ui() -> None:
    assert PINNED_UTILITY_LABEL == "pinned (utility)"
    en = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
    assert en["common.fit.pinned-utility"] == PINNED_UTILITY_LABEL


def test_a_pinned_query_shows_the_label_and_a_scored_one_its_percentage() -> None:
    assert query_fit_text(0, PINNED_UTILITY) == "pinned (utility)"
    assert query_fit_text(67, PINNED_UTILITY) == "pinned (utility)"
    assert query_fit_text(78, None) == "78%"
    assert query_fit_text(0, None) == "0%"
    assert query_fit_text(None, None) == "-"
    assert pinned_label(None) is None


def test_the_ledger_records_the_pin_and_keeps_the_real_score() -> None:
    ledger = move_ledger.MoveLedger()
    _pin(ledger, score=0.0)
    assert ledger.pinned_of("q1") == "utility"
    assert ledger.confidence_of("q1") == 0


def test_a_scored_move_records_no_pin() -> None:
    ledger = move_ledger.MoveLedger()
    move_ledger.apply(
        ledger,
        "q1",
        "dynamodb",
        pass_=MovePass.SCORE,
        reason_code=ReasonCode.HIGHEST_SCORE,
        score_after=78.0,
    )
    assert ledger.pinned_of("q1") is None


def test_an_engine_change_unpins_and_a_tie_only_move_keeps_the_pin() -> None:
    ledger = move_ledger.MoveLedger()
    _pin(ledger)
    qa = {
        "query_id": "q1",
        "assigned_engine": "aurora_postgresql",
        "confidence": 0,
        "pinned": "utility",
        "tied_engines": [],
    }
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "aurora_postgresql",
        pass_=MovePass.PLACE,
        reason_code=ReasonCode.TIE_CLEARED,
        tied_engines=[],
    )
    assert qa["pinned"] == "utility"
    move_ledger.apply_to_assignment(
        ledger,
        qa,
        "dynamodb",
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        score_after=70.0,
        tied_engines=[],
    )
    assert qa["pinned"] is None
    assert qa["confidence"] == 70


def test_the_trace_carries_the_pin_and_its_summary_says_it() -> None:
    ledger = move_ledger.MoveLedger()
    _pin(ledger, score=43.0)
    trace = build_query_trace(
        "q1", [(1, m) for m in ledger.moves], "aurora_postgresql", {"aurora_postgresql"}
    )
    assert trace.pinned == "utility"
    assert trace.final_score == 43.0
    assert "pinned (utility)" in trace.summary
    assert "(43)" not in trace.summary


def test_a_pinned_summary_says_the_pin_and_a_scored_one_its_score() -> None:
    pinned = render_trace_summary("aurora_mysql", 43.0, 1, None, pinned=PINNED_UTILITY)
    assert pinned == "Ends on Aurora MySQL, pinned (utility), after 1 routing step."
    scored = render_trace_summary("dynamodb", 78.0, 1, None)
    assert scored == "Ends on DynamoDB (78) after 1 routing step."


def test_the_report_appendix_shows_the_pin_not_the_score() -> None:
    from src.report.query_traces import _trace_md

    ledger = move_ledger.MoveLedger()
    _pin(ledger, score=43.0)
    trace = build_query_trace(
        "q1", [(1, m) for m in ledger.moves], "aurora_postgresql", {"aurora_postgresql"}
    ).model_dump(mode="json", by_alias=True)
    text = "\n".join(_trace_md(trace, {}))
    assert "- Final engine: Aurora PostgreSQL, pinned (utility)" in text
    assert "(43)" not in text.split("- Final engine:")[1].split("\n")[0]


# --- a version saved before ``pinned`` existed ------------------------------------


def _saved_without_the_field() -> tuple[dict, dict]:
    ledger = move_ledger.MoveLedger()
    _pin(ledger, "q1", 0.0)
    move_ledger.apply(
        ledger,
        "q2",
        "dynamodb",
        pass_=MovePass.SCORE,
        reason_code=ReasonCode.HIGHEST_SCORE,
        score_after=78.0,
    )
    qas = [
        QueryAssignment(
            query_id="q1",
            assigned_engine="aurora_postgresql",
            confidence=100,
            source_tables=["pg_index"],
            assignment_reason="utility/metadata",
        ),
        QueryAssignment(
            query_id="q2",
            assigned_engine="dynamodb",
            confidence=78,
            source_tables=["t"],
            assignment_reason="highest fit",
        ),
    ]
    v1 = Assignment(
        job_id="job",
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
        moves=ledger.moves,
    ).model_dump(mode="json")
    for qa in v1["query_assignments"]:
        qa.pop("pinned", None)
    v2 = {**json.loads(json.dumps(v1)), "version": 2, "previous_version": 1, "moves": []}
    return v1, v2


def test_an_old_version_without_the_field_loads_with_its_pins() -> None:
    v1, v2 = _saved_without_the_field()
    assert QueryAssignment.model_validate(v2["query_assignments"][0]).pinned is None
    assert move_ledger.pins_from_moves(v2, [v1, v2]) == ["q1"]
    pins = {qa["query_id"]: qa["pinned"] for qa in v2["query_assignments"]}
    assert pins == {"q1": "utility", "q2": None}


def test_the_edit_surface_labels_an_old_versions_pins(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    v1, v2 = _saved_without_the_field()
    store.write_json("db/job/assignment/v1/assignment.json", v1)
    store.write_json("db/job/assignment/v2/assignment.json", v2)
    data, _, _, _ = load_assignment_for_edit(store, "db", "job", 2)
    by_id = {qa["query_id"]: qa for qa in data["query_assignments"]}
    assert by_id["q1"]["pinned"] == "utility"
    # Its confidence is its real score (0), not the old fixed 100.
    assert by_id["q1"]["confidence"] == 0
    assert by_id["q2"]["pinned"] is None
    assert (
        "pinned"
        not in store.read_json("db/job/assignment/v2/assignment.json")["query_assignments"][0]
    )


# --- the mean fit -----------------------------------------------------------------


def test_the_mean_fit_leaves_pins_out_and_counts_them() -> None:
    from src.agents.referee.routed_confidence import routed_fits

    queries = [
        {
            "query_id": "q1",
            "query_text": "SHOW TABLES",
            "query_type": "SHOW",
            "tables_accessed": [],
        },
        {
            "query_id": "q2",
            "query_text": "SELECT * FROM t WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": ["db.t"],
        },
    ]
    analysis = {
        "table_recommendations": [{"table_id": "db.t", "confidence_score": 80}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }
    base = {"query_id": "", "assigned_engine": "aurora_postgresql", "in_scope": True}
    with_pin = {
        "query_assignments": [
            {**base, "query_id": "q1", "pinned": "utility"},
            {**base, "query_id": "q2"},
        ]
    }
    only_scored = {"query_assignments": [{**base, "query_id": "q2"}]}
    fits = routed_fits(with_pin, {}, queries, {"aurora_postgresql": analysis})
    alone = routed_fits(only_scored, {}, queries, {"aurora_postgresql": analysis})
    fit = fits["aurora_postgresql"]
    assert fit.pinned_queries == 1
    assert fit.queries == 1
    assert fit.confidence == alone["aurora_postgresql"].confidence


def test_an_engine_holding_only_pins_shows_no_average() -> None:
    """Every query routed to the engine is a utility pin: no routed figure, and the
    analysis average is never shown in its place (#1373)."""
    from src.shared.ranking import confidence_text, fit_text

    only_pins = {
        "target": "aurora_mysql",
        "routed_confidence": None,
        "routed_pinned_queries": 3,
        "confidence_score": 64,
    }
    assert fit_text(only_pins) == "all 3 routed queries pinned (utility)"
    assert confidence_text(only_pins) == "all 3 routed queries pinned (utility)"
    scored = {"target": "dynamodb", "routed_confidence": 86, "routed_pinned_queries": 0}
    assert fit_text(scored) == "86% fit"
    assert fit_text(scored, "workload fit") == "86% workload fit"
