# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-engine text-search class scores (catalog PR C, #920; review item 8).

The text-search class used to rate every query by the strongest text pattern an engine
has, whatever the query did: a ``LIKE '%x%'`` took OpenSearch's relevance-ranked 95 and
Aurora MySQL's FULLTEXT 80, although no MySQL index serves a leading wildcard. Each text
pattern now counts only for the text search it describes (``only_when`` on the facade's
text-search kind), and each engine's number is its own documented access path:

- a leading-wildcard ``LIKE`` and a regular expression: Aurora PostgreSQL's ``pg_trgm``
  index serves both; Aurora MySQL and DocumentDB read every row (no B-tree serves a
  leading wildcard, ``REGEXP`` is never indexed, a non-prefix ``$regex`` is a collection
  scan). How much that scan costs a frequent query on a large table stays
  ``substring-match``'s conditional penalty (#911): no second penalty is added here.
- a full-text match: OpenSearch's inverted index (95), MySQL FULLTEXT (80), PostgreSQL
  tsvector with GIN (75), DocumentDB ``$text`` with its documented limits (60).
- a similarity function: ``pg_trgm`` (80), OpenSearch fuzzy queries (85).

OpenSearch stays the search engine: no engine outscores it on any text search.
A ``LIKE`` whose whole pattern is a bind parameter shows no text search (its value is
unknown, #912), so no text search is visible.
"""

from __future__ import annotations

import pytest

from src.agents.referee.access_pattern import AD_HOC_FILTER, TEXT_SEARCH, classify_query
from src.shared.catalog.access_classes import class_engine_scores
from src.shared.catalog.patterns import pattern, patterns
from tests.unit.agents.referee.test_access_pattern import KEYS

ENGINES = ("aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb", "opensearch")


def _class_scores(sql: str, engine: str) -> tuple[str, dict[str, int], frozenset[str]]:
    query = {"query_id": "q", "query_text": sql, "query_type": "SELECT"}
    result = classify_query(query, KEYS, engine)
    scores = class_engine_scores(result.facts)[result.name]
    return result.name, {e: scores[e] for e in ENGINES}, result.facts


class TestEachTextSearchIsRatedByItsOwnAccessPath:
    @pytest.mark.parametrize(
        "sql,engine,expected",
        [
            (
                "SELECT * FROM products WHERE title LIKE '%boot%'",
                "postgresql",
                {
                    "aurora_mysql": 45,
                    "aurora_postgresql": 80,
                    "documentdb": 45,
                    "dynamodb": 0,
                    "opensearch": 80,
                },
            ),
            (
                "SELECT * FROM `products` WHERE `title` LIKE '%boot%'",
                "mysql",
                {
                    "aurora_mysql": 45,
                    "aurora_postgresql": 80,
                    "documentdb": 45,
                    "dynamodb": 0,
                    "opensearch": 80,
                },
            ),
            (
                "SELECT * FROM products WHERE title ~* $1",
                "postgresql",
                {
                    "aurora_mysql": 45,
                    "aurora_postgresql": 75,
                    "documentdb": 45,
                    "dynamodb": 0,
                    "opensearch": 75,
                },
            ),
            (
                "SELECT * FROM `products` WHERE `title` REGEXP 'boot+'",
                "mysql",
                {
                    "aurora_mysql": 45,
                    "aurora_postgresql": 75,
                    "documentdb": 45,
                    "dynamodb": 0,
                    "opensearch": 75,
                },
            ),
            (
                "SELECT * FROM products WHERE to_tsvector(body) @@ to_tsquery($1)",
                "postgresql",
                {
                    "aurora_mysql": 80,
                    "aurora_postgresql": 75,
                    "documentdb": 60,
                    "dynamodb": 0,
                    "opensearch": 95,
                },
            ),
            (
                "SELECT * FROM `products` WHERE MATCH(`title`) AGAINST ('boots')",
                "mysql",
                {
                    "aurora_mysql": 80,
                    "aurora_postgresql": 75,
                    "documentdb": 60,
                    "dynamodb": 0,
                    "opensearch": 95,
                },
            ),
            (
                "SELECT * FROM products WHERE similarity(title, $1) > 0.3",
                "postgresql",
                {
                    "aurora_mysql": 0,
                    "aurora_postgresql": 80,
                    "documentdb": 0,
                    "dynamodb": 0,
                    "opensearch": 85,
                },
            ),
        ],
    )
    def test_the_class_score_is_the_matching_patterns(self, sql, engine, expected):
        name, scores, _ = _class_scores(sql, engine)
        assert name == TEXT_SEARCH
        assert scores == expected

    def test_a_query_with_two_kinds_takes_each_engines_best(self):
        _, scores, facts = _class_scores(
            "SELECT * FROM products WHERE title LIKE '%x%' OR to_tsvector(body) @@ to_tsquery($1)",
            "postgresql",
        )
        assert {"leading_wildcard", "full_text"} <= facts
        assert scores["aurora_mysql"] == 80  # its FULLTEXT rating, for the full-text match
        assert scores["aurora_postgresql"] == 80  # pg_trgm, for the wildcard
        assert scores["opensearch"] == 95

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM products WHERE title LIKE '%boot%'",
            "SELECT * FROM products WHERE title ~* $1",
            "SELECT * FROM products WHERE to_tsvector(body) @@ to_tsquery($1)",
            "SELECT * FROM products WHERE similarity(title, $1) > 0.3",
        ],
    )
    def test_opensearch_stays_the_search_engine(self, sql):
        _, scores, _ = _class_scores(sql, "postgresql")
        assert scores["opensearch"] == max(scores.values())


class TestNoTextSearchIsVisible:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM products WHERE title ILIKE $1", "postgresql"),
            ("SELECT * FROM `products` WHERE `title` LIKE ?", "mysql"),
        ],
    )
    def test_a_parameterised_like_shows_no_visible_text_search(self, sql, engine):
        """#912: the bound value is unknown, so no leading wildcard is assumed; the
        query is a filter on a non-key attribute (and keeps OpenSearch's triage signal)."""
        name, _, facts = _class_scores(sql, engine)
        assert name == AD_HOC_FILTER
        assert not facts & {"leading_wildcard", "pattern_match", "full_text", "similarity_match"}

    def test_a_prefix_like_is_no_text_search(self):
        name, _, _ = _class_scores("SELECT * FROM products WHERE title LIKE 'boot%'", "postgresql")
        assert name != TEXT_SEARCH


class TestTheScanStaysSubstringMatchsConditionalPenalty:
    def test_no_text_search_pattern_but_substring_match_has_a_scan_threshold(self):
        """#911's rule is not duplicated: the scan of a frequent query on a large table is
        priced once, by substring-match's threshold."""
        assert [p.id for p in patterns() if p.scan_threshold is not None] == ["substring-match"]

    def test_the_text_search_penalties_are_unchanged(self):
        for pattern_id in ("wildcard-search", "regex-search", "fuzzy-search", "full-text-search"):
            assert (pattern(pattern_id).penalty, pattern(pattern_id).penalty_basis) == (
                40,
                "cannot_run",
            )


_SOURCES = (
    "PostgreSQL documentation",
    "MySQL Reference Manual",
    "Amazon DocumentDB Developer Guide",
)


@pytest.mark.parametrize(
    "pattern_id,engine",
    [
        ("wildcard-search", "aurora_postgresql"),
        ("wildcard-search", "aurora_mysql"),
        ("wildcard-search", "documentdb"),
        ("regex-search", "aurora_postgresql"),
        ("regex-search", "aurora_mysql"),
        ("regex-search", "documentdb"),
        ("fuzzy-search", "aurora_postgresql"),
        ("full-text-search", "aurora_postgresql"),
        ("full-text-search", "aurora_mysql"),
        ("full-text-search", "documentdb"),
    ],
)
def test_every_text_search_class_score_cites_its_public_documentation(pattern_id, engine):
    entry = pattern(pattern_id).engines[engine]
    assert entry.parity_anchor is None, (pattern_id, engine)
    assert any(source in entry.why for source in _SOURCES), (pattern_id, engine, entry.why)
