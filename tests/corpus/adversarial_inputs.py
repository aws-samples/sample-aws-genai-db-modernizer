"""The shared adversarial input corpus for the text and SQL heuristics (#1411).

Every SQL detector in the declared fact modules and every prose rewriter or guard
runs over these entries (``tests/unit/corpus/test_adversarial_corpus.py``), so a
hostile input a reviewer once wrote by hand is checked against every heuristic,
not only the one it was written for.

A SQL entry pairs a hostile statement with its ``twin``: the same statement with
the hostile part (a keyword inside a string, a comment, a quoted identifier, ...)
replaced by something inert. A detector's default expectation is that it answers
the same on both, since the engine would run the same query. A prose entry has a
twin when the hostile part is not prose (a code fence); otherwise each prose
detector declares its own expected answer for it.

Each entry names the dialects it applies to: a MySQL ``#`` comment is an operator
in PostgreSQL, and only PostgreSQL has dollar-quoted strings. The test passes
each dialect to every detector that takes one (AGENTS.md, "Pass the source
dialect or engine wherever SQL is parsed").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALL_DIALECTS: tuple[str, ...] = ("mysql", "postgresql", "sqlserver", "oracle")


@dataclass(frozen=True)
class SqlEntry:
    id: str
    text: str
    twin: str
    dialects: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class ProseEntry:
    id: str
    text: str
    twin: str | None
    why: str


# SQL every heuristic looks for, written so it is inert inside a string literal (its
# quotes doubled), a comment or a dollar-quoted string. No ``$``, ``*/`` or newline.
_HOSTILE_WORDS = (
    "GROUP BY y ORDER BY z LIMIT 5 OFFSET 2 JOIN u ON 1=1 COUNT(*) MAX(a) MIN(b) SUM(c) "
    "OVER (PARTITION BY x) ROW_NUMBER() a LIKE ''%b'' b ILIKE ? REGEXP ''^x'' "
    "MATCH (a) AGAINST (''x'') to_tsquery(''x'') similarity(a, ?) WHERE status = ''x'' "
    "AND c IS NULL AND d NOT IN (1) AND e <> 2 AND f BETWEEN 1 AND 5 AND g = ANY(ARRAY[1]) "
    "AND EXISTS (SELECT 1) WITH RECURSIVE r AS (SELECT 1 UNION SELECT 1 FROM r) "
    "INSERT INTO u VALUES (1) ON CONFLICT DO NOTHING RETURNING id "
    "ON DUPLICATE KEY UPDATE a = 1 UPDATE u SET a = 1 DELETE FROM u MERGE INTO u "
    "doc->>''k'' JSON_EXTRACT(doc, ''k'') GROUP_CONCAT(a) LATERAL CROSS APPLY "
    "ST_Distance(a, b) date_trunc(''day'', ts) NOW() CALL p() FOR UPDATE SKIP LOCKED "
    "SQL_CALC_FOUND_ROWS created_at session_id token NEXTVAL information_schema"
)
_IN_COMMENT = _HOSTILE_WORDS.replace("''", "'")
# Words the base statements share (SELECT id FROM t WHERE ...): not hostile.
_BASE_WORDS = frozenset({"SELECT", "FROM", "WHERE", "AND", "AS", "ID", "NOTE"})


def hostile_keywords() -> frozenset[str]:
    """The words of the hostile SQL, upper-cased: what must not survive a pass that
    blanks literals and comments."""
    words = {w for w in re.findall(r"[A-Za-z_]+", _HOSTILE_WORDS.upper()) if len(w) > 1}
    return frozenset(words - _BASE_WORDS)


SQL_ENTRIES: tuple[SqlEntry, ...] = (
    SqlEntry(
        "keywords_in_string",
        f"SELECT id FROM t WHERE note = '{_HOSTILE_WORDS}'",
        "SELECT id FROM t WHERE note = 'x'",
        ALL_DIALECTS,
        "clause keywords, a call, a window and a wildcard LIKE inside a string literal",
    ),
    SqlEntry(
        "range_and_lock_in_string",
        "SELECT id FROM t WHERE note = 'BETWEEN 1 AND 5, a < 9, FOR UPDATE SKIP LOCKED'",
        "SELECT id FROM t WHERE note = 'x'",
        ALL_DIALECTS,
        "a range, a comparison and a locking clause inside a string literal",
    ),
    SqlEntry(
        "utility_verbs_in_string",
        "SELECT id FROM t WHERE note = 'DROP TABLE t; SHOW TABLES; information_schema'",
        "SELECT id FROM t WHERE note = 'x'",
        ALL_DIALECTS,
        "DDL, a session verb and a catalog name inside a string literal",
    ),
    SqlEntry(
        "timestamp_and_session_names_in_string",
        "SELECT id FROM t WHERE note = 'created_at session_id token'",
        "SELECT id FROM t WHERE note = 'x'",
        ALL_DIALECTS,
        "timestamp and session column names inside a string literal",
    ),
    SqlEntry(
        "where_in_string",
        "SELECT id, 'WHERE a IN (1) AND b = 2' AS note FROM t",
        "SELECT id, 'x' AS note FROM t",
        ALL_DIALECTS,
        "a WHERE clause, a key equality and an IN list inside a string literal, no real WHERE",
    ),
    SqlEntry(
        "where_in_line_comment",
        "SELECT id FROM t -- WHERE a = 1 AND b IN (2)",
        "SELECT id FROM t",
        ALL_DIALECTS,
        "a WHERE clause with a key equality inside a line comment, no real WHERE",
    ),
    SqlEntry(
        "where_in_block_comment",
        "SELECT id FROM t /* WHERE a = 1 AND b IN (2) */ ORDER BY id",
        "SELECT id FROM t ORDER BY id",
        ALL_DIALECTS,
        "a WHERE clause with a key equality inside a block comment, no real WHERE",
    ),
    SqlEntry(
        "where_in_hash_comment",
        "SELECT id FROM t # WHERE a = 1 AND b IN (2)\nORDER BY id",
        "SELECT id FROM t\nORDER BY id",
        ("mysql",),
        "a WHERE clause with a key equality inside a MySQL # comment, no real WHERE",
    ),
    SqlEntry(
        "where_in_dollar_quoted_string",
        "SELECT $$ WHERE a = 1 AND b IN (2) $$ AS note, id FROM t",
        "SELECT 'x' AS note, id FROM t",
        ("postgresql",),
        "a WHERE clause with a key equality inside a dollar-quoted string, no real WHERE",
    ),
    SqlEntry(
        "key_after_where_in_comment",
        "SELECT id FROM t WHERE id > 1 -- AND a = 1 AND b IN (2)\nORDER BY id",
        "SELECT id FROM t WHERE id > 1\nORDER BY id",
        ALL_DIALECTS,
        "a key equality and an IN list in a comment after a range-only WHERE",
    ),
    SqlEntry(
        "key_after_where_in_string",
        "SELECT id FROM t WHERE id > 1 AND note > 'a = 1 AND b IN (2)'",
        "SELECT id FROM t WHERE id > 1 AND note > 'x'",
        ALL_DIALECTS,
        "a key equality and an IN list in a string after a range-only WHERE",
    ),
    SqlEntry(
        "subquery_in_string",
        "SELECT id FROM t WHERE note > '(SELECT x FROM u WHERE a.b = c.d)'",
        "SELECT id FROM t WHERE note > 'x'",
        ALL_DIALECTS,
        "a correlated subquery shape inside a string literal",
    ),
    SqlEntry(
        "subquery_in_block_comment",
        "SELECT id FROM t WHERE id > 1 /* (SELECT x FROM u WHERE a.b = c.d) */",
        "SELECT id FROM t WHERE id > 1",
        ALL_DIALECTS,
        "a correlated subquery shape inside a block comment",
    ),
    SqlEntry(
        "subquery_in_dollar_quoted_string",
        "SELECT $$ (SELECT x FROM u WHERE a.b = c.d) $$ AS note FROM t WHERE id > 1",
        "SELECT 'x' AS note FROM t WHERE id > 1",
        ("postgresql",),
        "a correlated subquery shape inside a dollar-quoted string",
    ),
    SqlEntry(
        "computed_join_in_block_comment",
        "SELECT id FROM t /* JOIN u ON lower(u.a) = t.b */ WHERE id = 1",
        "SELECT id FROM t WHERE id = 1",
        ALL_DIALECTS,
        "a join on a computed key inside a block comment",
    ),
    SqlEntry(
        "computed_join_in_line_comment",
        "SELECT id FROM t -- JOIN u ON lower(u.a) = t.b\nWHERE id = 1",
        "SELECT id FROM t\nWHERE id = 1",
        ALL_DIALECTS,
        "a join on a computed key inside a line comment",
    ),
    SqlEntry(
        "computed_join_in_string",
        "SELECT id FROM t WHERE note = ' JOIN u ON lower(u.a) = t.b '",
        "SELECT id FROM t WHERE note = 'x'",
        ALL_DIALECTS,
        "a join on a computed key inside a string literal",
    ),
    SqlEntry(
        "computed_join_in_dollar_quoted_string",
        "SELECT $$ JOIN u ON lower(u.a) = t.b $$ AS note, id FROM t WHERE id = 1",
        "SELECT 'x' AS note, id FROM t WHERE id = 1",
        ("postgresql",),
        "a join on a computed key inside a dollar-quoted string",
    ),
    SqlEntry(
        "doubled_quote_in_string",
        "SELECT id FROM t WHERE note = 'it''s a JOIN u, GROUP BY x' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ALL_DIALECTS,
        "a doubled quote inside a string, keywords after it",
    ),
    SqlEntry(
        "backslash_escaped_quote",
        "SELECT id FROM t WHERE note = 'a\\' JOIN u ON 1=1 GROUP BY x' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("mysql",),
        "MySQL's backslash-escaped quote: the string runs past it",
    ),
    SqlEntry(
        "backslash_ends_no_string",
        "SELECT id FROM t WHERE path = 'C:\\' AND c IS NULL AND note = 'x'",
        "SELECT id FROM t WHERE path = 'C:x' AND c IS NULL AND note = 'x'",
        ("postgresql", "sqlserver", "oracle"),
        "a backslash is an ordinary character outside MySQL: the string ends at its quote",
    ),
    SqlEntry(
        "escape_string_backslash_quote",
        "SELECT id FROM t WHERE note = E'a\\' JOIN u ON 1=1 GROUP BY x' AND id = 1",
        "SELECT id FROM t WHERE note = E'x' AND id = 1",
        ("postgresql",),
        "PostgreSQL's E'...' string, the one there that reads a backslash as an escape",
    ),
    SqlEntry(
        "oracle_q_quote_with_apostrophe",
        f"SELECT id FROM t WHERE note = q'[it's {_IN_COMMENT}]' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "Oracle's q'[...]' string: an apostrophe and clause keywords inside it are text",
    ),
    SqlEntry(
        "oracle_q_quote_closer_then_quote",
        "SELECT id FROM t WHERE note = q'[a] ' JOIN u ON 1=1 GROUP BY x]' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "a closing delimiter not followed by a quote, and a quote not after one, end nothing",
    ),
    SqlEntry(
        "oracle_q_quote_braces",
        "SELECT id FROM t WHERE note = q'{it's a JOIN u, GROUP BY x}' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "q'{...}' closes at }'",
    ),
    SqlEntry(
        "oracle_q_quote_parentheses",
        "SELECT id FROM t WHERE note = q'(it's a JOIN u, GROUP BY x)' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "q'(...)' closes at )'",
    ),
    SqlEntry(
        "oracle_q_quote_angle_brackets",
        "SELECT id FROM t WHERE note = q'<it's a JOIN u, GROUP BY x>' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "q'<...>' closes at >'",
    ),
    SqlEntry(
        "oracle_q_quote_same_delimiter",
        "SELECT id FROM t WHERE note = q'!it's a JOIN u, GROUP BY x!' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "q'!...!' closes at !', and any other punctuation works the same way",
    ),
    SqlEntry(
        "oracle_national_q_quote",
        "SELECT id FROM t WHERE note = nq'[it's a JOIN u, GROUP BY x]' AND id = 1",
        "SELECT id FROM t WHERE note = 'x' AND id = 1",
        ("oracle",),
        "the national form nq'[...]'",
    ),
    SqlEntry(
        "line_comment",
        f"SELECT id FROM t -- {_IN_COMMENT}\nWHERE id = 1",
        "SELECT id FROM t\nWHERE id = 1",
        ALL_DIALECTS,
        "clause keywords inside a line comment",
    ),
    SqlEntry(
        "line_comment_with_apostrophe",
        "SELECT id FROM t -- don't JOIN u\nWHERE id = 1 AND note = 'x'",
        "SELECT id FROM t\nWHERE id = 1 AND note = 'x'",
        ALL_DIALECTS,
        "an apostrophe in a comment must not open a string",
    ),
    SqlEntry(
        "comment_after_terminator",
        "SELECT id FROM t WHERE id = 1; -- ORDER BY id LIMIT 1",
        "SELECT id FROM t WHERE id = 1;",
        ALL_DIALECTS,
        "a line comment after the statement's terminator",
    ),
    SqlEntry(
        "block_comment",
        f"SELECT /* {_IN_COMMENT} */ id FROM t WHERE id = 1",
        "SELECT id FROM t WHERE id = 1",
        ALL_DIALECTS,
        "a call, a window and clause keywords inside a block comment",
    ),
    SqlEntry(
        "leading_block_comment",
        f"/* DROP TABLE u; SELECT * FROM u {_IN_COMMENT} */ SELECT id FROM t WHERE id = 1",
        "SELECT id FROM t WHERE id = 1",
        ALL_DIALECTS,
        "a leading block comment holding another statement",
    ),
    SqlEntry(
        "nested_block_comment",
        f"SELECT id FROM t /* outer /* inner */ {_IN_COMMENT} */ WHERE id = 1",
        "SELECT id FROM t WHERE id = 1",
        ("postgresql", "sqlserver"),
        "a nested block comment, which PostgreSQL and SQL Server end at the outer */",
    ),
    SqlEntry(
        "mysql_hash_comment",
        f"SELECT id FROM t # {_IN_COMMENT}\nWHERE id = 1",
        "SELECT id FROM t\nWHERE id = 1",
        ("mysql",),
        "MySQL's # line comment (an operator in PostgreSQL)",
    ),
    SqlEntry(
        "dollar_quoted_string",
        f"SELECT $$ {_IN_COMMENT} $$ AS note, id FROM t WHERE id = 1",
        "SELECT 'x' AS note, id FROM t WHERE id = 1",
        ("postgresql",),
        "PostgreSQL's dollar-quoted string",
    ),
    SqlEntry(
        "nested_dollar_quoted_tags",
        f"SELECT $outer$ a $inner$ {_IN_COMMENT} $inner$ b $outer$ AS note, id FROM t "
        "WHERE id = 1",
        "SELECT 'x' AS note, id FROM t WHERE id = 1",
        ("postgresql",),
        "a dollar-quoted string holding another tag, which does not end it",
    ),
    SqlEntry(
        "do_block_body",
        "DO $$ BEGIN CALL refresh_stats(); END $$",
        "DO $body$ BEGIN CALL refresh_stats(); END $body$",
        ("postgresql",),
        "a DO block's dollar-quoted body is code the engine runs, not a string",
    ),
    SqlEntry(
        "function_body",
        "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 WHERE a IN (SELECT b FROM u) $$ "
        "LANGUAGE sql",
        "CREATE FUNCTION f() RETURNS int AS $fn$ SELECT 1 WHERE a IN (SELECT b FROM u) $fn$ "
        "LANGUAGE sql",
        ("postgresql",),
        "a routine's AS $$ ... $$ body is code the engine runs, not a string",
    ),
    SqlEntry(
        "backtick_identifiers",
        "SELECT `order`, `group by`, `count`, `join` FROM t WHERE `limit` = 1",
        "SELECT c1, c2, c3, c4 FROM t WHERE c5 = 1",
        ("mysql",),
        "keywords as backtick-quoted MySQL identifiers",
    ),
    SqlEntry(
        "bracket_identifiers",
        "SELECT [order], [group by], [count], [join] FROM t WHERE [limit] = 1",
        "SELECT c1, c2, c3, c4 FROM t WHERE c5 = 1",
        ("sqlserver",),
        "keywords as bracket-quoted SQL Server identifiers",
    ),
    SqlEntry(
        "bracket_identifier_with_apostrophe",
        "SELECT [O'Brien]] Notes] FROM t WHERE status = 'x' AND c IS NULL AND note = 'y'",
        "SELECT c1 FROM t WHERE status = 'x' AND c IS NULL AND note = 'y'",
        ("sqlserver",),
        "an apostrophe (and a ]] escape) inside a bracketed SQL Server name opens no string",
    ),
    # Maintenance and DDL statements that name a table (#483): the table must be the
    # same with the hostile words in a comment or a string as without them.
    SqlEntry(
        "alter_index_on_with_comment",
        f"ALTER INDEX ix ON t REBUILD /* {_IN_COMMENT} */",
        "ALTER INDEX ix ON t REBUILD",
        ("sqlserver",),
        "T-SQL ALTER INDEX ... ON <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "update_statistics_with_comment",
        f"UPDATE STATISTICS t /* {_IN_COMMENT} */",
        "UPDATE STATISTICS t",
        ("sqlserver",),
        "T-SQL UPDATE STATISTICS <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "drop_index_on_with_comment",
        f"DROP INDEX ix ON t /* {_IN_COMMENT} */",
        "DROP INDEX ix ON t",
        ("mysql", "sqlserver"),
        "DROP INDEX ... ON <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "truncate_with_comment",
        f"TRUNCATE TABLE t /* {_IN_COMMENT} */",
        "TRUNCATE TABLE t",
        ALL_DIALECTS,
        "TRUNCATE TABLE <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "alter_table_with_comment",
        f"ALTER TABLE t ADD COLUMN c int /* {_IN_COMMENT} */",
        "ALTER TABLE t ADD COLUMN c int",
        ALL_DIALECTS,
        "ALTER TABLE <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "alter_table_comment_option_string",
        f"ALTER TABLE t COMMENT = '{_HOSTILE_WORDS}'",
        "ALTER TABLE t COMMENT = 'x'",
        ("mysql",),
        "ALTER TABLE with a table comment option holding clause keywords in a string",
    ),
    SqlEntry(
        "comment_on_table_string",
        f"COMMENT ON TABLE t IS '{_HOSTILE_WORDS}'",
        "COMMENT ON TABLE t IS 'x'",
        ("postgresql", "oracle"),
        "COMMENT ON TABLE <table> with clause keywords in a string literal",
    ),
    SqlEntry(
        "optimize_table_with_line_comment",
        f"OPTIMIZE TABLE t -- {_IN_COMMENT}",
        "OPTIMIZE TABLE t",
        ("mysql",),
        "MySQL OPTIMIZE TABLE <table> with clause keywords in a line comment",
    ),
    SqlEntry(
        "lock_tables_with_comment",
        f"LOCK TABLES t READ /* {_IN_COMMENT} */",
        "LOCK TABLES t READ",
        ("mysql",),
        "MySQL LOCK TABLES <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "vacuum_with_line_comment",
        f"VACUUM ANALYZE t -- {_IN_COMMENT}",
        "VACUUM ANALYZE t",
        ("postgresql",),
        "PostgreSQL VACUUM <table> with clause keywords in a line comment",
    ),
    SqlEntry(
        "describe_with_comment",
        f"DESCRIBE t /* {_IN_COMMENT} */",
        "DESCRIBE t",
        ("mysql",),
        "MySQL DESCRIBE <table> with clause keywords in a block comment",
    ),
    SqlEntry(
        "double_quoted_identifiers",
        'SELECT "order", "group by", "count", "join" FROM t WHERE "limit" = 1',
        "SELECT c1, c2, c3, c4 FROM t WHERE c5 = 1",
        ("postgresql", "oracle"),
        "keywords as double-quoted identifiers",
    ),
)


PROSE_ENTRIES: tuple[ProseEntry, ...] = (
    ProseEntry(
        "fenced_code_block",
        "The plan keeps reads local.\n\n```sql\n"
        "SELECT a -- robust, seamless — ensure\nFROM dynamodb.t; -- typically\n```\n",
        "The plan keeps reads local.\n\n",
        "style-list words, dashes and an engine id inside a backtick fence",
    ),
    ProseEntry(
        "tilde_fenced_code_block",
        "The plan keeps reads local.\n\n~~~\nleverage — robust -- seamless\n~~~\n",
        "The plan keeps reads local.\n\n",
        "the same inside a tilde fence",
    ),
    ProseEntry(
        "unclosed_code_fence",
        "The plan keeps reads local.\n\n```\nleverage — robust -- seamless\n",
        "The plan keeps reads local.\n\n",
        "an unclosed fence runs to the end of the text (CommonMark)",
    ),
    ProseEntry(
        "code_span",
        "Set `robust_mode` and `--seamless` before the first wave.",
        "Set the flag and the option before the first wave.",
        "style-list words inside backtick code spans",
    ),
    ProseEntry(
        "negation",
        "This design is not robust. Do not move these reads to OpenSearch.",
        None,
        "negation: a filler word is still one, and a negated engine is not recommended",
    ),
    ProseEntry(
        "numeric_ranges",
        "Plan 2-4 weeks for the first wave, 9 am – 5 pm, with 30s–60s of lag.",
        None,
        "ranges: an ASCII hyphen and an en dash between figures are range marks",
    ),
    ProseEntry(
        "identifier_colon",
        "Rows are ordered by meta_id: one Query per comment_id.",
        None,
        "a column name before a colon is prose, not a reason code",
    ),
    ProseEntry(
        "negated_attribution",
        "ElastiCache does not serve the post meta lookups.",
        None,
        "a negated table attribution states what the target does not do",
    ),
    ProseEntry(
        "risk_and_wave_claims",
        "There are 9 high risks. Move OpenSearch first, then Aurora PostgreSQL.",
        None,
        "a wrong risk count and a wrong wave order, which the summary checks must catch",
    ),
    ProseEntry(
        "quoted_engine_id",
        'The setting is "dynamodb" in the `aurora_mysql` field.',
        None,
        "a raw engine id quoted or in a code span",
    ),
)


# Very long input (#1230): built at two sizes so a test can bound the growth of a
# detector's cost, relative to a same-process reference, never in seconds.
LONG_SQL_UNIT = "SELECT a FROM t WHERE note = 'it''s GROUP BY x' /* JOIN */ AND b = ? -- c\n"
LONG_PROSE_UNIT = "The reads stay local. `code_span` -- 2-4 weeks.\n"


def long_text(unit: str, size: int) -> str:
    """``unit`` repeated to about ``size`` characters: one very long input."""
    return unit * max(1, size // len(unit))
