"""Queries on a fallback engine (#729).

A query whose engine was decided by a fallback rule (``fallback.no_score``,
``fallback.no_owner``, ``fallback.no_group_owner``: no analyzed engine could
take it) or whose every candidate engine is gated sits on an engine that cannot
run it as written. The maintainer decision (2026-10-14) is not to revive a
removed engine but to flag the query with the gates that ruled each engine out,
a redesign suggestion and the existing way to re-run. Synthesis records them
in ``query_traces.unservable`` (selected with ``report_facts.is_unservable``
from the query's own decision trace) and the facts layer states them once for
every deliverable (ADR-032).
"""

from __future__ import annotations

import re
from typing import Any

from src.contracts.move_ledger import (
    CandidateScore,
    GroupGate,
    Move,
    MoveEvidence,
    MovePass,
    ReasonCode,
)
from src.contracts.synthesis_output import ReportFacts
from src.shared.capability_labels import (
    CAPABILITY_DISPLAY_NAMES,
    CAPABILITY_REDESIGN_HINTS,
    GATE_REDESIGN_HINTS,
    GENERIC_REDESIGN_HINT,
    redesign_hints,
)
from src.shared.query_trace import build_query_trace, query_trace_digest
from src.shared.report_facts import (
    UNSERVABLE_SHOWN,
    compute_report_facts,
    is_unservable,
    unservable_query_facts,
)


def _move(qid: str, code: ReasonCode, to: str, candidates: list[CandidateScore], **kw: Any) -> Move:
    return Move(
        seq=kw.pop("seq", 1),
        query_id=qid,
        pass_=kw.pop("pass_", MovePass.AURORA_FALLBACK),
        to_engine=to,
        reason_code=code,
        evidence=MoveEvidence(candidates=candidates, group_gates=kw.pop("group_gates", [])),
        **kw,
    )


GATED = [
    CandidateScore(engine="dynamodb", score=None, gate="capability"),
    CandidateScore(engine="documentdb", score=None, gate="capability"),
]


def _trace(moves: list[Move], final: str, plan: set[str]) -> dict[str, Any]:
    trace = build_query_trace(moves[0].query_id, [(1, m) for m in moves], final, plan)
    return trace.model_dump(mode="json", by_alias=True)


class TestIsUnservable:
    def test_a_fallback_rule_is_unservable(self) -> None:
        for code in (
            ReasonCode.FALLBACK_NO_SCORE,
            ReasonCode.FALLBACK_NO_OWNER,
            ReasonCode.FALLBACK_NO_GROUP_OWNER,
        ):
            move = _move("q1", code, "aurora_mysql", GATED)
            assert is_unservable(_trace([move], "aurora_mysql", {"aurora_mysql", "dynamodb"}))

    def test_every_candidate_gated_is_unservable_even_without_a_fallback(self) -> None:
        gated = [*GATED, CandidateScore(engine="aurora_mysql", score=70.0, gate="model_flag")]
        move = _move("q1", ReasonCode.HIGHEST_SCORE, "aurora_mysql", gated, pass_=MovePass.SCORE)
        assert is_unservable(_trace([move], "aurora_mysql", {"aurora_mysql"}))

    def test_a_query_an_engine_can_serve_is_not(self) -> None:
        ladder = [*GATED, CandidateScore(engine="aurora_mysql", score=80.0)]
        move = _move("q1", ReasonCode.HIGHEST_SCORE, "aurora_mysql", ladder, pass_=MovePass.SCORE)
        assert not is_unservable(_trace([move], "aurora_mysql", {"aurora_mysql", "dynamodb"}))

    def test_a_later_move_onto_a_serving_engine_clears_it(self) -> None:
        fallback = _move("q1", ReasonCode.FALLBACK_NO_SCORE, "aurora", GATED)
        pick = _move(
            "q1",
            ReasonCode.CUSTOMER_PICK,
            "aurora_mysql",
            [*GATED, CandidateScore(engine="aurora_mysql", score=75.0)],
            seq=2,
            pass_=MovePass.CUSTOMER_PICK,
            from_engine="aurora",
        )
        assert not is_unservable(_trace([fallback, pick], "aurora_mysql", {"aurora_mysql"}))

    def test_no_ledger_is_not(self) -> None:
        assert not is_unservable({"query_id": "q1", "has_ledger": False})


