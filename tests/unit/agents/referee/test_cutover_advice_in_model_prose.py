# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2197: model-written design text never gives cutover advice or claims readiness.

A DocumentDB designer wrote "should be checked against real note counts before cutover"
and "Review the source index list before cutover" into two trade-offs, and synthesis
passed them to ``report.json`` and the engineering report. The shared schema-design
prose pass now sends a field with a cutover or readiness claim back to its
deterministic sentence and logs the rejection. The claim is found by one rule,
``cutover_wording.cutover_claims``, which the deliverables guard uses for model text too.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.shared.engine_names import display_engine

# The two sentences a real discourse run wrote (batch11122123), in DocumentDB trade-off impacts.
REAL_RUN = [
    "A reviewable with a very long note history grows its document. The assumed bound keeps "
    "the document near 18 KB, far below the 16 MB limit, but the bound is an assumption and "
    "should be checked against real note counts before cutover.",
    "Constraints that the source enforced through unique indexes on other tables, such as "
    "unique names and unique pairs, are not enforced in DocumentDB until the application or a "
    "migration step creates the matching unique indexes. Review the source index list before "
    "cutover and recreate the unique ones that the application relies on.",
]
DESCRIPTION = "Reviewable notes are embedded under reviewables as a notes array."


def _trade_off_design(impact: str) -> dict[str, Any]:
    return {
        "trade_offs": [
            {
                "description": DESCRIPTION,
                "impact": impact,
                "source_tables": ["public.reviewables"],
                "target_tables": ["reviewables"],
            }
        ]
    }


def _review(engine: str) -> str:
    return (
        f"Review this trade-off with the {display_engine(engine)} schema design before migration."
    )


@pytest.mark.parametrize("impact", REAL_RUN)
def test_the_real_run_sentences_go_back_to_the_deterministic_sentence(impact: str) -> None:
    out, rejections = clean_schema_design_prose("documentdb", _trade_off_design(impact))
    assert out is not None
    entry = out["trade_offs"][0]
    assert entry["impact"] == _review("documentdb")
    assert entry["description"] == DESCRIPTION
    assert rejections == ['documentdb trade_offs[0].impact: cutover advice ("cutover timing")']


ADVICE = "Load-test the queries before cutover."

# Every model-written field the pass cleans, and the deterministic text it falls back to.
FIELDS = [
    (
        "dynamodb",
        {"trade_offs": [{"description": ADVICE, "source_tables": ["public.users"]}]},
        ("trade_offs", 0, "description"),
        "DynamoDB design trade-off for public.users.",
    ),
    ("dynamodb", {"trade_offs": [ADVICE]}, ("trade_offs", 0, None), "DynamoDB design trade-off."),
    (
        "opensearch",
        {
            "access_patterns": [
                {"description": ADVICE, "operation": "search", "index": "topics"},
            ]
        },
        ("access_patterns", 0, "description"),
        "search on topics",
    ),
    (
        "documentdb",
        {"unsupported_patterns": [{"workaround": ADVICE}]},
        ("unsupported_patterns", 0, "workaround"),
        "Review these queries with the DocumentDB schema design before migration.",
    ),
    (
        "documentdb",
        {"unsupported_patterns": [{"reason": ADVICE}]},
        ("unsupported_patterns", 0, "reason"),
        "",
    ),
    (
        "elasticache",
        {"migration_notes": [{"application_logic_required": ADVICE}]},
        ("migration_notes", 0, "application_logic_required"),
        "",
    ),
    (
        "aurora_postgresql",
        {
            "optimizations": [
                {"category": "index", "target": "public.topics", "recommendation": ADVICE}
            ]
        },
        ("optimizations", 0, "recommendation"),
        "Index optimization on public.topics.",
    ),
    (
        "aurora_postgresql",
        {"app_layer_notes": [{"recommendation": ADVICE}]},
        ("app_layer_notes", 0, "recommendation"),
        "",
    ),
]


@pytest.mark.parametrize("engine, design, where, fallback", FIELDS)
def test_every_model_written_field_is_checked(
    engine: str, design: dict[str, Any], where: tuple[str, int, str | None], fallback: str
) -> None:
    out, rejections = clean_schema_design_prose(engine, design)
    assert out is not None
    key, i, field = where
    value = out[key][i] if field is None else out[key][i][field]
    assert value == fallback
    path = f"{key}[{i}]" + (f".{field}" if field else "")
    assert rejections == [f'{engine} {path}: cutover advice ("cutover timing")']


