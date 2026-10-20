"""Acceptance tests of the per-query access-pattern switch (#728, ADR-031 Decision 14 Amendment 5).

They read only the routing path every production caller uses
(``ScoringContext.from_pipeline`` and ``score_query_for_engine``):

- #784: more relational evidence found on a table never lowers an engine's score for
  a query (the ``web_crawler_requests`` 79 -> 78 repro);
- #885: a key-anchored join never inherits its tables' strongest pattern (``topics`` +
  ``categories`` must not take a ``categories`` key-lookup 100); that discourse's
  ``topics`` + ``categories`` joins stay on Aurora PostgreSQL is checked on the sample
  itself (``tests/e2e/test_per_query_pattern_score.py``);
- criterion 4b (narrowed by the maintainer to the pattern score, 2026-10-16): adding a
  query to a table never changes another query's score through the pattern dimension,
  except through the one key-anchor rule (#1061): a new query that makes a column a key
  of its table (another access pattern looks the table up by it) can change the class of
  the queries that use that column, and of no other query.
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.access_pattern import SchemaKeys, query_access
from src.agents.referee.query_pattern import QueryPatternModel
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.contracts.analysis_output import ScoreBreakdown
from src.tools.analysis.scoring import compute_confidence

WEIGHTS = {
    "aurora_postgresql": {"pattern_match": 0.4, "complexity": 0.3, "performance": 0.2, "cost": 0.1},
    "dynamodb": {"pattern_match": 0.6, "complexity": 0.2, "performance": 0.05, "cost": 0.15},
    "documentdb": {"pattern_match": 0.4, "complexity": 0.3, "performance": 0.2, "cost": 0.1},
}
ENGINES = tuple(WEIGHTS)


def _rec(engine, table_id, pattern, complexity=80, performance=60, cost=70):
    breakdown = {
        "pattern_match_score": pattern,
        "complexity_score": complexity,
        "performance_score": performance,
        "cost_score": cost,
    }
    return {
        "table_id": table_id,
        "confidence_score": compute_confidence(ScoreBreakdown(**breakdown), WEIGHTS[engine]),
        "score_breakdown": breakdown,
    }


def _analysis(engine, table_patterns, patterns=(), performance=60):
    return {
        "table_recommendations": [
            _rec(engine, t, p, performance=performance) for t, p in table_patterns.items()
        ],
        "workload_analysis": {"patterns_detected": list(patterns), "anti_patterns_detected": []},
    }


def _table(name, pk, columns, indexes=()):
    return {
        "table_id": f"d.{name}",
        "table_name": name,
        "schema_name": "d",
        "row_count": 100_000,
        "size_mb": 10.0,
        "primary_key": pk,
        "columns": [{"column_name": c, "data_type": "int"} for c in columns],
        "indexes": [{"index_name": f"ix_{c}", "columns": [c], "is_unique": False} for c in indexes],
        "foreign_keys": None,
    }


def _query(qid, text, tables, qtype="SELECT"):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "tables_accessed": [f"d.{t}" for t in tables],
        "calls_per_second": 1.0,
        "has_joins": len(tables) > 1,
    }


TABLES = [
    _table("topics", ["id"], ["id", "category_id", "user_id", "title", "views"], ["category_id"]),
    _table("categories", ["id"], ["id", "name", "position"]),
    _table("web_crawler_requests", ["id"], ["id", "date", "user_agent", "count"], ["date"]),
    {**_table("flags", ["id"], ["id", "name", "enabled"]), "row_count": 40},
]


def _collector(*queries):
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": list(queries)},
    }


def _score(query, engine, collector, analysis):
    """The query's score, with its cost dimension held fixed: these tests are about the
    pattern dimension (criterion 4b is narrowed to it), and the cost dimension shares an
    engine's standing cost over the queries it could serve (#494), so a query added to
    the workload moves it by design. ``test_per_query_cost.py`` tests the cost dimension,
    including that no other query's pattern moves it."""
    ctx = ScoringContext.from_pipeline({engine: analysis}, None, collector)
    with patch.object(ScoringContext, "servable_queries", lambda self, engine: frozenset()):
        return score_query_for_engine(query, engine, ctx).score


