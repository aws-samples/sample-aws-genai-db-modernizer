"""The one renderer of a Move's reason (#339): plain English, display names, no codes."""

from __future__ import annotations

import re

import pytest

from src.agents.referee.query_scoring import GATE_KIND_HARD_EXCLUSION, GATE_KINDS
from src.contracts.move_ledger import (
    CandidateScore,
    EngineScore,
    Move,
    MoveActor,
    MoveEvidence,
    MovePass,
    ReasonCode,
)
from src.shared.move_reasons import (
    GATE_TEXT_KINDS,
    GROUP_OWNER_GATE,
    WRITE_GATE,
    format_score,
    gate_text,
    render_move_reason,
    render_outscored,
    render_runner_up,
    render_trace_summary,
    rule_label,
)

_CODES = {c.value for c in ReasonCode} | {p.value for p in MovePass}


def _move(**kw: object) -> Move:
    base: dict[str, object] = {
        "seq": 1,
        "query_id": "q1",
        "pass_": MovePass.SCORE,
        "to_engine": "dynamodb",
        "reason_code": ReasonCode.HIGHEST_SCORE,
    }
    base.update(kw)
    return Move.model_validate(base)


def _assert_public(sentence: str) -> None:
    """No reason code, pass id or engine id; a sentence that ends with a full stop."""
    assert sentence and sentence.endswith(".")
    for code in _CODES:
        if "." in code or "_" in code:
            assert code not in sentence, (code, sentence)
    for engine_id in ("aurora_mysql", "aurora_postgresql", "dynamodb", "documentdb", "opensearch"):
        assert engine_id not in sentence, (engine_id, sentence)  # display names only
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", sentence), sentence


# Every reason code, as a first assignment, an engine change and a same-engine Move.
@pytest.mark.parametrize("code", list(ReasonCode))
@pytest.mark.parametrize("from_engine", [None, "documentdb", "dynamodb"])
def test_every_reason_code_renders_public_wording(code, from_engine):
    move = _move(
        reason_code=code,
        from_engine=from_engine,
        score_before=None if from_engine is None else 87.0,
        score_after=82.0,
        runner_up=EngineScore(engine="aurora_mysql", score=54.0),
        evidence=MoveEvidence(
            candidates=[
                CandidateScore(engine="aurora_mysql", score=54.0),
                CandidateScore(engine="dynamodb", score=82.0),
                CandidateScore(engine="elasticache", gate="capability"),
            ],
            previous_tied_engines=["documentdb", "dynamodb"],
        ),
    )
    _assert_public(render_move_reason(move))


def test_first_assignment_names_the_winner_and_runner_up():
    move = _move(score_after=95.0, runner_up=EngineScore(engine="documentdb", score=86.0))
    assert render_move_reason(move) == (
        "DynamoDB scored highest for this query (95), ahead of DocumentDB (86)."
    )


def test_reality_check_consolidation_from_the_wordpress_sample():
    # wordpress, the REPLACE INTO wp_wc_product_meta_lookup query (assignment v2).
    move = _move(
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        from_engine="documentdb",
        to_engine="dynamodb",
        score_before=77.0,
        score_after=69.0,
        runner_up=EngineScore(engine="aurora_mysql", score=54.0),
    )
    sentence = render_move_reason(move)
    assert sentence.startswith("Moved from DocumentDB to DynamoDB (77 to 69): ")
    assert "the reality check removed DocumentDB" in sentence


def test_a_deciding_tie_in_evidence_is_named():
    move = _move(
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        from_engine="documentdb",
        to_engine="aurora_mysql",
        score_before=80.0,
        score_after=70.0,
        evidence=MoveEvidence(tie_rule=ReasonCode.TIE_SOURCE_COMPATIBLE),
    )
    assert "compatible with the source database" in render_move_reason(move)


def test_an_unresolved_tie_names_its_partners_and_the_placeholder():
    move = _move(
        pass_=MovePass.TIE,
        reason_code=ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD,
        score_after=87.0,
        tied_engines=["documentdb", "dynamodb"],
    )
    assert render_move_reason(move) == (
        "DynamoDB tied with DocumentDB at 87; the engine already serving more of the "
        "settled workload holds it until the customer chooses."
    )


def test_a_cleared_tie_names_the_earlier_partners():
    move = _move(
        pass_=MovePass.PLACE,
        reason_code=ReasonCode.TIE_CLEARED,
        from_engine="dynamodb",
        score_before=87.0,
        score_after=87.0,
        evidence=MoveEvidence(previous_tied_engines=["documentdb", "dynamodb"]),
    )
    assert render_move_reason(move) == (
        "The tie with DocumentDB is settled: the query stays on DynamoDB."
    )


