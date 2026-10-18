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
from src.tools.sql.query_facts import ColumnTypes, QueryFacts, query_facts
from tests.unit.tools.sql._timing import assert_cost_ratio

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
    _REPO / "src" / "agents" / "referee" / "cache_overlay.py",
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
            ("SELECT * FROM t WHERE a LIKE CONCAT('%', ?)", "leading_wildcard_like"),
            ("SELECT * FROM t WHERE a LIKE ?", "like_parameter"),
            ("SELECT * FROM t WHERE a NOT ILIKE ?", "like_parameter"),
            ("SELECT * FROM t WHERE MATCH (a) AGAINST ('x')", "fulltext_search"),
            ("SELECT * FROM t WHERE v @@ to_tsquery('x')", "fulltext_search"),
            ("SELECT ts_rank(v, q) FROM t", "fulltext_search"),
            ("SELECT * FROM t WHERE CONTAINS(a, 'x')", "fulltext_search"),
            ("SELECT * FROM t WHERE a ~* 'x'", "pattern_match"),
            ("SELECT * FROM t WHERE a RLIKE 'x'", "pattern_match"),
            ("SELECT similarity(a, 'x') FROM t", "similarity_match"),
            ("SELECT * FROM t WHERE a LIKE ?", "text_search"),
            ("SELECT * FROM t WHERE a REGEXP 'x'", "text_search"),
            ("SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t", "ranking_window_function"),
            ("SELECT LAG(a, 1) OVER (ORDER BY a) FROM t", "offset_window_function"),
            ("SELECT NTILE(4) OVER (ORDER BY a) FROM t", "offset_window_function"),
            ("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "windowed_aggregate"),
            ("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r", "recursive_cte"),
            (
                "WITH tree AS (SELECT id FROM n UNION ALL SELECT n.id FROM n JOIN tree ON 1=1) "
                "SELECT * FROM tree",
                "recursive_cte",
            ),
            ("SELECT `COUNT` (id) FROM t", "aggregation"),
            ("SELECT COUNT(*) FROM t", "aggregation"),
            ("SELECT a FROM t GROUP BY a", "aggregation"),
            ("SELECT SQL_CALC_FOUND_ROWS * FROM t", "found_rows"),
            ("SELECT FOUND_ROWS()", "found_rows"),
            ("SELECT SUM(x) OVER (PARTITION BY y) FROM t", "window_function"),
            ("SELECT a FROM t GROUP BY a", "group_by"),
            ("SELECT a FROM t GROUP BY a", "dedup_only_group_by"),
            ("SELECT * FROM g JOIN u ON lower(g.name) = u.name_lower", "computed_join_key"),
            ("INSERT INTO t SELECT * FROM u", "write_with_subquery"),
            ("SELECT * FROM t WHERE NOT EXISTS (SELECT 1 FROM u)", "exists_or_in_subquery"),
            ("SELECT * FROM t WHERE a IN ( SELECT b FROM u)", "exists_or_in_subquery"),
            ("SELECT * FROM t LIMIT 10", "row_limit"),
            ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", "row_limit"),
            ("SELECT TOP 5 * FROM t", "row_limit"),
            ("SELECT * FROM t WHERE id IN (1, 2)", "in_list"),
            ("SELECT * FROM t WHERE id IN\n(1, 2)", "in_list"),  # newline before ( (#1010)
            ("SELECT * FROM t WHERE id IN\t(1, 2)", "in_list"),
            ("SELECT * FROM t WHERE id in  (1, 2)", "in_list"),
            ("SELECT * FROM t WHERE (id)IN(1, 2)", "in_list"),  # no space before IN
            ("SELECT * FROM t WHERE id NOT IN (1, 2)", "in_list"),
            ("SELECT JSON_EXTRACT(doc, '$.a') FROM t", "json_access"),
            ("SELECT doc->'a' FROM t", "json_access"),
            ("INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 1", "duplicate_key_upsert"),
            ("REPLACE INTO t VALUES (1)", "duplicate_key_upsert"),
            ("SELECT GROUP_CONCAT(a) FROM t", "string_aggregate_call"),
            ("CALL refresh_stats()", "stored_routine_keyword"),
            ("SELECT doc->>'a' FROM t", "json_access"),
            ("SELECT * FROM t WHERE doc @> '{\"a\": 1}'", "json_access"),
            ("SELECT CAST(doc AS JSON) FROM t", "json_access"),
            ("SELECT * FROM t WHERE id = ANY(ARRAY[1, 2])", "array_operation"),
            ("SELECT unnest(tags) FROM t", "array_operation"),
            ("SELECT * FROM t JOIN LATERAL (SELECT 1) x ON true", "lateral_join"),
            ("INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING", "on_conflict_or_returning"),
            ("INSERT INTO t VALUES (1) RETURNING id", "on_conflict_or_returning"),
            ("SELECT * FROM t WHERE a BETWEEN 1 AND 2 ORDER BY a", "range_predicate"),
            ("SELECT * FROM t WHERE a >= 1 ORDER BY a", "range_predicate"),  # spaced (#933)
            ("SELECT * FROM t WHERE a < ?", "range_predicate"),
            ("SELECT * FROM t WHERE name LIKE 'ab%'", "range_predicate"),  # a prefix
            ("SELECT * FROM t WHERE a NOT IN (1)", "negation_word"),
            ("SELECT * FROM t WHERE a!=b", "negation_word"),
            ("SELECT (SELECT 1 FROM u WHERE u.id = t.uid) FROM t", "correlated_subquery_shape"),
            ("SELECT * FROM t WHERE event_time > ?", "range_predicate"),  # #933: not only >=/<=
            ("SELECT date_trunc('day', ts) FROM t", "time_function_keyword"),
            ("SELECT * FROM t ORDER BY a", "order_by"),
            ("UPDATE a JOIN b ON b.id = a.id SET a.x = ?", "multi_table_write"),
            ("SELECT ST_Distance(a, b) FROM t", "geospatial_keyword"),
            ("SELECT MAX(id) FROM t", "min_max_call"),
            ("SELECT * FROM t WHERE status = ?", "status_equals_value"),
            ("SELECT * FROM t WHERE a IS NULL", "is_null"),
            ("SELECT COUNT(*) FROM t", "count_star_or_one"),
            ("SELECT * FROM t ORDER BY score DESC LIMIT 10", "top_n"),
            ("SELECT TOP (5) * FROM t ORDER BY a", "top_n"),
            ("SELECT * FROM sessions WHERE token = ?", "session_key"),
            ("SELECT * FROM [UserSession] WHERE [SessionToken] = @t", "session_key"),
            ("SELECT * FROM user_sessions WHERE session_token = ?", "session_key"),
            ("SELECT * FROM user_auth_tokens WHERE auth_token = $1", "session_key"),
            ("SELECT * FROM t WHERE v @@ plainto_tsquery($1)", "search_depth"),
            ("SELECT a, count(*) FROM t WHERE a LIKE $1 GROUP BY a", "search_depth"),
            ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "merge_statement"),
            ("SELECT JSON_VALUE(doc, '$.a') FROM t", "json_access"),
            ("SELECT * FROM t WHERE id = ?", "where_word"),
            ("SELECT * FROM t WHERE id IN (1, 2)", "keyed_after_where"),
            (
                "WITH w AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM w",
                "write_statement_text",
            ),
            ("/* job */ TRUNCATE t", "write_statement_text"),
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
            ("SELECT * FROM t WHERE a @> ARRAY[1]", "json_access"),  # an array
            ("SELECT * FROM t WHERE note = 'a->b'", "json_access"),  # a literal
            ("SELECT * FROM t WHERE a LIKE 'x%'", "leading_wildcard_like"),  # a prefix
            ("SELECT * FROM t WHERE a LIKE 'x%'", "text_search"),  # a prefix is a range
            ("SELECT * FROM t -- a LIKE '%x'", "leading_wildcard_like"),  # a comment
            ("SELECT * FROM t WHERE note = 'a LIKE ?'", "like_parameter"),  # a literal
            ("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t", "aggregation"),
            ("SELECT * FROM t ORDER BY rank", "window_function"),  # a column
            ("SELECT rank FROM t", "ranking_window_function"),
            ("SELECT LAG(a) FROM t", "offset_window_function"),  # no OVER
            ("SELECT COUNT(*) FROM t", "windowed_aggregate"),
            ("SELECT * FROM t WHERE a = 'MATCH (b) AGAINST (c)'", "fulltext_search"),
            ("SELECT regexp_replace(a, 'x', 'y') FROM t", "pattern_match"),
            ("SELECT * FROM t -- GROUP BY a", "group_by"),
            ("SELECT count FROM t", "aggregation"),  # a column named count
            ("SELECT * FROM t WHERE a = 'x'", "in_list"),
            ("SELECT * FROM t WHERE a IN", "in_list"),  # no list
            ("SELECT lookup_in (x) FROM t", "in_list"),  # IN ends a longer name
            ("SELECT * FROM t WHERE min (a) > 1", "in_list"),  # IN inside a word
            ("SELECT fooWITH RECURSIVE", "recursive_cte"),
            ("SELECT * FROM t WHERE note = 'lateral (x)'", "lateral_join"),  # a value (#985)
            ("SELECT * FROM t WHERE a <> 1", "range_predicate"),
            ("SELECT CASE WHEN a > 1 THEN 1 END FROM t", "range_predicate"),  # not a filter
            ("SELECT * FROM t WHERE doc->>'a' = ?", "range_predicate"),  # JSON, no range
            ("SELECT * FROM t WHERE credit_limit > 5", "row_limit"),
            ("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t", "order_by"),  # a window's
            ("SELECT * FROM t WHERE a != 1", "negation_word"),  # space before !=
            ("SELECT * FROM t WHERE status = 1", "status_equals_value"),  # a bare number
            ("SELECT COUNT(id) FROM t", "count_star_or_one"),
            ("SELECT * FROM t LIMIT 10", "top_n"),  # no ORDER BY
            ("SELECT TOP (5) * FROM t", "top_n"),
            ("SELECT * FROM t WHERE [SessionTimeout] > 5", "session_key"),
            ("SELECT * FROM t WHERE session_timeout > 5", "session_key"),
            ("SELECT * FROM t WHERE payment_token = ?", "session_key"),
            ("SELECT * FROM t WHERE purpose = 'reset_token'", "session_key"),  # a literal
            ("SELECT * FROM t WHERE event_type = 'login_token'", "session_key"),
            ("SELECT * FROM t WHERE user_id = ?", "session_key"),  # a user, not a session
            ("SELECT login FROM users WHERE id = ?", "session_key"),  # a user name
            ("SELECT * FROM t WHERE a LIKE $1", "search_depth"),  # a plain LIKE filter
            ("SELECT * FROM t WHERE a = 'x' -- MERGE INTO t USING s", "merge_statement"),
            ("SELECT json_value FROM t", "json_access"),  # a column, not a call
            ("WITH a AS (SELECT 1 UNION SELECT 2) SELECT * FROM a", "recursive_cte"),
            ("SELECT * FROM t WHERE a > ?", "keyed_after_where"),  # a range
            ("SELECT * FROM t WHERE a >= ?", "keyed_after_where"),
            ("/* delete from t */ SELECT 1", "write_statement_text"),
        ],
    )
    def test_near_misses_are_not_the_fact(self, sql, fact):
        assert getattr(query_facts(sql), fact) is False

    def test_parsed_facts_are_read_in_the_source_engines_dialect(self, monkeypatch):
        calls = []
        real = query_facts_module.query_readings

        def recording(text, engine):
            calls.append(engine)
            return real(text, engine)

        monkeypatch.setattr(query_facts_module, "query_readings", recording)
        sql = "SELECT * FROM t WHERE a LIKE :1 -- dialect"
        assert query_facts(sql, "Oracle").like_parameter
        assert query_facts(sql, "oracle") is query_facts(sql, "Oracle")
        assert query_facts(sql).like_parameter  # the generic dialect's reading
        assert calls == ["oracle", ""]

    def test_recursive_cte_is_one_fact_for_every_dialect(self):
        # One definition for the capability gate, the Aurora PostgreSQL and
        # DocumentDB patterns and the Aurora engine choice (#893).
        assert query_facts("with recursive r as (select 1) select * from r").recursive_cte
        tsql = (
            "WITH [Tree] AS (SELECT Id FROM Nodes UNION ALL SELECT n.Id FROM Nodes n "
            "JOIN [Tree] t ON n.ParentId = t.Id) SELECT * FROM [Tree]"
        )
        assert query_facts(tsql, "sqlserver").recursive_cte

    def test_counts(self):
        facts = query_facts("SELECT a, b, count(id), max(id) FROM t GROUP BY a, b")
        assert facts.aggregate_function_count == 2
        joined = query_facts("SELECT * FROM a JOIN b ON 1=1 JOIN c ON 1=1\nJOIN d ON 1=1")
        assert joined.join_count == 3

    def test_join_count_is_the_facades(self):
        # A join word in a comment is not a join; a comma-separated FROM list is.
        assert query_facts("SELECT * FROM a JOIN b ON 1=1 -- join c").join_count == 1
        assert query_facts("SELECT * FROM a, b WHERE a.id = b.id").join_count == 1

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
            "SELECT * FROM t WHERE a REGEXP 'x'",
            "SELECT * FROM t",
        ],
    )
    def test_the_dynamodb_exclusion_is_the_leading_wildcard_part_of_text_search(self, sql):
        """The DynamoDB exclusion reads ``leading_wildcard_like``, one of the parts of
        the ``text_search`` fact the capability gate and triage's text_search signal
        read (#893): an excluded query always needs the inverted index."""
        excluded = check_exclusions("q", sql, "dynamodb") is not None
        needs_text_search = "text_pattern_match" in detect_required_capabilities(sql, [])
        facts = query_facts(sql)
        assert excluded is facts.leading_wildcard_like
        assert needs_text_search is facts.text_search
        assert needs_text_search or not excluded


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
    facts.timestamp_column(_COLUMN_TYPES)  # read with the collection's types, as readers do


