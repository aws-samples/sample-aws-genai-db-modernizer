"""Close calls: queries whose runner-up is a kept engine a few points behind (#730).

The reality check keeps deciding for the customer (ADR-031 Decision 7), but a
query whose final engine beats another kept engine by less than
``CLOSE_CALL_MARGIN`` points could just as well run there. Synthesis records
those queries in ``query_traces.close_calls`` (selected with
``report_facts.is_close_call`` from the same runner-up the "Why this engine"
trace shows) and the facts layer groups them by engine and alternative, once,
for every deliverable (ADR-032).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts
from src.shared.query_trace import query_trace_digest
from src.shared.report_facts import (
    CLOSE_CALL_IDS_SHOWN,
    CLOSE_CALL_MARGIN,
    close_call_facts,
    compute_report_facts,
    is_close_call,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wordpress_move_trace.json"


def _trace(gap: float | None, gate: str | None = None, tied: list[str] | None = None) -> dict:
    final = 80.0
    runner = {
        "engine": "aurora_mysql",
        "engine_name": "Aurora MySQL",
        "score": None if gap is None else final - gap,
        "final_score": final,
        "basis": "query",
        "gap": gap,
        "gate": gate,
        "explanation": "x",
    }
    return {
        "query_id": "q1",
        "final_engine": "dynamodb",
        "final_score": final,
        "tied_engines": tied or [],
        "runner_up": runner,
    }


class TestIsCloseCall:
    def test_a_runner_up_inside_the_margin_is_a_close_call(self) -> None:
        assert is_close_call(_trace(1.0))
        assert is_close_call(_trace(CLOSE_CALL_MARGIN - 0.1))

    def test_the_margin_itself_is_not_a_close_call(self) -> None:
        assert not is_close_call(_trace(CLOSE_CALL_MARGIN))
        assert not is_close_call(_trace(30.0))

    def test_an_exact_tie_a_rule_settled_is_a_close_call(self) -> None:
        assert is_close_call(_trace(0.0))

    def test_an_open_tie_is_a_pending_choice_not_a_close_call(self) -> None:
        # Listed under "Pending engine choices" already.
        assert not is_close_call(_trace(0.0, tied=["dynamodb", "aurora_mysql"]))

    def test_a_gated_runner_up_cannot_take_the_query(self) -> None:
        assert not is_close_call(_trace(None, gate="capability"))
        assert not is_close_call({"query_id": "q1", "runner_up": None})

    def test_the_margin_is_five_points(self) -> None:
        # Chosen from the three samples' runner-up gaps (#730): see the constant.
        assert CLOSE_CALL_MARGIN == 5.0


def _row(
    qid: str, engine: str, alt: str, gap: float, basis: str = "query", tie: bool = False
) -> dict:
    return {
        "settled_tie": tie,
        "query_id": qid,
        "engine": engine,
        "alternative": alt,
        "score": 80.0,
        "alternative_score": 80.0 - gap,
        "gap": gap,
        "basis": basis,
    }


def _report(rows: list[dict] | None) -> dict[str, Any]:
    digest: dict[str, Any] = {"has_ledger": True, "query_count": 10, "queries": []}
    if rows is not None:
        digest["close_calls"] = rows
    return {"query_traces": digest}


class TestCloseCallFacts:
    def test_groups_by_engine_and_alternative_with_the_settled_ties_apart(self) -> None:
        rows = [
            _row("q3", "dynamodb", "aurora_mysql", 3.0),
            _row("q1", "dynamodb", "aurora_mysql", 0.0, tie=True),
            _row("q2", "dynamodb", "aurora_mysql", 1.5, basis="group_mean"),
            _row("q9", "aurora_mysql", "dynamodb", 4.0),
            _row("q8", "aurora_mysql", "documentdb", 0.0, tie=True),
        ]
        facts = close_call_facts(_report(rows))
        assert facts is not None
        assert facts["margin_points"] == CLOSE_CALL_MARGIN
        # The headline counts the real close calls; the settled ties are apart.
        assert facts["close_call_query_count"] == 3
        assert facts["settled_tie_count"] == 2
        first, second, third = facts["engine_pairs"]
        assert (first["engine"], first["alternative"]) == ("dynamodb", "aurora_mysql")
        assert first["pair_query_count"] == 2
        assert first["exact_tie_count"] == 1
        assert first["group_mean_count"] == 1
        # The gap range is the close calls' own, never the tie's 0.
        assert (first["min_gap"], first["max_gap"]) == (1.5, 3.0)
        # Closest close calls first, then the settled ties.
        assert first["query_ids"] == ["q2", "q3", "q1"]
        assert first["more_query_count"] == 0
        assert (second["engine"], second["pair_query_count"]) == ("aurora_mysql", 1)
        assert (third["alternative"], third["pair_query_count"], third["exact_tie_count"]) == (
            "documentdb",
            0,
            1,
        )
        assert (third["min_gap"], third["max_gap"]) == (0.0, 0.0)

    def test_shows_at_most_the_cap_and_counts_the_rest(self) -> None:
        n = CLOSE_CALL_IDS_SHOWN + 3
        rows = [_row(f"q{i:02d}", "documentdb", "aurora_postgresql", 1.0) for i in range(n)]
        pair = close_call_facts(_report(rows))["engine_pairs"][0]  # type: ignore[index]
        assert pair["pair_query_count"] == n
        assert len(pair["query_ids"]) == CLOSE_CALL_IDS_SHOWN
        assert pair["more_query_count"] == 3

    def test_none_for_a_report_written_before_close_calls(self) -> None:
        assert close_call_facts(_report(None)) is None
        assert close_call_facts({}) is None

    def test_no_close_calls_is_an_empty_block_not_none(self) -> None:
        facts = close_call_facts(_report([]))
        assert facts == {
            "margin_points": CLOSE_CALL_MARGIN,
            "close_call_query_count": 0,
            "settled_tie_count": 0,
            "engine_pairs": [],
        }

    def test_the_whole_facts_block_carries_them_and_validates(self) -> None:
        report = {
            "ranking": [{"target": "dynamodb", "workload_percent": 100.0}],
            **_report([_row("q1", "dynamodb", "aurora_mysql", 2.0)]),
        }
        facts = compute_report_facts(report)
        assert facts["close_calls"]["close_call_query_count"] == 1
        ReportFacts.model_validate(facts)


@pytest.fixture
def wordpress() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def test_the_digest_records_every_close_call_from_its_trace(wordpress) -> None:
    """Every query of the effective assignment is checked, listed or not, and
    each recorded row is the trace's own runner-up."""
    versions = {1: wordpress["v1"], 2: wordpress["v2"]}
    digest = query_trace_digest(wordpress["v2"], versions.get, 2)
    assert digest.close_calls, "the wordpress fixture has close calls"
    for row in digest.close_calls:
        assert 0 <= row.gap < CLOSE_CALL_MARGIN
        assert row.engine != row.alternative
        assert row.score is not None and row.alternative_score is not None
        assert round(row.score - row.alternative_score, 1) == row.gap
    by_id = {t.query_id: t for t in digest.queries}
    for row in digest.close_calls:
        if row.query_id in by_id:
            runner = by_id[row.query_id].runner_up
            assert runner is not None
            assert (runner.engine, runner.gap) == (row.alternative, row.gap)
        # A settled tie is exactly that: equal scores, and a tie rule decided it.
        if row.settled_tie:
            assert row.gap == 0 and row.score == row.alternative_score
    ties = [r for r in digest.close_calls if r.settled_tie]
    assert ties, "the wordpress fixture has ties the source-compatible rule settled"
    ids = [r.query_id for r in digest.close_calls]
    assert len(ids) == len(set(ids))
    # Deterministic order: engine, alternative, gap, query id.
    keys = [(r.engine, r.alternative, r.gap, r.query_id) for r in digest.close_calls]
    assert keys == sorted(keys)


