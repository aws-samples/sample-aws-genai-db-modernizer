"""The text facts and the utility check read the statement as its engine runs it (#1415).

Each used to scan the raw text, so a keyword inside a string literal or a comment read
as SQL: a point read with ``SQL_CALC_FOUND_ROWS`` or ``information_schema`` in a value
needed aggregation and was pinned as an admin statement. They now read
``sql_spans.code_text``: the dialect's comments blanked, each literal a ``?``, quoted
identifiers kept. The corpus (``tests/unit/corpus``) checks every fact against every
hiding place; these pin the helper itself and what the facts must keep seeing.
"""

from __future__ import annotations

import pytest

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement
from src.shared.sql_spans import NEUTRAL_NAME, code_text, keyword_text
from src.tools.sql.query_facts import QueryFacts


class TestCodeText:
    def test_a_literal_becomes_a_placeholder_and_a_comment_a_space(self):
        text = "SELECT id FROM t /* JOIN u */ WHERE note = 'IS NULL' -- COUNT(*)\nAND id = 1"
        assert code_text(text, "postgresql") == "SELECT id FROM t   WHERE note = ?  \nAND id = 1"

    def test_a_doubled_quote_stays_inside_its_literal(self):
        assert code_text("SELECT 'it''s IN (1)' AS a", "oracle") == "SELECT ? AS a"

    @pytest.mark.parametrize("engine", ["postgresql", "sqlserver", "oracle", ""])
    def test_a_double_quoted_name_is_an_identifier_outside_mysql(self, engine):
        text = 'SELECT "latitude" FROM t'
        assert code_text(text, engine) == text

    def test_mysql_reads_double_quotes_as_a_string(self):
        assert code_text('SELECT id FROM t WHERE a = "IS NULL"', "mysql") == (
            "SELECT id FROM t WHERE a = ?"
        )

    def test_a_backtick_identifier_is_kept_and_hides_no_comment(self):
        text = "SELECT `order#items` FROM t"
        assert code_text(text, "mysql") == text

    def test_a_hash_is_a_comment_in_mysql_only(self):
        assert code_text("SELECT a # MIN(b)\nFROM t", "mysql") == "SELECT a  \nFROM t"
        assert code_text("SELECT a # b FROM t", "postgresql") == "SELECT a # b FROM t"

    def test_a_dollar_quoted_string_is_a_literal_in_postgresql(self):
        assert code_text("SELECT $x$ COUNT(*) $x$ AS n", "postgresql") == "SELECT ? AS n"
        # A parameter is not an opener.
        assert code_text("SELECT a FROM t WHERE b = $1", "postgresql") == (
            "SELECT a FROM t WHERE b = $1"
        )

    def test_empty_and_none(self):
        assert code_text("", "mysql") == ""
        assert code_text(None, None) == ""  # type: ignore[arg-type]


# A keyword only inside a literal or a comment: each fact is false.
_HIDDEN = {
    "found_rows": "SQL_CALC_FOUND_ROWS",
    "needs_aggregation": "FOUND_ROWS()",
    "exists_or_in_subquery": "EXISTS (SELECT 1)",
    "in_list": "IN (1, 2)",
    "duplicate_key_upsert": "ON DUPLICATE KEY UPDATE",
    "stored_routine_keyword": "CALL p()",
    "array_operation": "unnest(a)",
    "on_conflict_or_returning": "RETURNING id",
    "negation_word": "a NOT IN b",
    "time_function_keyword": "date_trunc",
    "geospatial_keyword": "ST_Distance",
    "min_max_call": "MAX(a)",
    "status_equals_value": "status = ?",
    "is_null": "IS NULL",
    "count_star_or_one": "COUNT(*)",
    "session_key": "session token",
    "search_depth": "to_tsquery(a)",
}


