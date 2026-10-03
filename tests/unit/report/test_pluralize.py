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
