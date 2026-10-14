"""The customer-text guard on ``reality-check/output.json``'s ``flag_decisions`` (#647).

A real-model validation run produced a moved flag and the guard reported the
raw engine ids in ``flagged_engine``/``destination_engine``. Those are
structured id fields, like ``Consolidation.from_engine``; the decision's
``reason`` is still scanned. The model's ``explanation`` is exempt because no
deliverable renders it, and a test here keeps that true.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.agents.referee.model_flags import apply_consolidation_flags
from src.agents.referee.query_scoring import ScoringContext
from src.contracts.model_flag import ModelFlag
from src.contracts.reality_check_output import FlagDecision
from src.tools.validation.customer_text_guard import scan_json

REPO = Path(__file__).resolve().parents[4]


def _query(qid: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
        "query_type": "SELECT",
        "tables_accessed": [f"t_{qid}"],
    }


def _decisions() -> list[dict]:
    """Real decisions: q1 moved (DynamoDB -> Aurora MySQL), q2 kept, q3 ignored,
    plus a co-dependency member moved with q1."""
    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "t_q1", "confidence_score": 69},
                {"table_id": "t_g1", "confidence_score": 69},
            ]
        },
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "t_q1", "confidence_score": 54},
                {"table_id": "t_g1", "confidence_score": 54},
            ]
        },
    }
    ctx = ScoringContext(analysis_outputs=analysis)
    original = [
        {"query_id": q, "assigned_engine": "aurora_mysql" if q == "a1" else "documentdb"}
        for q in ("q1", "g1", "q2", "a1")
    ]
    revised = [{"query_id": q, "assigned_engine": "dynamodb"} for q in ("q1", "g1", "q2")] + [
        {"query_id": "a1", "assigned_engine": "aurora_mysql"}
    ]
    flags = [
        ModelFlag.model_validate(
            {
                "query_ids": ids,
                "flagged_engine": engine,
                "reason_category": "client_side_join",
                "explanation": "needs a server-side join",
            }
        )
        for ids, engine in ((["q1"], "dynamodb"), (["q2"], "dynamodb"), (["a1"], "dynamodb"))
    ]

    def apply(selected: list[ModelFlag], candidates: set[str]) -> list[dict]:
        _, _, decisions, _ = apply_consolidation_flags(
            selected,
            [dict(qa) for qa in revised],
            [
                {
                    "from_engine": "documentdb",
                    "to_engine": "dynamodb",
                    "query_count": 3,
                    "reason": "r",
                    "saved_cost_estimate": 0,
                }
            ],
            original,
            ctx,
            {q: _query(q) for q in ("q1", "g1", "q2", "a1")},
            candidates,
            co_dependency_groups=[["q1", "g1"]],
        )
        return decisions

    # q2 has nowhere to go (DynamoDB is the only kept engine for that call).
    decisions = apply([flags[0], flags[2]], {"dynamodb", "aurora_mysql"})
    decisions += apply([flags[1]], {"dynamodb"})
    return decisions


def test_a_real_flag_decisions_list_passes_the_guard():
    decisions = _decisions()
    assert sorted(d["outcome"] for d in decisions) == ["ignored", "kept", "moved"]
    moved = next(d for d in decisions if d["outcome"] == "moved")
    assert (moved["flagged_engine"], moved["destination_engine"]) == ("dynamodb", "aurora_mysql")
    assert moved["moved_with_group"] == ["g1"]
    for d in decisions:
        FlagDecision.model_validate(d)
    assert scan_json({"flag_decisions": decisions}) == []


def test_the_reason_is_still_scanned_and_the_explanation_is_not():
    """The ``reason`` is customer-facing text; the model's ``explanation`` is
    audit evidence no deliverable shows (maintainer decision, 2026-10-14)."""
    decision = {**_decisions()[0], "reason": "moved to aurora_mysql", "explanation": "use dynamodb"}
    paths = {f.path for f in scan_json({"flag_decisions": [decision]})}
    assert "$.flag_decisions[0].reason" in paths
    assert "$.flag_decisions[0].explanation" not in paths


# A read of the key in renderer code: a quoted key or an attribute access.
_EXPLANATION_READ = re.compile(r"""["']explanation["']|\.explanation\b""")


def test_no_renderer_reads_the_explanation():
    """The guard exempts ``explanation`` only because nothing renders it. If a
    report renderer or the UI starts reading it, this fails and the exemption in
    customer_text_guard.ID_FIELD_PATHS has to be revisited."""
    sources = [
        *REPO.glob("src/report/**/*.py"),
        *REPO.glob("src/report/templates/*"),
        *(
            p
            for p in REPO.glob("src/ui/src/**/*.js")
            if "__tests__" not in p.parts and "node_modules" not in p.parts
        ),
    ]
    assert sources
    readers = [
        str(p.relative_to(REPO))
        for p in sources
        if p.is_file() and _EXPLANATION_READ.search(p.read_text(encoding="utf-8"))
    ]
    assert readers == []


def test_the_id_fields_are_exempt_only_under_flag_decisions():
    findings = scan_json(
        {"other": [{"flagged_engine": "dynamodb", "destination_engine": "aurora_mysql"}]}
    )
    assert {f.path for f in findings} == {
        "$.other[0].flagged_engine",
        "$.other[0].destination_engine",
    }
