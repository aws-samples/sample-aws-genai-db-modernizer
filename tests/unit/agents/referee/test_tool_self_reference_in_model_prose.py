# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2206: model design text never calls the tool "the pipeline".

The discourse sample's Aurora PostgreSQL ``app_layer_notes`` said "The pipeline emits
no trigger DDL". The shared schema-design prose pass rewrites that "no X" form as
"No X is generated", and sends a field with any other self-reference ("the pipeline"
before a tool verb, "this pipeline", "our pipeline") back to its deterministic text.
Legitimate uses ("aggregation pipeline", "sync pipeline", ...) are never touched.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.shared.prose_style import (
    pipeline_term_spans,
    pipeline_terms_in,
    replace_pipeline_terms,
)
from src.tools.validation.customer_text_guard import scan_text

ISSUE_SENTENCE = (
    "The pipeline emits no trigger DDL, so deploy it from the source definition and "
    "test it on Aurora PostgreSQL"
)


def test_the_issue_sentence_reads_in_customer_words() -> None:
    assert pipeline_terms_in(ISSUE_SENTENCE) == ["the pipeline"]
    out = replace_pipeline_terms(ISSUE_SENTENCE)
    assert out == (
        "No trigger DDL is generated, so deploy it from the source definition and "
        "test it on Aurora PostgreSQL"
    )
    assert pipeline_terms_in(out) == [] and scan_text(out) == []


def test_the_issue_sentence_in_a_design_is_rewritten_and_logs_nothing() -> None:
    schema = {"app_layer_notes": [{"recommendation": ISSUE_SENTENCE}]}
    cleaned, rejections = clean_schema_design_prose("aurora_postgresql", schema)
    assert cleaned["app_layer_notes"][0]["recommendation"].startswith(
        "No trigger DDL is generated, so"
    )
    assert rejections == []


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Deploy it by hand because the pipeline generates no views.",
            "Deploy it by hand because no views are generated.",
        ),
        ("The pipeline writes no stored procedure.", "No stored procedure is generated."),
        (
            "The pipeline produces no triggers and no rules.",
            "No triggers and no rules are generated.",
        ),
    ],
)
def test_the_no_form_is_rewritten_with_agreement(text: str, expected: str) -> None:
    assert replace_pipeline_terms(text) == expected
    assert pipeline_terms_in(expected) == []


@pytest.mark.parametrize(
    "text",
    [
        "The pipeline generates the DDL for each table.",
        "The pipeline does not emit trigger DDL.",
        "The pipeline doesn't emit trigger DDL.",
        "The pipeline didn’t write the index.",
        "The pipeline then renders a report.",
        "The Pipeline Creates a table per group.",
        "This pipeline keeps posts and topics together.",
        "Our pipeline keeps posts and topics together.",
        "Review the DDL; the pipeline outputs it as written.",
    ],
)
def test_other_self_references_fall_back_to_the_deterministic_sentence(text: str) -> None:
    assert pipeline_terms_in(replace_pipeline_terms(text)) != []
    assert scan_text(text) != []  # the deliverables guard flags it too
    schema = {"trade_offs": [{"description": "Users keyed by id.", "impact": text}]}
    cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
    out = cleaned["trade_offs"][0]["impact"]
    assert out != text and pipeline_terms_in(out) == [] and scan_text(out) == []
    assert len(rejections) == 1 and "pipeline terms" in rejections[0]


@pytest.mark.passes_on_main  # a negative guard: these uses were never flagged
@pytest.mark.parametrize(
    "text",
    [
        "Run the aggregation pipeline with $lookup to join orders.",
        "The aggregation pipeline writes the result with $out.",
        "The sync pipeline emits change events to the target.",
        "The CI/CD pipeline builds the schema scripts.",
        "A data pipeline generates the nightly export.",
        "The ETL pipeline writes rows to the table.",
        "The change-stream pipeline produces one event per update.",
        "The $lookup pipeline creates a joined array.",
        "Build a pipeline of stages, then the pipeline stage runs first.",
        "Pipelines are checked before release.",
        "Our pipelines are irrelevant here.",
    ],
)
def test_legitimate_uses_of_pipeline_are_left_alone(text: str) -> None:
    assert pipeline_term_spans(text) == []
    assert pipeline_terms_in(text) == []
    assert replace_pipeline_terms(text) == text
    assert scan_text(text) == []
    schema = {"trade_offs": [{"description": "Users keyed by id.", "impact": text}]}
    cleaned, rejections = clean_schema_design_prose("documentdb", schema)
    assert cleaned["trade_offs"][0]["impact"] == text
    assert rejections == []