def test_a_customer_cache_request_says_so():
    move = _move(
        pass_=MovePass.CACHE_OWNER_NORMALIZE,
        reason_code=ReasonCode.CACHE_LEGACY_OWNER,
        from_engine="elasticache",
        to_engine="aurora_mysql",
        actor=MoveActor.CUSTOMER,
    )
    assert "the customer asked to cache it" in render_move_reason(move)


@pytest.mark.parametrize("gate", [*GATE_KINDS, WRITE_GATE, GROUP_OWNER_GATE])
def test_every_gate_has_plain_text(gate):
    text = gate_text(gate)
    assert text and "_" not in text
    assert text != gate_text("a gate kind nobody emits")  # its own wording, not the fallback
    _assert_public(render_runner_up("aurora_mysql", 82.0, "dynamodb", None, gate=gate))


def test_every_gate_kind_the_scorer_emits_has_wording():
    # Completeness: a new scorer gate kind must get its own wording here.
    assert GATE_TEXT_KINDS == {*GATE_KINDS, WRITE_GATE, GROUP_OWNER_GATE}
    assert GATE_KIND_HARD_EXCLUSION in GATE_TEXT_KINDS


def test_no_gate_no_text():
    assert gate_text(None) is None


@pytest.mark.parametrize("code", list(ReasonCode))
def test_every_rule_has_a_label(code):
    assert rule_label(code) != "routing rule"
    assert "_" not in rule_label(code)


def test_runner_up_gap_ahead_and_tied():
    assert render_runner_up("dynamodb", 69.0, "aurora_mysql", 54.0) == (
        "Aurora MySQL was next at 54, 15 points behind DynamoDB (69)."
    )
    assert render_runner_up("dynamodb", 87.0, "documentdb", 87.0) == (
        "DocumentDB tied with DynamoDB at 87."
    )
    assert render_runner_up("aurora_mysql", 81.9, "dynamodb", 80.0, group=True) == (
        "DynamoDB was next at 80 (group mean), 1.9 points behind Aurora MySQL (81.9)."
    )


def test_a_settled_tie_names_its_rule():
    assert (
        render_runner_up("dynamodb", 87.0, "documentdb", 87.0, tie_rule=ReasonCode.TIE_CLEARED)
        == "DocumentDB tied with DynamoDB at 87; a later routing step settled the tie."
    )
    assert render_runner_up(
        "aurora_mysql", 79.0, "dynamodb", 79.0, tie_rule=ReasonCode.TIE_SOURCE_COMPATIBLE
    ) == (
        "DynamoDB tied with Aurora MySQL at 79; the tie went to the engine compatible with "
        "the source database."
    )


def test_outscored_names_the_step_that_decided():
    settle = _move(
        pass_=MovePass.SETTLE,
        reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        from_engine="documentdb",
        to_engine="aurora_mysql",
    )
    assert render_outscored(
        "aurora_mysql", 82.0, "documentdb", 87.0, settle, 2, left_it=True, in_plan=False
    ) == (
        "DocumentDB scored higher (87 against 82), but the query left it in version 2 "
        "(engine consolidated away), and it is not part of the final plan."
    )
    pick = _move(
        pass_=MovePass.CUSTOMER_PICK,
        reason_code=ReasonCode.CUSTOMER_PICK,
        from_engine="dynamodb",
        to_engine="aurora_mysql",
        actor=MoveActor.CUSTOMER,
    )
    assert render_outscored(
        "aurora_mysql", 54.0, "dynamodb", 69.0, pick, 3, left_it=True, in_plan=True
    ) == (
        "DynamoDB scored higher (69 against 54), but the query left it in version 3 "
        "(customer choice)."
    )


def test_rollback_summary_says_the_flag_still_applies():
    rollback = _move(
        pass_=MovePass.POST_SCHEMA_FLAG,
        reason_code=ReasonCode.ROLLBACK_REDESIGN_FAILED,
        from_engine="aurora_mysql",
        to_engine="dynamodb",
    )
    assert render_trace_summary("dynamodb", None, 3, "ignored", rollback) == (
        "Ends on DynamoDB after 3 routing steps: the reroute to Aurora MySQL was rolled back "
        "because its redesign failed, and the design review's flag on DynamoDB still applies."
    )


def test_scores_format_without_noise():
    assert format_score(82.0) == "82"
    assert format_score(81.94) == "81.9"
    assert format_score(None) == "n/a"


