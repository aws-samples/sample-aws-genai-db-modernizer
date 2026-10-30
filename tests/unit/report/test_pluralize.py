# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pluralisation helper (issue #206).

The executive summary deck printed "1 session store queries": every count+noun
string in the old deck renderer and the report renderers built its noun with a fixed
plural (or a one-off ``"" if n == 1 else "s"`` at a couple of call sites),
disagreeing on the rule and getting at least one of them wrong. One helper,
``formatting.plural_noun``, now carries the English count-agreement rule; every
count+noun call site composes ``f"{n} {plural_noun(n, 'noun')}"``.
"""

from __future__ import annotations

import pytest

from src.report import renderers


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
