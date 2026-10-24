"""Each dialect's own comment, string and identifier forms, in the one span grammar (#1416).

``sql_spans`` is the one scanner behind ``code_text`` (the text facts),
``strip_comments_and_literals`` and ``scan_sql``. Per source dialect:

- PostgreSQL and SQL Server nest block comments, MySQL and Oracle end one at its
  first ``*/``;
- MySQL and MariaDB end a line at ``#`` and read ``\\`` as an escape in a string;
  PostgreSQL only in an ``E'...'`` string; SQL Server and Oracle never;
- a doubled quote is an escaped quote everywhere;
- PostgreSQL quotes strings with ``$tag$``, and the body of a ``DO`` block or an
  ``AS $$ ... $$`` routine is code, not a string;
- SQL Server quotes identifiers with ``[...]`` (``]]`` escapes a ``]``).

With no dialect known the grammar reads as it did before (``test_sql_spans.py``
pins that against the old regexes).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.agents.referee.aurora_choice import detect_heterogeneous_features
from src.agents.referee.query_shape import locking_clause, readable_sql, skips_locked_rows
from src.shared.sql_spans import code_text
from src.shared.unsupported_pattern import is_dedup_only_group_by, strip_comments_and_literals
from src.tools.sql.query_facts import QueryFacts
from src.tools.sql.regex_facts import regex_recursive_cte, scan_sql
from tests.unit.tools.sql._timing import assert_cost_ratio

_NESTED = "SELECT id FROM t /* a /* b */ JOIN u FOR UPDATE SKIP LOCKED */ WHERE id = 1"


# -- The issue's own reproductions -------------------------------------------------


def test_scan_sql_blanks_a_mysql_hash_comment():
    assert "JOIN" not in scan_sql("SELECT id FROM t # JOIN u\nWHERE id = 1", "mysql")


def test_a_lock_inside_a_nested_postgresql_comment_is_no_lock():
    sql = "SELECT id FROM t /* a /* b */ FOR UPDATE SKIP LOCKED */ WHERE id = 1"
    assert locking_clause(sql, "postgresql") is None
    assert skips_locked_rows(sql, "postgresql") is False


def test_a_bracketed_group_by_is_a_name_not_a_clause():
    assert (
        is_dedup_only_group_by("SELECT [group by] FROM t WHERE [limit] = 1", "sqlserver") is False
    )


@pytest.mark.parametrize("engine", ["sqlserver", "postgresql", "oracle"])
def test_a_trailing_backslash_ends_no_string_outside_mysql(engine):
    sql = "SELECT id FROM t WHERE path = 'C:\\' AND deleted_at IS NULL AND name = 'x'"
    assert QueryFacts(sql, engine).is_null is True


def test_an_apostrophe_inside_a_bracketed_name_opens_no_string():
    sql = "SELECT [O'Brien Notes] FROM t WHERE status = 'x' AND deleted_at IS NULL AND name = 'y'"
    assert QueryFacts(sql, "sqlserver").is_null is True


def test_a_call_inside_a_do_block_is_a_call():
    sql = "DO $$ BEGIN CALL refresh_stats(); END $$"
    assert QueryFacts(sql, "postgresql").stored_routine_keyword is True


def test_a_subquery_inside_a_function_body_is_a_subquery():
    sql = (
        "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 WHERE a IN (SELECT b FROM u) $$ "
        "LANGUAGE sql"
    )
    assert QueryFacts(sql, "postgresql").exists_or_in_subquery is True


# -- Nested block comments ---------------------------------------------------------


@pytest.mark.parametrize("engine", ["postgresql", "postgres", "sqlserver"])
def test_postgresql_and_sql_server_end_a_block_comment_at_its_outer_close(engine):
    for blank in (strip_comments_and_literals, code_text, scan_sql, readable_sql):
        out = blank(_NESTED, engine)
        assert "JOIN" not in out and "SKIP" not in out, (blank.__name__, out)
        assert "WHERE id = 1" in out


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "oracle"])
def test_mysql_and_oracle_end_a_block_comment_at_its_first_close(engine):
    for blank in (strip_comments_and_literals, code_text, scan_sql):
        assert "JOIN u" in blank(_NESTED, engine), blank.__name__


@pytest.mark.passes_on_main  # guards what the fix must not change
def test_an_unterminated_nested_comment_is_plain_text():
    # Not valid PostgreSQL: as ``sql_comments`` reads it, the opener is plain text.
    sql = "SELECT a /* x /* y */ FROM t"
    assert "FROM t" in strip_comments_and_literals(sql, "postgresql")


# -- Backslash escapes ------------------------------------------------------------


@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_mysql_reads_a_backslash_quote_as_an_escape(engine):
    sql = "SELECT id FROM t WHERE note = 'a\\' JOIN u' AND id = 1"
    for blank in (strip_comments_and_literals, code_text, scan_sql):
        out = blank(sql, engine)
        assert "JOIN" not in out and "id = 1" in out, (blank.__name__, out)


@pytest.mark.parametrize("engine", ["postgresql", "sqlserver", "oracle"])
def test_a_backslash_is_an_ordinary_character_elsewhere(engine):
    sql = "SELECT id FROM t WHERE path = 'C:\\' JOIN u ON 1 = 1 WHERE note = 'x'"
    for blank in (strip_comments_and_literals, code_text, scan_sql):
        assert "JOIN u" in blank(sql, engine), blank.__name__


@pytest.mark.parametrize("prefix", ["E", "e"])
def test_a_postgresql_escape_string_reads_a_backslash_quote_as_an_escape(prefix):
    sql = f"SELECT id FROM t WHERE note = {prefix}'a\\' JOIN u' AND id = 1"
    for blank in (strip_comments_and_literals, code_text, scan_sql):
        out = blank(sql, "postgresql")
        assert "JOIN" not in out and "id = 1" in out, (blank.__name__, out)
    assert code_text(sql, "postgresql") == "SELECT id FROM t WHERE note = ? AND id = 1"


def test_an_e_ending_a_name_opens_no_escape_string():
    # ``note'...'`` is no E-string: the ``e`` belongs to the name before it.
    sql = "SELECT id FROM t WHERE name = 'C:\\' AND deleted_at IS NULL AND note = 'x'"
    assert QueryFacts(sql, "postgresql").is_null is True


@pytest.mark.passes_on_main  # guards what the fix must not change
@pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "oracle", None])
def test_a_doubled_quote_is_an_escape_everywhere(engine):
    sql = "SELECT id FROM t WHERE note = 'it''s a JOIN u' AND id = 1"
    out = strip_comments_and_literals(sql, engine)
    assert "JOIN" not in out and "id = 1" in out


# -- SQL Server bracket identifiers -----------------------------------------------


def test_sql_server_blanks_a_bracket_identifier_and_keeps_it_as_code():
    sql = "SELECT [O'Brien]] Notes] FROM t WHERE [group by] = 'x' AND c IS NULL"
    assert "group" not in strip_comments_and_literals(sql, "sqlserver")
    assert "IS NULL" in strip_comments_and_literals(sql, "sqlserver")
    assert code_text(sql, "sqlserver") == (
        "SELECT [O'Brien]] Notes] FROM t WHERE [group by] = ? AND c IS NULL"
    )
    # scan_sql reads the bare name, ``]]`` unescaped.
    assert (
        scan_sql(sql, "sqlserver")
        == "SELECT O'Brien] Notes FROM t WHERE group by =   AND c IS NULL"
    )


@pytest.mark.passes_on_main  # guards what the fix must not change
@pytest.mark.parametrize("engine", ["mysql", "postgresql", "oracle"])
def test_brackets_are_no_identifier_in_the_literal_grammar_elsewhere(engine):
    # PostgreSQL's ``ARRAY[...]`` and ``a[1]`` are subscripts, not names.
    sql = "SELECT a[1] FROM t WHERE b = ANY(ARRAY[1, 2])"
    assert strip_comments_and_literals(sql, engine) == sql


# -- scan_sql reads the dialect ---------------------------------------------------


def test_scan_sql_blanks_postgresql_dollar_quoted_strings():
    sql = "SELECT $t$ a $$ GROUP_CONCAT(x) $$ b $t$ AS note FROM t"
    assert "GROUP_CONCAT" not in scan_sql(sql, "postgresql")
    assert "GROUP_CONCAT" in scan_sql(sql, "mysql")


def test_scan_sql_still_reads_a_national_string_as_one_literal():
    assert scan_sql("SELECT N'x' FROM [t]", "sqlserver") == "SELECT   FROM t"


def test_string_aggregate_call_ignores_a_mysql_hash_comment():
    assert QueryFacts("SELECT a FROM t # GROUP_CONCAT(b)\n", "mysql").string_aggregate_call is False
    assert QueryFacts("SELECT GROUP_CONCAT(b) FROM t # x\n", "mysql").string_aggregate_call is True


# -- PostgreSQL routine bodies ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN CALL refresh_stats(); END $$",
        "DO LANGUAGE plpgsql $body$ BEGIN CALL refresh_stats(); END $body$",
        "CREATE PROCEDURE p() LANGUAGE plpgsql AS $$ BEGIN CALL refresh_stats(); END $$",
    ],
)
def test_a_routine_body_is_read_as_code(sql):
    assert "CALL refresh_stats()" in code_text(sql, "postgresql")


@pytest.mark.passes_on_main  # guards what the fix must not change
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$ CALL p() $$ AS note FROM t",
        "SELECT id FROM t WHERE note = $q$ x IN (SELECT 1) $q$",
        # A string inside a body is still a string.
        "DO $$ BEGIN RAISE NOTICE 'call p() IN (SELECT 1)'; END $$",
    ],
)
def test_a_dollar_string_that_is_no_body_stays_a_literal(sql):
    facts = QueryFacts(sql, "postgresql")
    assert facts.stored_routine_keyword is False
    assert facts.exists_or_in_subquery is False


@pytest.mark.passes_on_main  # guards what the fix must not change
def test_a_routine_body_is_code_in_postgresql_only():
    sql = "DO $$ BEGIN CALL refresh_stats(); END $$"
    assert code_text(sql, "postgresql") != code_text(sql, "mysql")
    assert code_text(sql, "mysql") == sql  # no dollar quoting there


# -- Linear time ------------------------------------------------------------------


def _unterminated_nested(n_chars: int) -> str:
    return "SELECT 1 " + "/* /* */ " * (n_chars // 9)


def _nested_pairs(n_chars: int) -> str:
    return "SELECT 1 " + "/* a /* b */ c */ " * (n_chars // 18)


def _brackets(n_chars: int) -> str:
    return "SELECT 1 " + "[a]]b' " * (n_chars // 7)


def _escape_strings(n_chars: int) -> str:
    return "SELECT 1 " + "E'\\' " * (n_chars // 5)


def _bodies(n_chars: int) -> str:
    return "SELECT 1 " + "DO $$ x $$ " * (n_chars // 11)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("make_text", "engine"),
    [
        (_unterminated_nested, "postgresql"),
        (_unterminated_nested, "sqlserver"),
        (_nested_pairs, "postgresql"),
        (_brackets, "sqlserver"),
        (_escape_strings, "postgresql"),
        (_bodies, "postgresql"),
    ],
    ids=lambda p: getattr(p, "__name__", p),
)
def test_the_dialect_scans_are_linear_in_the_query_length(
    make_text: Callable[[int], str], engine: str
):
    long_chars, short = 50_000, 1_000
    long_texts = [make_text(long_chars)]
    short_texts = [make_text(short) for _ in range(long_chars // short)]

    def run(texts: list[str]) -> None:
        for text in texts:
            strip_comments_and_literals(text, engine)
            code_text(text, engine)
            scan_sql(text, engine)

    assert_cost_ratio(lambda: run(long_texts), lambda: run(short_texts), 3.0)


# -- Every reader of the grammar --------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "join_kept"),
    [("mysql", False), ("postgresql", True), ("sqlserver", True), ("oracle", True)],
)
def test_a_backslash_escapes_a_double_quote_only_where_it_is_a_mysql_string(engine, join_kept):
    sql = 'SELECT "a\\" JOIN u " FROM t'
    assert ("JOIN" in strip_comments_and_literals(sql, engine)) is join_kept


def test_readable_sql_reads_a_bracketed_apostrophe_as_part_of_the_name():
    sql = "SELECT [O'Brien] FROM t WHERE a IS NULL AND b = 'x'"
    assert "IS NULL" in readable_sql(sql, "sqlserver")


@pytest.mark.passes_on_main  # guards what the fix must not change
def test_dynamic_sql_in_an_escape_string_inside_a_body_is_a_write():
    sql = "DO $$ BEGIN EXECUTE E'DELETE FROM t WHERE a = \\'x\\''; END $$"
    assert QueryFacts(sql, "postgresql").write_statement_text is True


def test_the_regex_fallbacks_read_the_dialect():
    sql = "WITH r AS (SELECT 1 # UNION SELECT * FROM r\n) SELECT * FROM r"
    assert regex_recursive_cte(sql, "mysql") is False
    assert regex_recursive_cte(sql, "postgresql") is True


def test_the_aurora_choice_reads_sequences_in_the_dialect():
    collection = {"queries": {"query_patterns": [{"query_text": "SELECT 1 # NEXT VALUE FOR s\n"}]}}
    assert detect_heterogeneous_features(collection, "mysql")["sequences"] is False
    assert detect_heterogeneous_features(collection, "sqlserver")["sequences"] is True
