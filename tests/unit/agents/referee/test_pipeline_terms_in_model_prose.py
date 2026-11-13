# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2184: the design split's own terms never reach the customer from model text.

A DynamoDB group draft wrote "which another design part owns" and "Other design
parts read users by the source id" into its trade-offs, and synthesis passed them
to ``report.json`` and the engineering report. The shared schema-design prose pass
rewrites "design part(s)" to "part(s) of the design" (no word dropped, no fact
changed) and sends a field with any other pipeline term back to its deterministic
text; the skills that write this text carry the customer-wording rule.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.shared.prose_style import (
    PIPELINE_TERMS,
    PIPELINE_TERMS_RULE,
    pipeline_terms_in,
    replace_pipeline_terms,
)
from src.shared.skill_sections import skill_path
from src.tools.validation.customer_text_guard import scan_text

# The sentences a real discourse run wrote (batch11121734), and how they read after.
REAL_RUN = [
    (
        "The caller's group ids come from public.group_users, which another design part owns, "
        "so this design defines no placeholder or index for it.",
        "The caller's group ids come from public.group_users, which another part of the design "
        "owns, so this design defines no placeholder or index for it.",
    ),
    (
        "Other design parts read users by the source id, so the id stays in the key.",
        "Other parts of the design read users by the source id, so the id stays in the key.",
    ),
    (
        "The write that changes users.title belongs to the users design part.",
        "The write that changes users.title belongs to the users part of the design.",
    ),
    (
        "The source statement reads user_profiles, badges and user_badges, which other design "
        "parts own.",
        "The source statement reads user_profiles, badges and user_badges, which other parts of "
        "the design own.",
    ),
    ("Design parts share one table.", "Parts of the design share one table."),
]


@pytest.mark.parametrize("text, expected", REAL_RUN)
def test_design_part_is_rewritten_in_customer_words(text: str, expected: str) -> None:
    out = replace_pipeline_terms(text)
    assert out == expected
    assert pipeline_terms_in(out) == []
    assert scan_text(out) == []


@pytest.mark.parametrize(
    "text",
    [
        "Posts are keyed by topic_id; each partition holds one topic.",
        "The `design_part` attribute is an identifier, not prose.",
        "Every part of the design reads posts by id.",
        "",
    ],
)
def test_text_without_the_term_is_unchanged(text: str) -> None:
    assert replace_pipeline_terms(text) == text
    assert pipeline_terms_in(text) == []


def test_the_guard_and_the_prose_pass_share_one_term_list() -> None:
    from src.tools.validation import customer_text_guard

    assert customer_text_guard.PIPELINE_TERMS is PIPELINE_TERMS
    assert set(PIPELINE_TERMS) <= set(customer_text_guard.LOG_ONLY_PHRASES)


def test_schema_design_pass_rewrites_design_part_and_logs_nothing() -> None:
    text, expected = REAL_RUN[1]
    schema = {
        "trade_offs": [{"description": "Users keyed by id.", "impact": text}],
        "unsupported_patterns": [{"pattern_type": "join", "workaround": REAL_RUN[0][0]}],
        "access_patterns": [{"description": REAL_RUN[2][0], "operation": "Query"}],
    }
    cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
    assert cleaned["trade_offs"][0]["impact"] == expected
    assert cleaned["unsupported_patterns"][0]["workaround"] == REAL_RUN[0][1]
    assert cleaned["access_patterns"][0]["description"] == REAL_RUN[2][1]
    assert rejections == []
    assert schema["trade_offs"][0]["impact"] == text  # the design on disk is unchanged


@pytest.mark.parametrize("term", [t for t in PIPELINE_TERMS if t != "design part"])
def test_any_other_pipeline_term_sends_the_field_back(term: str) -> None:
    impact = f"Posts and their meta stay together because {term} them."
    schema = {"trade_offs": [{"description": "Posts keyed by id.", "impact": impact}]}
    cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
    out = cleaned["trade_offs"][0]["impact"]
    assert out != impact and pipeline_terms_in(out) == [] and scan_text(out) == []
    assert len(rejections) == 1 and f'"{term}"' in rejections[0]


DATA_MODELING_SKILLS = (
    "aurora_mysql-data-modeling.md",
    "aurora_postgresql-data-modeling.md",
    "documentdb-data-modeling.md",
    "dynamodb-data-modeling.md",
    "elasticache-data-modeling.md",
    "opensearch-index-modeling.md",
)


@pytest.mark.parametrize("name", DATA_MODELING_SKILLS)
def test_every_designer_skill_carries_the_rule_and_never_uses_the_term(name: str) -> None:
    text = skill_path(name).read_text()
    assert PIPELINE_TERMS_RULE in text
    assert pipeline_terms_in(text.replace(PIPELINE_TERMS_RULE, "")) == []


def test_the_group_draft_agent_carries_the_rule() -> None:
    from pathlib import Path

    agent = Path(__file__).resolve().parents[4] / ".claude" / "agents" / "dynamodb-group-draft.md"
    text = agent.read_text()
    assert PIPELINE_TERMS_RULE in text
    assert pipeline_terms_in(text.replace(PIPELINE_TERMS_RULE, "")) == []
