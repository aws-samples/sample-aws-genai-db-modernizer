"""Pluralisation helper (issue #206).

The executive summary deck printed "1 session store queries": every count+noun
string in ``pptx_report.py`` and ``renderers.py`` built its noun with a fixed
plural (or a one-off ``"" if n == 1 else "s"`` at a couple of call sites),
disagreeing on the rule and getting at least one of them wrong. One helper,
``renderers.plural_noun``, now carries the English count-agreement rule; every
count+noun call site composes ``f"{n} {plural_noun(n, 'noun')}"``.
"""

from __future__ import annotations

import pytest

from src.report import pptx_report, renderers


class TestPluralNoun:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [(0, "0 risks"), (1, "1 risk"), (2, "2 risks"), (12, "12 risks")],
    )
    def test_regular_plural(self, n: int, expected: str) -> None:
        assert f"{n} {renderers.plural_noun(n, 'risk')}" == expected

    @pytest.mark.parametrize(
        ("n", "expected"),
        [(0, "0 queries"), (1, "1 query"), (2, "2 queries")],
    )
    def test_irregular_plural(self, n: int, expected: str) -> None:
        assert f"{n} {renderers.plural_noun(n, 'query', 'queries')}" == expected

    def test_defaults_to_appending_s(self) -> None:
        assert renderers.plural_noun(1, "table") == "table"
        assert renderers.plural_noun(2, "table") == "tables"
        assert renderers.plural_noun(0, "table") == "tables"


class TestPluralVerb:
    """Verb agreement is the opposite polarity of noun pluralisation (a
    singular subject takes the "-s" form), and irregular enough (is/are,
    touches/touch) that both forms are always given explicitly -- a
    dedicated helper rather than reusing plural_noun for verbs, which read
    backwards at the call site (#206 follow-up)."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [(0, "sit"), (1, "sits"), (2, "sit")],
    )
    def test_irregular_s_verb(self, n: int, expected: str) -> None:
        assert renderers.plural_verb(n, "sits", "sit") == expected

    @pytest.mark.parametrize(
        ("n", "expected"),
        [(0, "are"), (1, "is"), (2, "are")],
    )
    def test_to_be(self, n: int, expected: str) -> None:
        assert renderers.plural_verb(n, "is", "are") == expected

    def test_composes_with_a_count_and_noun(self) -> None:
        assert (
            f"1 risk {renderers.plural_verb(1, 'sits', 'sit')} on dynamodb alone."
            == "1 risk sits on dynamodb alone."
        )
        assert (
            f"2 risks {renderers.plural_verb(2, 'sits', 'sit')} on dynamodb alone."
            == "2 risks sit on dynamodb alone."
        )


class TestSessionStoreRegression:
    """The exact bug reported in #206: 'PDF says "1 session store queries"'."""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, "0 session store queries in the whole workload"),
            (1, "1 session store query in the whole workload"),
            (2, "2 session store queries in the whole workload"),
        ],
    )
    def test_evidence_text_pluralises_query_correctly(self, count: int, expected: str) -> None:
        thinnest = {"count": count, "name": "session_store"}
        assert pptx_report._evidence_text(thinnest) == expected

    def test_evidence_text_falls_back_when_no_signal(self) -> None:
        assert pptx_report._evidence_text(None) == "limited supporting evidence"


class TestWorstEngineSentenceVerbAgreement:
    """#206 follow-up regression: the verb must agree with the count sitting
    on the worst engine (the sentence's subject), not with the sentence's
    other number (the total HIGH risk count across every engine)."""

    def test_singular_subject_takes_sits_even_when_total_is_plural(self) -> None:
        sentence = pptx_report._worst_engine_sentence(("dynamodb", 1), 5)
        assert sentence == "1 of the 5 HIGH risks sits on DynamoDB alone."

    def test_plural_subject_takes_sit(self) -> None:
        sentence = pptx_report._worst_engine_sentence(("dynamodb", 2), 5)
        assert sentence == "2 of the 5 HIGH risks sit on DynamoDB alone."

    def test_both_counts_singular(self) -> None:
        sentence = pptx_report._worst_engine_sentence(("dynamodb", 1), 1)
        assert sentence == "1 of the 1 HIGH risk sits on DynamoDB alone."


class TestRootCauseSentenceDeterminerAndVerb:
    def test_singular_uses_the_not_all(self) -> None:
        assert pptx_report._root_cause_sentence(1, "data model") == "the 1 HIGH risk is data model"

    def test_plural_uses_all(self) -> None:
        assert (
            pptx_report._root_cause_sentence(3, "data model") == "all 3 HIGH risks are data model"
        )
