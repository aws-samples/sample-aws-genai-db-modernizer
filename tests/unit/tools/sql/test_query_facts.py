"""The engine-neutral query facts (#491): one detector per fact, read by every gate.

The capability gate (``capability_registry``) and the exclusion gate
(``engine_exclusions``) both read ``src.tools.sql.query_facts``; neither keeps a
detector of its own, so a fact such as "leading-wildcard search" can't be
encoded twice and drift (#485).
"""

from __future__ import annotations

import ast
from functools import cached_property
from pathlib import Path

import pytest

from src.agents.referee.capability_registry import detect_required_capabilities
from src.agents.referee.engine_exclusions import check_exclusions
from src.tools.sql import query_facts as query_facts_module
from src.tools.sql.query_facts import QueryFacts, query_facts
from tests.unit.tools.sql._timing import assert_fast

_REPO = Path(__file__).resolve().parents[4]
# Every module whose detectors moved into ``src.tools.sql.query_facts``.
_READERS = (
    _REPO / "src" / "agents" / "referee" / "capability_registry.py",
    _REPO / "src" / "agents" / "referee" / "engine_exclusions.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_common_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_common_pattern_catalog.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_mysql_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_mysql_pattern_catalog.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_pg_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "aurora_pg_pattern_catalog.py",
    _REPO / "src" / "tools" / "analysis" / "dynamodb_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "documentdb_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "opensearch_analysis_tools.py",
    _REPO / "src" / "tools" / "analysis" / "redis_analysis_tools.py",
    _REPO / "src" / "agents" / "referee" / "triage.py",
    _REPO / "src" / "agents" / "referee" / "reality_check_floors.py",
    _REPO / "src" / "agents" / "referee" / "aurora_choice.py",
)

# Methods that would scan query text in place: a regex match, or a substring
# count or prefix/suffix test.
_SCAN_METHODS = frozenset(
    {"search", "match", "fullmatch", "findall", "finditer", "count", "startswith", "endswith"}
)


# The names the readers give a query's text, raw or lower-cased.
_QUERY_TEXT_NAMES = frozenset({"text", "text_lower", "query_text", "lower_text", "sql"})


def _reads_query_text(node: ast.AST) -> bool:
    """Whether an expression reads a query's text: one of ``_QUERY_TEXT_NAMES``,
    or a ``"query_text"`` key (``q.get("query_text")``, ``q["query_text"]``)."""
    return any(
        (isinstance(n, ast.Name) and n.id in _QUERY_TEXT_NAMES)
        or (isinstance(n, ast.Constant) and n.value == "query_text")
        for n in ast.walk(node)
    )


def _inline_detectors(tree: ast.AST) -> list[str]:
    """Inline detectors on query text: ``"like '%" in text_lower``,
    ``kw in text``, ``text.count(" join ")``, ``SOME_RE.search(text)``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            if any(_reads_query_text(c) for c in node.comparators):
                found.append(ast.unparse(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _SCAN_METHODS and (
                _reads_query_text(node.func.value) or any(_reads_query_text(a) for a in node.args)
            ):
                found.append(ast.unparse(node))
    return found


# Keyword tables a reader may still import: they read table and column names,
# not query text.
_NON_QUERY_TEXT_TABLES = frozenset({"STALENESS_TABLE_KEYWORDS"})


def _imported_detectors(tree: ast.AST) -> set[str]:
    """Keyword tables and regexes a module imports from elsewhere (a catalog
    module, another reader) instead of reading the query facts."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name.endswith(("_KEYWORDS", "_RE")) and alias.name not in _NON_QUERY_TEXT_TABLES
    }


def _module_level_names(tree: ast.Module) -> set[str]:
    return {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }


