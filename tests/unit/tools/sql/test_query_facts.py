"""The engine-neutral query facts (#491): one detector per fact, read by every gate.

The capability gate (``capability_registry``) and the exclusion gate
(``engine_exclusions``) both read ``src.tools.sql.query_facts``; neither keeps a
detector of its own, so a fact such as "leading-wildcard search" can't be
encoded twice and drift (#485).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.agents.referee.capability_registry import detect_required_capabilities
from src.agents.referee.engine_exclusions import check_exclusions
from src.tools.sql.query_facts import QueryFacts, query_facts

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
        ],
    )
    def test_each_fact_is_detected(self, sql, fact):
        assert getattr(query_facts(sql), fact) is True

    def test_empty_and_none_text_have_no_facts(self):
        empty = query_facts(None)
        assert empty == query_facts("")
        assert not any(
            getattr(empty, name)
            for name in QueryFacts.__dataclass_fields__
            if name != "join_keyword_count"
        )
        assert empty.join_keyword_count == 0

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
        ],
    )
    def test_near_misses_are_not_the_fact(self, sql, fact):
        assert getattr(query_facts(sql), fact) is False

    def test_recursive_cte_needs_a_word_boundary_before_with(self):
        # One definition for the capability gate and the Aurora PostgreSQL
        # recursive-CTE pattern (#491); the gate's copy had no leading \b.
        assert query_facts("with recursive r as (select 1) select * from r").recursive_cte

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