def _assignment(moves: list[Move], engines: dict[str, str]) -> dict[str, Any]:
    return {
        "version": 1,
        "previous_version": None,
        "query_assignments": [
            {"query_id": q, "assigned_engine": e, "source_tables": ["db.t"]}
            for q, e in engines.items()
        ],
        "moves": [m.model_dump(mode="json", by_alias=True) for m in moves],
    }


def _digest_for(moves: list[Move], engines: dict[str, str], caps: dict | None = None):
    a = _assignment(moves, engines)
    return query_trace_digest(a, {1: a}.get, 1, None, caps)


def test_the_digest_records_the_gates_and_capabilities_of_each_one() -> None:
    fb = _move("q-count", ReasonCode.FALLBACK_NO_SCORE, "aurora_mysql", GATED)
    ok = _move(
        "q-ok",
        ReasonCode.HIGHEST_SCORE,
        "dynamodb",
        [CandidateScore(engine="dynamodb", score=90.0)],
        seq=2,
        pass_=MovePass.SCORE,
    )
    digest = _digest_for(
        [fb, ok],
        {"q-count": "aurora_mysql", "q-ok": "dynamodb"},
        {"q-count": ["aggregation"], "q-ok": []},
    )
    assert [u.query_id for u in digest.unservable] == ["q-count"]
    row = digest.unservable[0]
    assert row.engine == "aurora_mysql"
    assert row.kind == "fallback"
    assert row.rule == "fallback.no_score"
    assert row.explanation.startswith("No engine could score this query")
    assert [(g.engine, g.gate) for g in row.blocking_gates] == [
        ("documentdb", "capability"),
        ("dynamodb", "capability"),
    ]
    assert all(g.gate_text and g.engine_name for g in row.blocking_gates)
    assert row.required_capabilities == ["aggregation"]


def test_a_group_fallback_names_the_group_gates() -> None:
    fb = _move(
        "q1",
        ReasonCode.FALLBACK_NO_GROUP_OWNER,
        "aurora_postgresql",
        [],
        pass_=MovePass.CO_DEPENDENCY,
        group_gates=[
            GroupGate(engine="dynamodb", query_id="q2", gate="capability"),
            GroupGate(engine="opensearch", query_id="q1", gate="write_gate"),
        ],
    )
    digest = _digest_for([fb], {"q1": "aurora_postgresql"})
    assert [(g.engine, g.gate) for g in digest.unservable[0].blocking_gates] == [
        ("dynamodb", "capability"),
        ("opensearch", "write_gate"),
    ]


def _row(qid: str, engine: str = "aurora_mysql", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "query_id": qid,
        "engine": engine,
        "engine_name": "Aurora MySQL",
        "kind": "fallback",
        "rule": "fallback.no_score",
        "rule_label": "no engine could score it",
        "explanation": "No engine could score this query, so it falls back to Aurora MySQL.",
        "blocking_gates": [
            {
                "engine": "dynamodb",
                "engine_name": "DynamoDB",
                "gate": "capability",
                "gate_text": "missing a capability the query needs",
            }
        ],
        "required_capabilities": ["aggregation"],
    }
    row.update(kw)
    return row


def _report(rows: list[dict] | None) -> dict[str, Any]:
    digest: dict[str, Any] = {"has_ledger": True, "query_count": 3, "queries": []}
    if rows is not None:
        digest["unservable"] = rows
    return {"query_traces": digest}


