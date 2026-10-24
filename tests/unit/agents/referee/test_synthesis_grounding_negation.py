"""A negated engine mention is not a claim about that engine (#1417).

``recommends_engine`` and ``check_summary_grounding`` share one definition of
negation, ``_engine_negated``.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_grounding import (
    _engine_negated,
    check_summary_grounding,
    recommends_engine,
)

ENGINE_TABLES = {"aurora_postgresql": ["db.wp_postmeta"], "elasticache": ["db.wp_options"]}


def _grounding(text: str) -> list[dict]:
    return check_summary_grounding(text, ENGINE_TABLES, "db")


@pytest.mark.parametrize(
    "sentence",
    [
        "Do not move these reads to OpenSearch.",
        "Don't move these reads to OpenSearch.",
        "Never offload these searches to OpenSearch.",
        "You cannot use OpenSearch for these reads.",
        "OpenSearch should not handle these reads.",
        "OpenSearch cannot serve these reads.",
        "Keep the reads in Aurora instead of OpenSearch.",
        "Keep the reads in Aurora without OpenSearch.",
        "Use no OpenSearch.",
    ],
)
def test_negated_mention_is_not_a_recommendation(sentence: str) -> None:
    assert not recommends_engine(sentence, "opensearch")


@pytest.mark.parametrize(
    "sentence",
    [
        "Move these reads to OpenSearch.",
        "Offload search to OpenSearch, which eliminates scans.",
        "OpenSearch should handle the full-text search.",
        "Do not hesitate to move these reads to OpenSearch.",
        "Not only keep the rows in Aurora, also use OpenSearch for search.",
        "Do not wait; move these reads to OpenSearch.",
        "Do not move these reads. Use OpenSearch for search.",
    ],
)
def test_recommendation_still_counts(sentence: str) -> None:
    assert recommends_engine(sentence, "opensearch")


def test_negation_in_another_sentence_does_not_leak() -> None:
    assert recommends_engine("This is not a risk. Move the reads to OpenSearch.", "opensearch")


@pytest.mark.parametrize(
    "sentence",
    [
        "ElastiCache does not serve the post meta lookups.",
        "ElastiCache doesn't serve wp_postmeta.",
        "ElastiCache never serves wp_postmeta.",
        "ElastiCache cannot serve the post meta lookups.",
        "The wp_postmeta table is not served by ElastiCache.",
        "The wp_postmeta table is never served from ElastiCache.",
    ],
)
def test_true_negative_claim_passes_grounding(sentence: str) -> None:
    assert _grounding(sentence) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "ElastiCache serves the post meta lookups.",
        "ElastiCache serves wp_postmeta, not Aurora PostgreSQL.",
        "Aurora PostgreSQL does not serve wp_options, and ElastiCache serves wp_postmeta.",
        "ElastiCache serves wp_postmeta and does not serve wp_options.",
        "ElastiCache serves wp_postmeta lookups not writes.",
    ],
)
def test_false_claim_still_fails_grounding(sentence: str) -> None:
    findings = _grounding(sentence)
    assert findings
    assert any(f["high_confidence"] for f in findings)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("Use ", " for search", False),
        ("Do not move these reads to ", "", True),
        ("Do not hesitate to move reads to ", "", False),
        ("a b c d e f g not ", "", True),
        ("not a b c d e f g ", "", False),
        ("", " would not help", True),
        ("", " serves lookups not writes", False),
    ],
)
def test_engine_negated(before: str, after: str, expected: bool) -> None:
    assert _engine_negated(before, after) is expected


@pytest.mark.parametrize(
    "sentence",
    [
        "There is no reason not to use OpenSearch.",
        "It isn't hard to move search to OpenSearch.",
        "We won't regret moving to OpenSearch.",
        "Do not delay moving search to OpenSearch.",
        "Without question, move search to OpenSearch.",
        "Use OpenSearch, never Aurora, for search.",
        "Use OpenSearch, not Aurora.",
        "There is no downside to using OpenSearch.",
        "It is hard to say no to OpenSearch.",
    ],
)
def test_double_negation_and_clause_breaks_still_recommend(sentence: str) -> None:
    assert recommends_engine(sentence, "opensearch")


def test_comma_clause_negates_only_its_own_engine() -> None:
    assert recommends_engine("Use DynamoDB, not Aurora.", "dynamodb")
    assert not recommends_engine("Use DynamoDB, not Aurora.", "aurora_postgresql")
    assert not recommends_engine("Use OpenSearch, never Aurora, for search.", "aurora_mysql")
    assert not recommends_engine("Keep reads in Aurora, but do not use OpenSearch.", "opensearch")


@pytest.mark.parametrize(
    "sentence",
    [
        "There is no reason not to use ElastiCache for wp_postmeta.",
        "ElastiCache will not only serve wp_options but also wp_postmeta.",
        "ElastiCache serves wp_options, but also serves wp_postmeta.",
    ],
)
def test_double_negation_and_not_only_are_still_claims(sentence: str) -> None:
    assert _grounding(sentence)


def test_not_only_after_the_engine_is_a_positive_claim() -> None:
    both = {"elasticache": ["db.wp_options", "db.wp_postmeta"]}
    sentence = "ElastiCache will not only serve wp_options but also wp_postmeta."
    assert check_summary_grounding(sentence, both, "db") == []
    assert not _engine_negated("", " will not only serve")
    assert _engine_negated("", " will not serve")


@pytest.mark.parametrize(
    "sentence",
    [
        "ElastiCache serves wp_options, but does not serve wp_postmeta.",
        "ElastiCache serves wp_options while it does not serve wp_postmeta.",
    ],
)
def test_negated_second_clause_passes_grounding(sentence: str) -> None:
    assert _grounding(sentence) == []
