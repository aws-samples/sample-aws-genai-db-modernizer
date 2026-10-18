"""#801 review: the customer gate's rejection reads "it <phrase>", so every
capability's phrase must read as what the query does or needs. A utility
statement *is* one ("it is a utility or DDL statement"), never "it needs a
utility or DDL statement"; several gaps are joined with "and"."""

from __future__ import annotations

import pytest

from src.agents.referee.capability_registry import ENGINE_CAPABILITIES
from src.shared.capability_labels import CAPABILITY_DISPLAY_NAMES, capability_gap_phrase


@pytest.mark.parametrize(
    ("capability", "phrase"),
    [
        ("aggregation", "needs aggregation"),
        ("complex_joins", "needs multi-table joins"),
        ("computed_join", "joins on a computed expression"),
        ("sql_admin", "is a utility or DDL statement"),
        ("text_pattern_match", "needs full-text or pattern search"),
        ("scan_engine", "uses a recursive query"),
        ("ranking_window", "uses a ranking window function"),
        ("offset_window", "uses LAG, LEAD or NTILE"),
        ("multi_doc_acid", "needs multi-document transactions"),
    ],
)
def test_each_capability_reads_as_what_the_query_does(capability: str, phrase: str) -> None:
    assert capability_gap_phrase([capability]) == phrase


def test_every_capability_has_a_phrase() -> None:
    known = set(CAPABILITY_DISPLAY_NAMES).union(*ENGINE_CAPABILITIES.values())
    known.discard("strong_consistency")  # an engine trait, never a query requirement
    covered = {"aggregation", "complex_joins", "computed_join", "sql_admin"}
    covered |= {"text_pattern_match", "scan_engine", "multi_doc_acid"}
    covered |= {"ranking_window", "offset_window"}
    assert known == covered


def test_two_needs_are_joined_with_and() -> None:
    assert capability_gap_phrase(["complex_joins", "aggregation"]) == (
        "needs aggregation and multi-table joins"
    )


def test_three_gaps_of_mixed_shape_read_as_one_list() -> None:
    assert capability_gap_phrase(["aggregation", "scan_engine", "complex_joins"]) == (
        "uses a recursive query, and needs aggregation and " "multi-table joins"
    )


def test_three_needs_read_as_a_list() -> None:
    assert capability_gap_phrase(["text_pattern_match", "aggregation", "complex_joins"]) == (
        "needs aggregation, multi-table joins and full-text or pattern search"
    )


def test_an_unknown_capability_falls_back_to_needs_its_label() -> None:
    assert capability_gap_phrase(["geo_index"]) == "needs geo index"