@pytest.mark.parametrize(
    "claim, labels",
    [
        ("The table is ready to switch.", '"ready"'),
        ("The design is proven to work, so the move can go ahead.", '"proven"'),
        ("Keep the source running until DynamoDB is in sync.", '"in sync"'),
        # #2265: the decommissioning advice is named too.
        ("Once replication has caught up, decommission the source.", '"caught up", "decommission"'),
        ("Retire the source when replication lag reaches zero.", '"decommission", "lag is zero"'),
        ("Decommission the source once you have completed your cutover.", '"decommission"'),
    ],
)
def test_a_readiness_claim_goes_back_too(claim: str, labels: str) -> None:
    out, rejections = clean_schema_design_prose("dynamodb", _trade_off_design(claim))
    assert out is not None
    assert out["trade_offs"][0]["impact"] == _review("dynamodb")
    assert rejections == [f"dynamodb trade_offs[0].impact: cutover advice ({labels})"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "fine",
    [
        "A reviewable with a very long note history grows its document; check the assumed "
        "bound against real note counts.",
        "The sort key is the id, so (user_id, name) is not proven unique and keying on it could "
        "overwrite a row.",
        "The GSI is ready-made for this lookup.",
        "A DynamoDB Streams consumer keeps the summary item in sync with the order items.",
        "1 of the 3 tables stays dual-read until wave 4.",
        "The index must exist before the query moves.",
    ],
)
def test_data_statements_are_kept_as_written(fine: str) -> None:
    out, rejections = clean_schema_design_prose("dynamodb", _trade_off_design(fine))
    assert out is not None
    assert out["trade_offs"][0]["impact"] == fine
    assert rejections == []


def test_a_field_with_cutover_advice_and_a_style_hit_logs_both() -> None:
    impact = "Moreover, review the index list before cutover."
    out, rejections = clean_schema_design_prose("documentdb", _trade_off_design(impact))
    assert out is not None
    assert out["trade_offs"][0]["impact"] == _review("documentdb")
    assert rejections == [
        'documentdb trade_offs[0].impact: plain-prose style list ("moreover")',
        'documentdb trade_offs[0].impact: cutover advice ("cutover timing")',
    ]


def test_the_prose_pass_and_the_guard_use_one_rule() -> None:
    """The guard's model-text words are the shared rule's, and the pass rejects every
    sentence the guard flags and keeps every one it passes."""
    from src.shared.cutover_wording import MODEL_TEXT_BANNED, MODEL_TEXT_LABELS
    from tests.unit.docs import test_no_cutover_advice as guard

    assert {label: guard.BANNED[label] for label in MODEL_TEXT_LABELS} == MODEL_TEXT_BANNED
    for advice in guard.MODEL_TEXT_ADVICE:
        out, rejections = clean_schema_design_prose("dynamodb", _trade_off_design(advice))
        assert out is not None and out["trade_offs"][0]["impact"] == _review("dynamodb"), advice
        assert len(rejections) == 1 and "cutover advice" in rejections[0], advice
    for fine in guard.MODEL_TEXT_FINE:
        out, rejections = clean_schema_design_prose("dynamodb", _trade_off_design(fine))
        assert out is not None and out["trade_offs"][0]["impact"] == fine, fine
        assert rejections == [], fine


DATA_MODELING_SKILLS = (
    "aurora_mysql-data-modeling.md",
    "aurora_postgresql-data-modeling.md",
    "documentdb-data-modeling.md",
    "dynamodb-data-modeling.md",
    "elasticache-data-modeling.md",
    "opensearch-index-modeling.md",
)


@pytest.mark.parametrize("name", DATA_MODELING_SKILLS)
def test_every_designer_skill_says_what_the_pass_does(name: str) -> None:
    """The cutover rule (``SKILL_RULE``, checked in every customer-prose skill) is followed
    by what synthesis does with a field that breaks it."""
    from src.shared.cutover_wording import CUTOVER_FALLBACK_RULE
    from src.shared.skill_sections import skill_sections
    from tests.unit.docs.test_no_cutover_advice import SKILL_RULE

    section = skill_sections(name)["Customer-facing text"]
    assert f"{SKILL_RULE} {CUTOVER_FALLBACK_RULE}" in section


def test_the_fallback_rule_names_decommissioning() -> None:
    """#2265: a field that advises on decommissioning is swapped too, and the skills say so."""
    from src.shared.cutover_wording import CUTOVER_FALLBACK_RULE

    assert CUTOVER_FALLBACK_RULE == (
        "Synthesis swaps a field that says when to cut over, that advises on decommissioning, "
        "or that uses readiness wording, for a short deterministic sentence."
    )


def test_the_group_draft_agent_says_what_the_pass_does() -> None:
    from pathlib import Path

    from src.shared.cutover_wording import CUTOVER_FALLBACK_RULE

    agent = Path(__file__).resolve().parents[4] / ".claude" / "agents" / "dynamodb-group-draft.md"
    assert CUTOVER_FALLBACK_RULE in agent.read_text()