@pytest.mark.parametrize(
    ("code", "runner_score", "settled"),
    [
        ("tie.source_compatible", 80.0, True),
        # A 0.04 gap rounds to 0 but no tie rule decided it: a close call, not a tie.
        ("highest_score", 79.96, False),
    ],
)
def test_a_settled_tie_is_read_off_the_tie_rule_not_a_rounded_gap(
    code: str, runner_score: float, settled: bool
) -> None:
    from src.contracts.move_ledger import (
        CandidateScore,
        Move,
        MoveEvidence,
        MovePass,
        ReasonCode,
    )

    move = Move(
        seq=1,
        query_id="q1",
        pass_=MovePass.SCORE,
        to_engine="aurora_mysql",
        reason_code=ReasonCode(code),
        score_after=80.0,
        evidence=MoveEvidence(
            candidates=[
                CandidateScore(engine="aurora_mysql", score=80.0),
                CandidateScore(engine="dynamodb", score=runner_score),
            ]
        ),
    )
    # dynamodb is in the plan through a second query.
    assignment: dict[str, Any] = {
        "version": 1,
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "aurora_mysql"},
            {"query_id": "q2", "assigned_engine": "dynamodb"},
        ],
        "moves": [move.model_dump(mode="json", by_alias=True)],
    }
    digest = query_trace_digest(assignment, {1: assignment}.get, 1)
    [row] = [r for r in digest.close_calls if r.query_id == "q1"]
    assert row.gap == 0
    assert row.settled_tie is settled