def test_784_more_relational_evidence_on_a_table_never_lowers_aurora():
    """discourse ``web_crawler_requests``: finding one more, weaker relational pattern on
    the table pulled Aurora PostgreSQL's table pattern score 79 -> 78 (a calls-weighted
    mean). The query's own class decides its pattern score now, so it does not drop.
    (Performance 62 makes the table's 1-point pattern drop cross an integer score.)"""
    lookup = _query(
        "wcr", "SELECT * FROM web_crawler_requests WHERE id = $1", ["web_crawler_requests"]
    )
    other = _query(
        "wcr-other",
        "UPDATE web_crawler_requests SET count = count + 1 WHERE date = $1 AND user_agent = $2",
        ["web_crawler_requests"],
        "OTHER",
    )
    before = _analysis("aurora_postgresql", {"d.web_crawler_requests": 79}, performance=62)
    after = _analysis(
        "aurora_postgresql",
        {"d.web_crawler_requests": 78},
        performance=62,
        patterns=[
            {
                "pattern_type": "standard-crud",
                "query_ids": ["wcr-other"],
                "table_ids": ["d.web_crawler_requests"],
            }
        ],
    )
    score_before = _score(lookup, "aurora_postgresql", _collector(lookup), before)
    score_after = _score(lookup, "aurora_postgresql", _collector(lookup, other), after)
    assert score_after == score_before


@given(st.integers(min_value=0, max_value=100), st.integers(min_value=0, max_value=100))
def test_784_no_table_pattern_score_lowers_a_querys_score(before, after):
    lookup = _query(
        "wcr", "SELECT * FROM web_crawler_requests WHERE id = $1", ["web_crawler_requests"]
    )
    collector = _collector(lookup)
    for engine in ENGINES:
        assert _score(
            lookup, engine, collector, _analysis(engine, {"d.web_crawler_requests": after})
        ) == _score(
            lookup, engine, collector, _analysis(engine, {"d.web_crawler_requests": before})
        )


JOIN = _query(
    "join",
    'SELECT "topics".*, "categories"."name" FROM "topics" '
    'INNER JOIN "categories" ON "categories"."id" = "topics"."category_id" '
    'WHERE "topics"."id" = $1',
    ["topics", "categories"],
)


def test_885_a_key_anchored_join_does_not_inherit_a_key_lookup_on_its_other_table():
    """``topics`` + ``categories`` must not take ``categories``' key-lookup 100 on DynamoDB."""
    collector = _collector(JOIN)
    with_lookup = _analysis("dynamodb", {"d.topics": 75, "d.categories": 100})
    without = _analysis("dynamodb", {"d.topics": 75, "d.categories": 60})
    assert _score(JOIN, "dynamodb", collector, with_lookup) == _score(
        JOIN, "dynamodb", collector, without
    )


# Every shape a query on the same tables can take: the classes a new query can add.
SHAPES = [
    ("SELECT * FROM topics WHERE id = $1", ["topics"], "SELECT"),
    ("SELECT * FROM topics WHERE category_id = $1", ["topics"], "SELECT"),
    ("SELECT * FROM topics WHERE category_id = $1 AND id > $2", ["topics"], "SELECT"),
    ("SELECT * FROM topics ORDER BY id LIMIT $1", ["topics"], "SELECT"),
    ("SELECT * FROM topics WHERE title = $1 AND views > $2", ["topics"], "SELECT"),
    ("SELECT category_id, count(*) FROM topics GROUP BY category_id", ["topics"], "SELECT"),
    ("SELECT * FROM topics WHERE title ILIKE '%' || $1 || '%'", ["topics"], "SELECT"),
    (
        "SELECT t.id FROM topics t JOIN categories c ON c.position = t.views",
        ["topics", "categories"],
        "SELECT",
    ),
    (JOIN["query_text"], ["topics", "categories"], "SELECT"),
    ("INSERT INTO topics (id, title) VALUES ($1, $2)", ["topics"], "INSERT"),
    ("UPDATE topics SET views = views + 1 WHERE id = $1", ["topics"], "UPDATE"),
    ("DELETE FROM topics WHERE views < $1", ["topics"], "DELETE"),
    ("SELECT id, name, enabled FROM flags", ["flags"], "SELECT"),  # a small reference read
    ("SELECT * FROM flags WHERE id = $1", ["flags"], "SELECT"),
]