@pytest.mark.parametrize("path", _READERS, ids=lambda p: p.name)
def test_readers_keep_no_detector_of_their_own(path):
    """No ``re`` import, no keyword table or regex of its own or imported from
    another module, and no inline regex or substring check on query text: every
    detector a reader needs is a query fact."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "re" not in imported
    assert not {n for n in _module_level_names(tree) if n.endswith(("_KEYWORDS", "_RE"))}
    assert _inline_detectors(tree) == []
    assert _imported_detectors(tree) == set()


@pytest.mark.parametrize(
    "source",
    [
        'def f(q):\n    text_lower = q.lower()\n    return "like \'%" in text_lower',
        "def f(text):\n    return any(kw in text for kw in KWS)",
        "def f(q):\n    return q.get('query_text', '').count(' join ') >= 2",
        "def f(text):\n    return bool(SOME_RE.search(text))",
        "def f(q):\n    return (q.get('query_text') or '').lower().startswith('select')",
    ],
)
def test_the_guard_catches_an_inline_detector(source):
    assert _inline_detectors(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "from src.tools.analysis.opensearch_pattern_catalog import FULLTEXT_KEYWORDS",
        "from src.agents.referee.cache_overlay import TOP_N_RE",
    ],
)
def test_the_guard_catches_an_imported_detector(source):
    assert _imported_detectors(ast.parse(source))


def test_the_guard_ignores_checks_on_other_values():
    source = "def f(table_id, col):\n    return 'log' in table_id.lower() or col in COLUMNS"
    assert _inline_detectors(ast.parse(source)) == []
    table_names = (
        "from src.tools.analysis.opensearch_pattern_catalog import STALENESS_TABLE_KEYWORDS"
    )
    assert _imported_detectors(ast.parse(table_names)) == set()


def _fact_names() -> list[str]:
    """Every fact :class:`QueryFacts` computes (its public cached properties)."""
    return [
        name
        for name, value in vars(QueryFacts).items()
        if isinstance(value, cached_property) and not name.startswith("_")
    ]


class TestFacts:
    @pytest.mark.parametrize(
        "sql,fact",
        [
            ("SELECT * FROM t WHERE a LIKE '%x'", "leading_wildcard_like"),
            ("SELECT * FROM t WHERE a ILIKE '%x'", "leading_wildcard_like"),
            ("SELECT * FROM t WHERE MATCH (a) AGAINST ('x')", "fulltext_match"),
            ("SELECT * FROM t WHERE v @@ to_tsquery('x')", "text_search_operator"),
            ("SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t", "ranking_window_function"),
            ("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r", "recursive_cte"),
            ("SELECT `COUNT` (id) FROM t", "aggregate_function_call"),
            ("SELECT SQL_CALC_FOUND_ROWS * FROM t", "sql_calc_found_rows"),
            ("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "window_function"),
            ("SELECT a FROM t GROUP BY a", "group_by"),
            ("SELECT a FROM t GROUP BY a", "dedup_only_group_by"),
            ("SELECT * FROM g JOIN u ON lower(g.name) = u.name_lower", "computed_join_key"),
            ("INSERT INTO t SELECT * FROM u", "write_with_subquery"),
            ("SELECT a, count(*) FROM t", "aggregation_keyword"),
            ("SELECT * FROM t WHERE NOT EXISTS (SELECT 1 FROM u)", "exists_or_in_subquery"),
            ("SELECT * FROM t WHERE a IN ( SELECT b FROM u)", "exists_or_in_subquery"),
            ("SELECT * FROM t LIMIT 10", "pagination_keyword"),
            ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", "pagination_keyword"),
            ("SELECT * FROM t WHERE id IN (1, 2)", "in_list"),
            ("SELECT * FROM t WHERE a ILIKE '%x'", "like_percent_literal"),
            ("SELECT * FROM t WHERE to_tsvector(a) @@ q", "text_search_keyword"),
            ("SELECT JSON_EXTRACT(doc, '$.a') FROM t", "json_function_keyword"),
            ("SELECT doc->'a' FROM t", "json_function_keyword"),
            ("SELECT * FROM t WHERE MATCH (a) AGAINST ('x')", "match_against_keyword"),
            ("INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 1", "duplicate_key_upsert"),
            ("REPLACE INTO t VALUES (1)", "duplicate_key_upsert"),
            ("SELECT GROUP_CONCAT(a) FROM t", "group_concat_call"),
            ("CALL refresh_stats()", "stored_routine_keyword"),
            ("SELECT RANK() OVER (ORDER BY a) FROM t", "window_function_keyword"),
            ("SELECT lag(a, 1) FROM t", "window_function_keyword"),
            ("SELECT doc->>'a' FROM t", "jsonb_operation"),
            ("SELECT * FROM t WHERE doc @> '{\"a\": 1}'", "jsonb_operation"),
            ("SELECT * FROM t WHERE id = ANY(ARRAY[1, 2])", "array_operation"),
            ("SELECT unnest(tags) FROM t", "array_operation"),
            ("SELECT ts_rank(v, q) FROM t", "tsvector_search"),
            ("SELECT * FROM t JOIN LATERAL (SELECT 1) x ON true", "lateral_join"),
            ("INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING", "on_conflict_or_returning"),
            ("INSERT INTO t VALUES (1) RETURNING id", "on_conflict_or_returning"),
            ("SELECT * FROM t WHERE created_at > ?", "timestamp_column_word"),
            ("SELECT * FROM t WHERE last_seen_at > ?", "timestamp_column_word"),
            ("SELECT * FROM sessions WHERE token = ?", "session_word"),
            ("SELECT count(id) FROM t", "aggregation_call_word"),
            ("SELECT a FROM t GROUP BY a", "aggregation_call_word"),
            ("SELECT * FROM t WHERE a BETWEEN 1 AND 2 ORDER BY a", "range_with_order_by"),
            ("SELECT * FROM t WHERE a>=1 ORDER BY a", "range_with_order_by"),
            ("SELECT * FROM t LIMIT 5", "limit_word"),
            ("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t", "window_function_word"),
            ("SELECT RANK() OVER (ORDER BY a) FROM t", "set_window_fields_shape"),
            ("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "set_window_fields_shape"),
            ("SELECT LAG(a) OVER (ORDER BY a) FROM t", "offset_or_bucket_window_call"),
            ("SELECT * FROM t WHERE a NOT IN (1)", "negation_word"),
            ("SELECT * FROM t WHERE a!=b", "negation_word"),
            ("SELECT (SELECT 1 FROM u WHERE u.id = t.uid) FROM t", "correlated_subquery_shape"),
            ("SELECT JSON_EXTRACT(doc, '$.a') FROM t", "json_extract_word"),
            ("SELECT a, b, count(id) FROM t GROUP BY a, b", "group_by_substring"),
            ("SELECT * FROM t WHERE v @@ q", "fulltext_keyword"),
            ("SELECT * FROM t WHERE a LIKE ?", "like_keyword"),
            ("SELECT * FROM t WHERE a ~* 'x'", "regex_keyword"),
            ("SELECT similarity(a, 'x') FROM t", "fuzzy_keyword"),
            ("SELECT * FROM t WHERE event_time >= ?", "timestamp_keyword"),
            ("SELECT * FROM t WHERE event_time >= ?", "time_range_operator"),
            ("SELECT date_trunc('day', ts) FROM t", "time_function_keyword"),
            ("SELECT * FROM t WHERE user_id = ?", "session_or_user_keyword"),
            ("SELECT * FROM t ORDER BY a", "order_by_substring"),
            ("SELECT * FROM t WHERE updated_at > ?", "timestamp_or_date_keyword"),
            ("SELECT ST_Distance(a, b) FROM t", "geospatial_keyword"),
            ("SELECT MAX(id) FROM t", "min_max_call"),
            ("SELECT * FROM t WHERE status = ?", "status_equals_value"),
            ("SELECT * FROM t WHERE EXISTS (SELECT 1)", "exists_call"),
            ("SELECT * FROM t WHERE a IS NULL", "is_null"),
            ("SELECT COUNT(*) FROM t", "count_star_or_one"),
            ("SELECT * FROM t WHERE event_date > ?", "event_timestamp_word"),
            ("SELECT * FROM t WHERE a LIKE ?", "like_pattern_value"),
            ("SELECT * FROM t WHERE MATCH AGAINST", "fulltext_word"),
            ("SELECT * FROM t WHERE a RLIKE 'x'", "regexp_word"),
            ("SELECT * FROM t WHERE similarity(a, 'x') > 0.3", "similarity_or_trigram"),
            ("SELECT id FROM themes WHERE user_selectable", "select_word_repeated"),
            ("SELECT * FROM t ORDER BY score DESC LIMIT 10", "top_n_shape"),
            ("SELECT TOP (5) * FROM t ORDER BY a", "top_n_shape"),
            ("SELECT * FROM sessions WHERE token = ?", "session_key"),
            ("SELECT * FROM [UserSession] WHERE [SessionToken] = @t", "session_key"),
            ("SELECT * FROM t WHERE v @@ plainto_tsquery($1)", "search_depth"),
            ("SELECT a, count(*) FROM t WHERE a LIKE $1 GROUP BY a", "search_depth"),
            ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "merge_statement"),
            ("SELECT JSON_VALUE(doc, '$.a') FROM t", "json_function_call"),
            ("SELECT * FROM t FOR JSON PATH", "json_function_call"),
            (
                "WITH tree AS (SELECT id FROM n UNION ALL SELECT n.id FROM n JOIN tree ON 1=1) "
                "SELECT * FROM tree",
                "self_referencing_recursive_cte",
            ),
        ],
    )
    def test_each_fact_is_detected(self, sql, fact):
        assert getattr(query_facts(sql), fact) is True

    def test_empty_and_none_text_have_no_facts(self):
        empty = query_facts(None)
        assert empty is query_facts("")
        assert not any(getattr(empty, name) for name in _fact_names())

    @pytest.mark.parametrize(
        "sql,needs",
        [
            ("SELECT COUNT(*) FROM t", True),
            ("SELECT a FROM t GROUP BY a", False),  # a dedup-only GROUP BY (#336)
            ("SELECT a FROM t GROUP BY a HAVING a > 1", True),
            ("SELECT a, ROW_NUMBER() OVER (ORDER BY a) FROM t", True),
            ("SELECT a FROM t", False),
        ],
    )
    def test_needs_aggregation(self, sql, needs):
        assert query_facts(sql).needs_aggregation is needs

    @pytest.mark.parametrize(
        "sql,fact",
        [
            ("SELECT a AS returning FROM t", "on_conflict_or_returning"),
            ("SELECT * FROM t WHERE a @> ARRAY[1]", "jsonb_operation"),
            ("SELECT * FROM t WHERE a LIKE  '%x'", "like_percent_literal"),  # two spaces
            ("SELECT * FROM t WHERE a = 'x'", "in_list"),
            ("SELECT fooWITH RECURSIVE", "recursive_cte"),
            ("SELECT count(*) FROM t", "aggregation_call_word"),  # no word after "("
            ("SELECT * FROM t WHERE a >= 1 ORDER BY a", "range_with_order_by"),  # space
            ("SELECT * FROM t WHERE a BETWEEN 1 AND 2", "range_with_order_by"),  # no ORDER BY
            ("SELECT * FROM t WHERE mydate_x = ?", "timestamp_column_word"),
            ("SELECT * FROM t WHERE a != 1", "negation_word"),  # space before !=
            ("SELECT a FROM t GROUP  BY a", "group_by_substring"),  # two spaces
            ("SELECT * FROM t WHERE a LIKE\n'x'", "like_keyword"),  # newline, no space
            ("SELECT * FROM t WHERE a > ?", "time_range_operator"),
            ("SELECT * FROM t WHERE status = 1", "status_equals_value"),  # a bare number
            ("SELECT COUNT(id) FROM t", "count_star_or_one"),
            ("SELECT * FROM t WHERE a LIKE CONCAT('%', ?)", "like_pattern_value"),
            ("SELECT * FROM t LIMIT 10", "top_n_shape"),  # no ORDER BY
            ("SELECT * FROM t WHERE [SessionTimeout] > 5", "session_key"),
            ("SELECT * FROM t WHERE a LIKE $1", "search_depth"),  # a plain LIKE filter
            ("SELECT * FROM t WHERE a = 'x' -- MERGE INTO t USING s", "merge_statement"),
            ("SELECT json_value FROM t", "json_function_call"),  # a column, not a call
            (
                "WITH a AS (SELECT 1 UNION SELECT 2) SELECT * FROM a",
                "self_referencing_recursive_cte",
            ),
        ],
    )
    def test_near_misses_are_not_the_fact(self, sql, fact):
        assert getattr(query_facts(sql), fact) is False

    def test_recursive_cte_needs_a_word_boundary_before_with(self):
        # One definition for the capability gate and the Aurora PostgreSQL
        # recursive-CTE pattern (#491); the gate's copy had no leading \b.
        assert query_facts("with recursive r as (select 1) select * from r").recursive_cte

    def test_counts(self):
        facts = query_facts("SELECT a, b, count(id), max(id) FROM t GROUP BY a, b")
        assert (facts.aggregate_call_word_count, facts.comma_count) == (3, 4)
        joined = query_facts("SELECT * FROM a JOIN b ON 1=1 JOIN c ON 1=1\nJOIN d ON 1=1")
        assert joined.join_with_spaces_count == 2

    def test_join_keyword_count_reads_every_join_word(self):
        assert query_facts("SELECT * FROM a JOIN b ON 1=1 -- join c").join_keyword_count == 2

    def test_facts_are_cached_per_text(self):
        sql = "SELECT * FROM cached_t WHERE a LIKE '%x'"
        assert query_facts(sql) is query_facts(sql)


class TestOneFactBothGates:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM t WHERE a LIKE '%x'",
            'SELECT * FROM t WHERE a LIKE "%x"',
            "SELECT * FROM t WHERE a NOT ILIKE '%x'",
            "SELECT * FROM t WHERE a LIKE 'x%'",
            "SELECT * FROM t WHERE a LIKE ?",
            "SELECT * FROM t",
        ],
    )
    def test_dynamodb_exclusion_and_text_search_capability_agree(self, sql):
        excluded = check_exclusions("q", sql, "dynamodb") is not None
        needs_text_search = "inverted_index" in detect_required_capabilities(sql, [])
        assert excluded is needs_text_search is query_facts(sql).leading_wildcard_like


# ---------------------------------------------------------------------------
# Lazy facts (#895 review): a reader pays only for the facts it reads
# ---------------------------------------------------------------------------


def _uncorrelated_subqueries(n_chars: int) -> str:
    """Many ``IN (SELECT ... WHERE t.c = ?)`` subqueries, none correlated: the
    text the DocumentDB correlated-subquery shape scans super-linearly."""
    part = "x IN (SELECT a FROM t WHERE t.c = ?) UNION ALL "
    return "SELECT * FROM t WHERE " + part * (n_chars // len(part))


def _read_non_documentdb_facts(text: str) -> None:
    """What triage, the gates, assignment, the reality check and the other
    analyses read (every step but the DocumentDB analysis)."""
    detect_required_capabilities(text, [])
    for engine in ("dynamodb", "opensearch", "elasticache"):
        check_exclusions("q", text, engine)
    facts = query_facts(text)
    for name in _fact_names():
        if name not in _DOCUMENTDB_ONLY_FACTS:
            getattr(facts, name)


# Read by the DocumentDB analysis alone.
_DOCUMENTDB_ONLY_FACTS = frozenset(
    {
        "aggregate_call_word_count",
        "window_function_word",
        "set_window_fields_shape",
        "offset_or_bucket_window_call",
        "negation_word",
        "correlated_subquery_shape",
        "json_extract_word",
        "comma_count",
    }
)


def test_a_fact_is_computed_only_when_read(monkeypatch):
    class _NeverSearched:
        def search(self, _text):
            raise AssertionError("the correlated-subquery shape was computed")

    monkeypatch.setattr(query_facts_module, "_CORRELATED_SUBQUERY_SHAPE_RE", _NeverSearched())
    text = _uncorrelated_subqueries(2_000) + " -- lazy"
    _read_non_documentdb_facts(text)
    with pytest.raises(AssertionError, match="correlated-subquery shape"):
        assert query_facts(text).correlated_subquery_shape is None


def test_a_fact_is_computed_once_per_text(monkeypatch):
    calls = []
    real = query_facts_module._has_computed_join_key

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(query_facts_module, "_has_computed_join_key", counting)
    text = "SELECT * FROM g JOIN u ON lower(g.name) = u.n -- once"
    assert query_facts(text).computed_join_key
    assert query_facts(text).computed_join_key
    assert detect_required_capabilities(text, []) == ["computed_join"]
    assert calls == [text]


@pytest.mark.slow
def test_long_queries_cost_the_steps_that_skip_documentdb_no_more_than_before():
    """Five 16,000-character queries with many uncorrelated subqueries. Before
    the facts were lazy, every step paid for the DocumentDB correlated-subquery
    scan (about 3 s per query, the #895 review measured); before #895 added it,
    the facts these steps read took about 7.5 ms per query. The budget, 0.25 s
    CPU for all five, keeps that within a small factor and fails the eager
    version at once."""
    texts = [_uncorrelated_subqueries(16_000) + f" -- {i}" for i in range(5)]

    def run() -> None:
        query_facts_module._query_facts.cache_clear()
        for text in texts:
            _read_non_documentdb_facts(text)

    assert_fast(run, 0.25)