def test_summary_line():
    assert render_trace_summary("dynamodb", 69.0, 2, "Aurora MySQL was next at 54.") == (
        "Ends on DynamoDB (69) after 2 routing steps. Aurora MySQL was next at 54."
    )
    assert render_trace_summary("dynamodb", None, 1, None) == (
        "Ends on DynamoDB after 1 routing step."
    )


def _auto_pick(from_engine: str, rule: str) -> Move:
    return _move(
        pass_=MovePass.AUTO_PICK,
        reason_code=ReasonCode.TIE_DECIDED_FOR_YOU,
        from_engine=from_engine,
        to_engine="documentdb",
        score_before=78.0,
        score_after=78.0,
        runner_up=EngineScore(engine="dynamodb", score=78.0),
        evidence=MoveEvidence(
            choice_id="app.incoming_emails",
            choice_scores={"documentdb": 78.0, "dynamodb": 78.0},
            choice_unrounded_scores={"documentdb": 78.2, "dynamodb": 78.15},
            choice_rule=rule,
        ),
    )


@pytest.mark.parametrize("from_engine", ["dynamodb", "documentdb"])
def test_a_decided_for_you_move_names_the_tie_the_rule_and_both_unrounded_scores(
    from_engine: str,
) -> None:
    """#1003: --auto's pick reads as decided for the customer, with what decided it."""
    sentence = render_move_reason(_auto_pick(from_engine, "unrounded_score"))
    _assert_public(sentence)
    assert "decided automatically" in sentence and "--auto" not in sentence
    assert "it scores higher before rounding (DocumentDB 78.20, DynamoDB 78.15)" in sentence
    assert "tied with DynamoDB" in sentence and "review if needed" in sentence
    workload = render_move_reason(_auto_pick(from_engine, "settled_workload"))
    assert "it already serves more of the plan" in workload


def test_query_traces_count_a_decided_for_you_tie_as_settled() -> None:
    from src.shared.query_trace import _latest_tie_rule

    assert _latest_tie_rule([(3, _auto_pick("dynamodb", "unrounded_score"))]) == (
        ReasonCode.TIE_DECIDED_FOR_YOU
    )


@pytest.mark.parametrize(
    "code", [ReasonCode.WRITE_GROUP_CUSTOMER_PROPAGATION, ReasonCode.GROUP_CUSTOMER_PROPAGATION]
)
def test_a_group_pulled_by_an_auto_pick_never_credits_the_customer(code: ReasonCode) -> None:
    """#1003: a group pulled by the decide-for-me rule follows the rule, not a customer."""
    move = _move(
        pass_=MovePass.CUSTOMER_GROUP_PROPAGATION,
        reason_code=code,
        from_engine="dynamodb",
        to_engine="documentdb",
        actor=MoveActor.RULE,
    )
    sentence = render_move_reason(move)
    _assert_public(sentence)
    assert "customer" not in sentence
    customer = render_move_reason(move.model_copy(update={"actor": MoveActor.CUSTOMER}))
    assert "customer" in customer


def _utility_pin(from_engine: str | None, source_compatible: bool | None) -> Move:
    evidence = {} if source_compatible is None else {"source_compatible": source_compatible}
    return _move(
        pass_=MovePass.UTILITY_PIN,
        from_engine=from_engine,
        to_engine="aurora_postgresql",
        reason_code=ReasonCode.OVERRIDE_UTILITY,
        evidence=evidence,
    )


# A first assignment, an engine change and a same-engine pin all share the clause (#1292).
@pytest.mark.parametrize("from_engine", [None, "dynamodb", "aurora_postgresql"])
class TestUtilityPinWording:
    @pytest.mark.passes_on_main  # what a homogeneous source must keep saying
    def test_a_source_compatible_pin_keeps_the_claim(self, from_engine):
        sentence = render_move_reason(_utility_pin(from_engine, True))
        assert "source-compatible relational engine" in sentence
        _assert_public(sentence)

    def test_a_heterogeneous_pin_names_the_chosen_engine_and_never_the_claim(self, from_engine):
        sentence = render_move_reason(_utility_pin(from_engine, False))
        assert "Aurora PostgreSQL" in sentence
        assert "source-compatible" not in sentence
        assert "chosen relational engine" in sentence
        _assert_public(sentence)

    def test_a_record_that_did_not_keep_the_fact_makes_no_claim(self, from_engine):
        """Saved ledgers written before the field existed still load and render."""
        move = _utility_pin(from_engine, None)
        assert move.evidence.source_compatible is None
        assert "source-compatible" not in render_move_reason(move)
