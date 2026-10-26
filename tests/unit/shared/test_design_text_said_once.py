"""A long sentence two design trade-offs share is stated once (#1574).

A scoped discourse run's DynamoDB design ran as 45 independent parts; two of them
wrote the skill example's "A GSI is eventually consistent: ..." sentence into their
own trade-offs, so report.json, the risk details, the engineering report and the UI
printed it twice. Synthesis now states each long sentence once across the trade-offs
it collects, so every deliverable reads the same deduplicated list.
"""

from __future__ import annotations

import copy

from src.shared.design_text import ALREADY_STATED, said_once_trade_offs

GSI = (
    "A GSI is eventually consistent: a query on it returns a write once DynamoDB has "
    "applied it to the index."
)


def _t(description: str, impact: str = "Impact.", engine: str = "dynamodb") -> dict:
    return {"description": description, "impact": impact, "engine": engine}


def test_a_shared_long_sentence_is_kept_in_the_first_and_dropped_from_later_ones() -> None:
    raw = [
        _t(f"Lookups by a non-key column use GSIs. {GSI} Items missing a key are not indexed."),
        _t(f"Username lookups use GSIs because the base key is user_id. {GSI} Read the base item."),
    ]
    before = copy.deepcopy(raw)
    out = said_once_trade_offs(raw)
    assert raw == before
    assert GSI in out[0]["description"]
    assert GSI not in out[1]["description"]
    assert out[1]["description"] == (
        "Username lookups use GSIs because the base key is user_id. Read the base item."
    )


def test_a_field_left_with_nothing_of_its_own_points_back() -> None:
    out = said_once_trade_offs([_t(f"Lookups use GSIs. {GSI}"), _t("Other.", impact=GSI)])
    assert out[1]["impact"] == ALREADY_STATED
    assert len(ALREADY_STATED.split()) < 15


def test_short_sentences_and_other_engines_texts_are_compared_too_but_short_ones_kept() -> None:
    short = "Reads are eventually consistent."
    out = said_once_trade_offs([_t(f"A. {short}"), _t(f"B. {short}")])
    assert out[1]["description"] == f"B. {short}"
    # The same long sentence under another engine is still a repeat in the report.
    out = said_once_trade_offs([_t(f"X. {GSI}"), _t(f"Y. {GSI}", engine="opensearch")])
    assert GSI not in out[1]["description"]


def test_no_repeat_leaves_the_list_unchanged() -> None:
    raw = [_t("One trade-off."), _t("Another trade-off.")]
    assert said_once_trade_offs(raw) == raw


def test_a_legacy_trade_off_keeps_its_two_fields_equal() -> None:
    out = said_once_trade_offs([_t(f"A. {GSI}"), _t(f"B. {GSI}", impact=f"B. {GSI}")])
    assert out[1]["description"] == "B." and out[1]["impact"] == "B."


def test_a_stored_report_reads_the_same_list_and_its_risk_details_follow() -> None:
    from src.shared.design_text import said_once_report

    report = {
        "trade_offs": [
            _t(f"A. {GSI}"),
            {"description": "Kept the cache.", "impact": "x", "engine": "reality-check"},
            _t(f"B. {GSI}"),
        ],
        "risk_assessment": {
            "risks": [
                {
                    "rule": "design.trade_offs",
                    "details": [f"A. {GSI}".rstrip("."), f"B. {GSI}".rstrip(".")],
                },
                {"rule": "other", "details": [f"B. {GSI}"]},
            ]
        },
    }
    out = said_once_report(report)
    assert out["trade_offs"][2]["description"] == "B."
    assert out["trade_offs"][1] == report["trade_offs"][1]  # the reality check's, in place
    assert out["risk_assessment"]["risks"][0]["details"] == [f"A. {GSI}".rstrip("."), "B"]
    assert out["risk_assessment"]["risks"][1] == report["risk_assessment"]["risks"][1]
    assert said_once_report(out) is out  # idempotent: nothing left to change


def test_synthesis_collects_the_design_trade_offs_stated_once() -> None:
    from types import SimpleNamespace

    from src.agents.referee.synthesis_handler import _collect_trade_offs

    schema = {"trade_offs": [_t(f"A. {GSI}"), _t(f"B. {GSI} Own text.")]}
    data = SimpleNamespace(engines={"dynamodb": SimpleNamespace(schema_design=schema)})
    out = _collect_trade_offs(data)
    assert [t["description"] for t in out] == [f"A. {GSI}", "B. Own text."]