# The collection the long queries read: ``t`` and its typed columns (#935).
_COLUMN_TYPES = ColumnTypes.from_collector(
    {
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.t",
                    "table_name": "t",
                    "columns": [
                        {"column_name": name, "data_type": "int"} for name in ("a", "c", "x")
                    ],
                }
            ]
        }
    }
)


# Read by the DocumentDB analysis alone.
_DOCUMENTDB_ONLY_FACTS = frozenset(
    {
        "aggregate_function_count",
        "windowed_aggregate",
        "negation_word",
        "correlated_subquery_shape",
    }
)


def test_a_fact_is_computed_only_when_read(monkeypatch):
    def _never_scanned(_text):
        raise AssertionError("the correlated-subquery shape was computed")

    monkeypatch.setattr(query_facts_module, "_has_correlated_subquery_shape", _never_scanned)
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


# The same total text (80,000 characters), as 5 long and as 80 short queries.
_LONG_QUERY_CHARS = 16_000
_SHORT_QUERY_CHARS = 1_000
_SHORT_QUERIES = 5 * _LONG_QUERY_CHARS // _SHORT_QUERY_CHARS
# Limit on long/short CPU time; see the test.
MAX_LONG_TO_SHORT = 3.0


def _run_steps_on(texts: list[str]) -> None:
    query_facts_module._query_facts.cache_clear()
    for text in texts:
        _read_non_documentdb_facts(text)