@pytest.mark.parametrize("fact", sorted(_HIDDEN))
@pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "oracle"])
def test_a_keyword_in_a_literal_or_comment_is_no_fact(fact, engine):
    words = _HIDDEN[fact].replace("'", "''")
    for text in (
        f"SELECT id FROM t WHERE note = '{words}'",
        f"SELECT id FROM t -- {_HIDDEN[fact]}\nWHERE id = 1",
        f"SELECT /* {_HIDDEN[fact]} */ id FROM t WHERE id = 1",
    ):
        assert getattr(QueryFacts(text, engine), fact) is False, text


def test_write_with_subquery_ignores_a_leading_comment():
    text = "/* DELETE FROM u WHERE a IN (SELECT 1) */ SELECT id FROM t WHERE id = 1"
    assert QueryFacts(text, "mysql").write_with_subquery is False
    assert QueryFacts("DELETE FROM u WHERE a IN (SELECT b FROM v)", "mysql").write_with_subquery


# What the facts must keep seeing: the same keyword in the statement itself, a value
# after an operator, a quoted identifier's name.
@pytest.mark.parametrize(
    ("fact", "text", "engine"),
    [
        ("found_rows", "SELECT SQL_CALC_FOUND_ROWS id FROM t LIMIT 5", "mysql"),
        ("found_rows", "SELECT `FOUND_ROWS`()", "mysql"),
        ("status_equals_value", "SELECT id FROM t WHERE status = 'active'", "postgresql"),
        ("status_equals_value", 'SELECT id FROM t WHERE status = "active"', "mysql"),
        ("status_equals_value", "SELECT id FROM t WHERE status = ?", "mysql"),
        ("geospatial_keyword", 'SELECT "latitude" FROM places', "postgresql"),
        ("geospatial_keyword", "SELECT `longitude` FROM places", "mysql"),
        ("session_key", "SELECT * FROM s WHERE [SessionToken] = @p", "sqlserver"),
        ("session_key", "SELECT * FROM sessions WHERE session_id = 'abc'", "mysql"),
        ("time_function_keyword", "SELECT date('now')", "postgresql"),
        ("count_star_or_one", "SELECT COUNT(*) FROM t WHERE a = 'x'", "oracle"),
        ("is_null", "SELECT id FROM t WHERE a = 'x' AND b IS NULL", "sqlserver"),
        ("negation_word", "SELECT id FROM t WHERE a NOT IN ('x', 'y')", "postgresql"),
        ("in_list", "SELECT id FROM t WHERE a IN ('x', 'y')", "mysql"),
        (
            "duplicate_key_upsert",
            "INSERT INTO t VALUES ('a') ON DUPLICATE KEY UPDATE b = 1",
            "mysql",
        ),
        ("on_conflict_or_returning", "INSERT INTO t VALUES ('a') RETURNING id", "postgresql"),
        ("array_operation", "SELECT id FROM t WHERE a = ANY(ARRAY['x'])", "postgresql"),
        ("stored_routine_keyword", "CALL refresh_totals('x')", "mysql"),
        ("search_depth", "SELECT id FROM t WHERE title % 'abc'", "postgresql"),
        ("search_depth", "SELECT id FROM t WHERE doc @@ to_tsquery('x')", "postgresql"),
    ],
)
@pytest.mark.passes_on_main
def test_the_fact_in_the_statement_still_counts(fact, text, engine):
    assert getattr(QueryFacts(text, engine), fact) is True