def _shape_query(index, n):
    text, tables, qtype = SHAPES[index]
    return _query(f"q{n}", text, tables, qtype)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(min_value=0, max_value=len(SHAPES) - 1), min_size=1, max_size=4),
    st.integers(min_value=0, max_value=len(SHAPES) - 1),
    st.integers(min_value=0, max_value=100),
    st.integers(min_value=0, max_value=100),
)
def test_4b_adding_a_query_to_a_table_never_changes_another_querys_score(
    existing, added, pattern_before, pattern_after
):
    """Criterion 4b, narrowed to the pattern score: a new query on the same table, and
    whatever pattern score the table's analysis then gives it, leaves every other query's
    score on every engine where it was (complexity, performance and cost held fixed),
    unless the newcomer makes a column a key of its table (#1061) and the query uses that
    column."""
    queries = [_shape_query(i, n) for n, i in enumerate(existing)]
    newcomer = _shape_query(added, len(existing))
    made_keys = _new_workload_keys(_collector(*queries), _collector(*queries, newcomer))
    queries = [q for q in queries if not _uses(q, made_keys)]
    for engine in ENGINES:
        before_analysis = _analysis(
            engine,
            {"d.topics": pattern_before, "d.categories": pattern_before, "d.flags": pattern_before},
        )
        after_analysis = _analysis(
            engine,
            {
                "d.topics": pattern_after,
                "d.categories": 100 - pattern_after,
                "d.flags": 100 - pattern_after,
            },
        )
        before = _collector(*queries)
        after = _collector(*queries, newcomer)
        for query in queries:
            assert _score(query, engine, after, after_analysis) == _score(
                query, engine, before, before_analysis
            ), (engine, query["query_text"], newcomer["query_text"])


def _new_workload_keys(before: dict, after: dict) -> set[tuple[str, str]]:
    """``(table, column)`` pairs the workload makes keys in ``after`` but not ``before``."""

    def pairs(collector: dict) -> set[tuple[str, str]]:
        keys = SchemaKeys.from_collector(collector)
        return {(t, c) for t, k in keys.tables.items() for c in k.workload_keys}

    return pairs(after) - pairs(before)


def _uses(query: dict, columns: set[tuple[str, str]]) -> bool:
    """Whether ``query`` pins, or joins on, one of ``columns``."""
    access = query_access(query, SchemaKeys.from_collector(_collector(query)), "postgresql")
    for scope in access.scopes:
        used = {(t, c) for t, cols in scope.pinned.items() for c in cols}
        used |= {(t, c) for a, ca, b, cb in scope.joined for t, c in ((a, ca), (b, cb))}
        if used & columns:
            return True
    return False


def test_4b_the_one_exception_a_second_lookup_makes_its_column_a_key():
    """The exception the property above allows, and only it (#1061): a second, different
    lookup of ``topics`` by ``title`` (no index or foreign key on it) makes ``title`` a
    key, so the first one becomes a key-scoped read; a query that does not use ``title``
    keeps its score. A second copy of the first lookup is no second lookup (#1104)."""
    first = _shape_query(4, 0)  # title = $1 AND views > $2
    copy = _shape_query(4, 1)
    second = _query("q1", "SELECT id FROM topics WHERE title = $1", ["topics"], "SELECT")
    bystander = _shape_query(1, 2)  # category_id = $1
    analysis = _analysis("dynamodb", {"d.topics": 50, "d.categories": 50, "d.flags": 50})
    alone = _collector(first, bystander)
    both = _collector(first, bystander, second)
    assert _new_workload_keys(alone, _collector(first, bystander, copy)) == set()
    assert _new_workload_keys(alone, both) == {("d.topics", "title")}
    # As written, the second lookup makes ``first`` key scoped. Routing reads it after
    # redesign since #1231 (ADR-031 Decision 2 Amendment 13): alone, the redesign assumes
    # the key on title that the second lookup supplies, so the score is the same either
    # way, and the record says which (an assumed key alone, none with the second lookup).
    alone_model = QueryPatternModel.from_pipeline(alone, {})
    both_model = QueryPatternModel.from_pipeline(both, {})
    assert alone_model.written_pattern_score(first, "dynamodb") < both_model.written_pattern_score(
        first, "dynamodb"
    )
    found = alone_model.redesign(first)
    assert found is not None and found.labels == ("topics.title",)
    assert both_model.redesign(first) is None
    assert _score(first, "dynamodb", both, analysis) == _score(first, "dynamodb", alone, analysis)
    assert _score(bystander, "dynamodb", both, analysis) == _score(
        bystander, "dynamodb", alone, analysis
    )