@pytest.mark.slow
def test_long_queries_cost_the_steps_that_skip_documentdb_no_more_than_before():
    """Five 16,000-character queries with many uncorrelated subqueries. Before
    the facts were lazy, every step paid for the DocumentDB correlated-subquery
    scan (about 3 s per query, the #895 review measured); before #895 added it,
    the facts these steps read took about 7.5 ms per query.

    The budget is a ratio, not seconds: an absolute 0.25 s passed locally (about
    45 ms) and failed on CI (0.26 to 0.32 s), because the runner is 5 to 7 times
    slower. The reference is the same steps on the same text cut into 80 queries
    of 1,000 characters, timed in this process the same way (best of up to 12
    runs of CPU time). Facts that scale with the text read cost the same on both;
    a scan that is super-linear in the query length costs far more on the long
    ones. Measured long/short on a laptop: 0.78 to 0.98 for short queries of 200
    to 2,000 characters. The limit is 3.0, three times the worst of those. The
    eager scan from before the facts were lazy measures about 250 on the same
    workload, and a slow fact added to a step fails it as well.
    """
    long_texts = [_uncorrelated_subqueries(_LONG_QUERY_CHARS) + f" -- {i}" for i in range(5)]
    short_texts = [
        _uncorrelated_subqueries(_SHORT_QUERY_CHARS) + f" -- {i}" for i in range(_SHORT_QUERIES)
    ]

    assert_cost_ratio(
        lambda: _run_steps_on(long_texts), lambda: _run_steps_on(short_texts), MAX_LONG_TO_SHORT
    )