class TestUnservableFacts:
    def test_states_each_query_with_its_gates_and_a_redesign(self) -> None:
        facts = unservable_query_facts(
            _report(
                [
                    _row("q2"),
                    _row("q1", kind="gated_everywhere", rule="highest_score"),
                ]
            )
        )
        assert facts is not None
        assert facts["unservable_query_count"] == 2
        assert facts["fallback_query_count"] == 1
        assert facts["gated_everywhere_query_count"] == 1
        assert [q["query_id"] for q in facts["unservable_queries"]] == ["q1", "q2"]
        first = facts["unservable_queries"][0]
        assert first["blocking_gates"][0]["gate_text"] == "missing a capability the query needs"
        # The hint is for the engine that ruled it out, for the capability it lacks.
        assert first["redesign_hints"] == [
            "For DynamoDB: " + CAPABILITY_REDESIGN_HINTS["aggregation"]["dynamodb"]
        ]
        assert facts["more_unservable_count"] == 0

    def test_without_capabilities_the_gates_suggest_the_redesign(self) -> None:
        row = _row("q1", required_capabilities=[])
        row["blocking_gates"][0]["gate"] = "write_gate"
        facts = unservable_query_facts(_report([row]))
        assert facts["unservable_queries"][0]["redesign_hints"] == [  # type: ignore[index]
            GATE_REDESIGN_HINTS["write_gate"]
        ]

    def test_names_at_most_the_cap(self) -> None:
        rows = [_row(f"q{i:03d}") for i in range(UNSERVABLE_SHOWN + 4)]
        facts = unservable_query_facts(_report(rows))
        assert facts["unservable_query_count"] == UNSERVABLE_SHOWN + 4  # type: ignore[index]
        assert len(facts["unservable_queries"]) == UNSERVABLE_SHOWN  # type: ignore[index]
        assert facts["more_unservable_count"] == 4  # type: ignore[index]

    def test_none_for_a_report_written_before_them(self) -> None:
        assert unservable_query_facts(_report(None)) is None
        assert unservable_query_facts({}) is None

    def test_the_whole_facts_block_carries_them_and_validates(self) -> None:
        report = {
            "ranking": [{"target": "aurora_mysql", "workload_percent": 100.0}],
            **_report([_row("q1")]),
        }
        facts = compute_report_facts(report)
        assert facts["unservable"]["unservable_query_count"] == 1
        ReportFacts.model_validate(facts)


class TestRedesignHints:
    def test_every_engine_that_lacks_a_capability_has_a_hint(self) -> None:
        """One hint per capability and per engine the capability registry says
        lacks it, and none for an engine that has it."""
        from src.agents.referee.capability_registry import ENGINE_CAPABILITIES

        for cap in CAPABILITY_DISPLAY_NAMES:
            lacking = {e for e, caps in ENGINE_CAPABILITIES.items() if cap not in caps}
            assert set(CAPABILITY_REDESIGN_HINTS.get(cap, {})) == lacking, cap

    def test_every_gate_kind_has_a_hint(self) -> None:
        from src.shared.move_reasons import GATE_TEXT_KINDS

        assert set(GATE_REDESIGN_HINTS) == set(GATE_TEXT_KINDS)

    def test_hints_name_only_the_engines_the_capability_gate_ruled_out(self) -> None:
        gates = [
            ("dynamodb", "capability"),
            ("documentdb", "capability"),
            ("opensearch", "write_gate"),
        ]
        hints = redesign_hints(["complex_joins", "aggregation"], gates)
        assert hints == [
            # DocumentDB has aggregation: no aggregation advice for it.
            "For DynamoDB: " + CAPABILITY_REDESIGN_HINTS["aggregation"]["dynamodb"],
            "For DocumentDB: " + CAPABILITY_REDESIGN_HINTS["complex_joins"]["documentdb"],
            "For DynamoDB: " + CAPABILITY_REDESIGN_HINTS["complex_joins"]["dynamodb"],
        ]

    def test_engines_with_the_same_advice_are_named_together(self) -> None:
        hints = redesign_hints(["sql_admin"], [("dynamodb", "capability")])
        assert hints[0].startswith("For DynamoDB: this is session, transaction or schema traffic")
        assert "TransactWriteItems" in hints[0] and "LOCK" in hints[0] and "SET" in hints[0]

    def test_without_a_capability_hint_the_gates_suggest_it_once_each(self) -> None:
        gates = [("dynamodb", "model_flag"), ("opensearch", "write_gate"), ("x", "model_flag")]
        assert redesign_hints([], gates) == [
            GATE_REDESIGN_HINTS["model_flag"],
            GATE_REDESIGN_HINTS["write_gate"],
        ]

    def test_a_generic_hint_when_nothing_is_known(self) -> None:
        assert redesign_hints([], []) == [GENERIC_REDESIGN_HINT]

    def test_hints_never_suggest_moving_to_a_kept_engine(self) -> None:
        texts = [
            *(t for by_engine in CAPABILITY_REDESIGN_HINTS.values() for t in by_engine.values()),
            *GATE_REDESIGN_HINTS.values(),
            GENERIC_REDESIGN_HINT,
        ]
        for text in texts:
            assert text.endswith(".")
            assert "kept engine" not in text and "move it" not in text.lower()
            assert not re.search(r"\b[a-z]+_[a-z]+\b", text), text