class TestUtilityStatement:
    @pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "oracle"])
    def test_a_catalog_name_in_a_literal_or_comment_is_application_access(self, engine):
        source = SourceScope(engine=engine)
        for text in (
            "SELECT id FROM t WHERE note = 'information_schema'",
            "SELECT id FROM t -- information_schema\nWHERE id = 1",
            "SELECT id FROM t WHERE id = 1 /* pg_catalog */",
        ):
            assert is_utility_statement(text, ["t"], source) is False, text

    @pytest.mark.passes_on_main
    def test_a_catalog_call_in_a_literal_is_not_one(self):
        source = SourceScope(engine="postgresql")
        assert is_utility_statement("SELECT 'nextval(1)' AS a", ["unknown"], source) is False
        assert is_utility_statement("SELECT nextval('s')", ["unknown"], source) is True

    @pytest.mark.passes_on_main
    @pytest.mark.parametrize(
        ("text", "engine"),
        [
            ("SELECT table_name FROM information_schema.tables WHERE a = 'x'", "mysql"),
            ('SELECT * FROM "information_schema"."tables"', "postgresql"),
            ("SELECT * FROM `information_schema`.`tables`", "mysql"),
            ("SELECT $1::regclass::oid", "postgresql"),
            ("SELECT 'x'::regtype", "postgresql"),
        ],
    )
    def test_a_real_catalog_read_is_still_utility(self, text, engine):
        assert is_utility_statement(text, ["unknown"], SourceScope(engine=engine)) is True


class TestAKeywordAsAQuotedNameIsAName:
    """#1478: the facts that look for a keyword read ``sql_spans.keyword_text``, where each
    quoted identifier is a neutral name; the facts that read a name keep it."""

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ('SELECT "where" FROM t', "postgresql"),
            ('SELECT "where" FROM t', "oracle"),
            ("SELECT `where` FROM t", "mysql"),
            ("SELECT [where] FROM t", "sqlserver"),
        ],
    )
    def test_the_issue_repro_has_no_where(self, sql, engine):
        facts = QueryFacts(sql, engine)
        assert not facts.where_word and not facts.keyed_after_where

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ('SELECT "is null", "count(*)", "not in" FROM t', "postgresql"),
            ("SELECT `is null`, `count(*)`, `not in` FROM t", "mysql"),
            ("SELECT [is null], [count(*)], [not in] FROM t", "sqlserver"),
        ],
    )
    def test_no_keyword_fact_reads_a_quoted_name(self, sql, engine):
        facts = QueryFacts(sql, engine)
        assert not (facts.is_null or facts.count_star_or_one or facts.negation_word), sql

    def test_keyword_text_writes_each_quoted_name_as_the_neutral_name(self):
        assert keyword_text('SELECT "where" FROM "app"."t"', "postgresql") == (
            f'SELECT "{NEUTRAL_NAME}" FROM "{NEUTRAL_NAME}"."{NEUTRAL_NAME}"'
        )
        assert keyword_text("SELECT [where] FROM [t]", "sqlserver") == (
            f"SELECT [{NEUTRAL_NAME}] FROM [{NEUTRAL_NAME}]"
        )
        # MySQL's "..." is a string, so it is a placeholder, as in code_text.
        assert keyword_text('SELECT `where` FROM t WHERE a = "x"', "mysql") == (
            f"SELECT `{NEUTRAL_NAME}` FROM t WHERE a = ?"
        )

    @pytest.mark.passes_on_main  # the names the name-reading facts must keep
    def test_a_name_reading_fact_keeps_the_name(self):
        assert QueryFacts('SELECT "latitude" FROM t', "postgresql").geospatial_keyword
        assert QueryFacts("SELECT id FROM t WHERE [SessionToken] = @p", "sqlserver").session_key

    def test_a_quoted_function_name_is_still_a_call(self):
        """A quoted name before ``(`` names the function called, not a column."""
        for sql in ('SELECT "count"(*) FROM t', 'SELECT "count" (*) FROM t'):
            assert keyword_text(sql, "postgresql") == sql
        assert QueryFacts("SELECT `FOUND_ROWS`()", "mysql").found_rows

    @pytest.mark.passes_on_main  # a real keyword next to a quoted name still reads
    def test_a_real_where_after_a_quoted_name_still_reads(self):
        facts = QueryFacts('SELECT "where" FROM t WHERE "id" = $1', "postgresql")
        assert facts.where_word and facts.keyed_after_where