class TestLocateColumn:
    """#942: the one resolution of a column to its collected table, shared by the
    timestamp-column fact and the leading-wildcard LIKE fact."""

    HOLDS = {("a", "x"), ("b", "x"), ("c", "y")}

    @classmethod
    def _locate(cls, use, known=("a", "b", "c")):
        from src.tools.sql.query_facts import locate_column

        return locate_column(
            use,
            lambda ref: ref.name if ref.name in known else None,
            lambda table, name: (table, name) in cls.HOLDS,
        )

    def test_the_one_table_that_holds_it(self):
        from src.tools.sql.facade import ColumnUse, TableRef

        use = ColumnUse("Y", ((TableRef("a"), TableRef("c")),))
        assert self._locate(use) == ("c", "")

    def test_two_holders_are_ambiguous_and_an_outer_scope_is_not_tried(self):
        from src.tools.sql.facade import ColumnUse, TableRef

        use = ColumnUse("x", ((TableRef("a"), TableRef("b")), (TableRef("c"),)))
        assert self._locate(use) == (None, "ambiguous: a, b")

    def test_the_inner_scope_wins_and_an_empty_one_falls_outward(self):
        from src.tools.sql.facade import ColumnUse, TableRef

        use = ColumnUse("y", ((TableRef("a"),), (TableRef("c"),)))
        assert self._locate(use) == ("c", "")

    def test_a_table_the_collection_lacks_and_no_holder_say_why(self):
        from src.tools.sql.facade import ColumnUse, TableRef

        assert self._locate(ColumnUse("x", ((TableRef("ghost"),),))) == (
            None,
            "no collected table ghost",
        )
        assert self._locate(ColumnUse("zzz", ((TableRef("a"),),)), known=("a",)) == (
            None,
            "no collected table holds it",
        )
        assert self._locate(ColumnUse("x", ())) == (None, "no collected table holds it")

    def test_a_regex_read_column_has_no_table(self):
        from src.tools.sql.facade import ColumnUse

        assert self._locate(ColumnUse("x", from_regex=True)) == (None, "statement not parsed")


class TestGroupingSetsFallback:
    """#968 review: the regex fallback reads a grouping list only inside a GROUP BY."""

    def test_a_postgresql_cube_function_is_not_one(self) -> None:
        from src.tools.sql.regex_facts import regex_grouping_sets

        assert not regex_grouping_sets("SELECT cube(1, 2) FROM t GROUP BY a")

    @pytest.mark.parametrize(
        "text",
        [
            "SELECT a, b, COUNT(*) FROM t GROUP BY a, b WITH CUBE",
            "SELECT a, COUNT(*) FROM t GROUP BY a WITH ROLLUP",
            "SELECT a, COUNT(*) FROM t GROUP BY ROLLUP (a)",
            "SELECT a, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), ())",
        ],
    )
    def test_each_grouping_list_is_one(self, text: str) -> None:
        from src.tools.sql.regex_facts import regex_grouping_sets

        assert regex_grouping_sets(text)

    def test_tsql_with_cube_on_the_parsed_path(self) -> None:
        assert query_facts(
            "SELECT a, b, COUNT(*) FROM t GROUP BY a, b WITH CUBE", "sqlserver"
        ).grouping_sets
