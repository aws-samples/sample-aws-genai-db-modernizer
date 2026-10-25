"""The one module that calls sqlglot: table references from SQL text (#557).

Every caller asks this facade for a small, frozen fact model
(:class:`TableReferences`) and never sees sqlglot's syntax tree, so a sqlglot
upgrade or a parser swap touches one file. The source engine picks the
dialect (``postgresql`` is ``postgres`` and ``sqlserver`` is ``tsql`` in
sqlglot); collector digest text is normalized first
(:func:`src.tools.sql.normalize.normalize_digest_text`), so a MySQL digest such
as ``IN (...) LIMIT ?, ...`` parses. Three dialect gaps in sqlglot are bridged
here too: Oracle positional binds (``:1``) are renamed (``:b1``), a space is
added after a PostgreSQL ``$1`` placeholder written flush against the next
token (``$1=$2``, never inside an identifier such as ``price$1usd``), and
T-SQL's ``DELETE``/``UPDATE``/``INSERT TOP (n)`` row limit is dropped when it
is a number, ``@variable`` or ``?``, and a MySQL ``REPLACE INTO`` is read as the
``INSERT INTO`` it is for access (sqlglot reads ``REPLACE`` only as a generic
command; #986), and an Oracle ``q'[...]'`` string is written as the plain ``'...'``
string it is (sqlglot cannot read the alternative quoting; #1481). Each rewrite runs only outside string
literals, quoted identifiers and comments, and none can add, remove or
rename a table name (tests pin each case).

What counts as a table reference:

- every table node in every statement (``FROM``/``JOIN``, subqueries and
  derived tables, ``INSERT INTO``, ``UPDATE``, ``DELETE FROM``, ``MERGE``
  target and source), in the order it appears in the text, duplicates
  collapsed;
- not a ``WITH``-defined CTE name where that CTE is visible (the #682 rules:
  later CTE bodies and the statement after the WITH list; its own body only
  when the CTE is recursive, i.e. ``RECURSIVE`` or a ``UNION`` in the body);
  a qualified name is never a CTE, and names compare case-insensitively;
- not MySQL/Oracle's ``DUAL`` pseudo-table, and not a table function such as
  ``generate_series(...)``;
- not a T-SQL temp table (``#tmp``, ``##tmp``) or table variable (``@t``):
  each is local to the session or batch, never a collected table, and the
  regex fallback never read one either. The query-shape facts still count a
  join to one.

Size cap: text longer than ``MAX_SQL_CHARS`` is not parsed or normalized; it
falls back with reason ``too_long``.

Fallback: when sqlglot raises, returns a generic ``Command`` node (syntax it
doesn't model, e.g. ``REFRESH MATERIALIZED VIEW``) or something that is not a
statement (an expression it read out of garbage text), or the text is empty, the
facade returns the regex extractor's answer
(:func:`src.tools.sql.regex_tables.regex_table_refs`) with
``parsed_by="regex"`` and the reason. It never raises. MySQL's ``LOAD DATA
... INTO TABLE t``, which sqlglot can't parse, takes this path; the regex
reads its target table (:mod:`src.tools.sql.bulk_load`, #777).

Resolving a reference to a collection's table id (``<database>.<table>``)
stays with the caller: it needs the collection's table list.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal

import sqlglot
from sqlglot import exp

from src.shared.sql_comments import block_comments_nest, strip_comments, strip_leading_comments
from src.shared.sql_spans import is_alternative_quote, literals_and_comments_for, spans
from src.tools.sql.normalize import (
    NormalizedQuery,
    normalize_digest_text,
    quoted_mask,
    without_comments_between_statements,
)
from src.tools.sql.refs import ColumnUse, TableRef
from src.tools.sql.regex_facts import (
    TIMESTAMP_COLUMN_NAME_RE,
    WORDS_RE,
    RegexMatch,
    WildcardLike,
    regex_aggregate_function_count,
    regex_append_targets,
    regex_fulltext_search,
    regex_group_by,
    regex_grouping_sets,
    regex_has_aggregation,
    regex_has_joins,
    regex_has_subquery,
    regex_join_count,
    regex_json_access,
    regex_like_parameter,
    regex_multi_table_write,
    regex_offset_window_function,
    regex_order_by,
    regex_pattern_match,
    regex_ranking_window_function,
    regex_recursive_cte,
    regex_row_limit,
    regex_similarity_match,
    regex_timestamp_column_names,
    regex_top_n,
    regex_wildcard_leading,
    regex_wildcard_likes,
    regex_window_function,
    regex_windowed_aggregate,
    regex_write_targets,
)
from src.tools.sql.regex_tables import regex_table_refs

# sqlglot logs a warning for every statement it parses as a generic Command.
# The facade records that as ``fallback_reason="command"`` instead, so the
# warning would only add noise to compact script output.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Source engine (``DatabaseEngine`` in src/contracts/collector_input.py, plus
# the ``postgres`` alias) -> sqlglot dialect. sqlglot has no DB2 dialect, so
# DB2 (deferred, #555) and anything unknown use sqlglot's generic dialect.
_DIALECTS = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
    "sqlserver": "tsql",
    "oracle": "oracle",
}

# The parser's version: part of what a stored reading was read with (#934,
# ``src.tools.sql.stored_facts``).
PARSER_VERSION: str = sqlglot.__version__

# Longer text is not parsed: it falls back to the regex with reason
# ``too_long``, so one huge statement can't slow a collection down. The
# samples' longest query is about 5,300 characters.
MAX_SQL_CHARS = 100_000

# The bridges below run only outside string literals, quoted identifiers and
# comments (the normalizer's scan), so none of them can rename a table.

# Oracle positional binds (``:1``) that sqlglot's Oracle dialect rejects; named
# binds (``:b1``, ``:name``) parse. The lookbehind keeps ``12:30`` intact.
_ORACLE_POSITIONAL_BIND_RE = re.compile(r"(?<![\w:]):(\d+)\b")

# PostgreSQL ``$1`` placeholders written with no space before the next token
# (``$1=$2``, ``IN ($3,$4)``): sqlglot's tokenizer reads ``$3,$`` as the start
# of a dollar-quoted string, so the facade adds the space. The lookbehind
# keeps identifiers that contain ``$<digits>`` (``price$1usd``) whole.
_PG_PLACEHOLDER_RE = re.compile(r"(?<![\w$])(\$\d+)(?=[^\s\d])")

# T-SQL ``DELETE TOP (n) FROM t`` / ``UPDATE TOP (n) t`` / ``INSERT TOP (n) INTO
# t``: sqlglot reads ``TOP (5000)`` as a table named TOP (a MySQL-style
# multi-table DELETE) or fails, so the facade drops the row limit. Only a
# number, a ``@variable`` or a ``?`` placeholder is dropped; any other
# expression (a subquery can name a table) is left for sqlglot or the fallback.
_TSQL_DML_TOP_RE = re.compile(
    r"\b(DELETE|UPDATE|INSERT)\s+TOP\s*\(\s*(?:\d+|@\w+|\?)\s*\)(?:\s*PERCENT\b)?",
    re.IGNORECASE,
)

# MySQL ``REPLACE [LOW_PRIORITY | DELAYED] INTO t ...`` writes rows by key like
# ``INSERT ... ON DUPLICATE KEY UPDATE`` (it deletes a row with the same key, then
# inserts), but sqlglot reads it only as a generic command. The facade parses it as
# the ``INSERT`` it is for access; the priority modifier, which names no table, is
# dropped. The pattern is anchored at the start of the statement (after its leading
# comments, #1178), so a ``REPLACE(...)`` function call never matches (#986).
_MYSQL_REPLACE_RE = re.compile(
    r"\A(\s*)REPLACE(?:\s+(?:LOW_PRIORITY|DELAYED))?(?=\s+INTO\b)", re.IGNORECASE
)

# How many ``AND``/``OR`` cut points the repair of a cut-short digest tries, last first.
_MAX_REPAIR_CUTS = 40
_AND_OR_RE = re.compile(r"\b(?:AND|OR)\b", re.IGNORECASE)

# A parsed statement must be one of these; anything else (a stray expression
# sqlglot read out of garbage text) is not a statement.
_STATEMENT_TYPES = (
    exp.Query,
    exp.DML,
    exp.DDL,
    exp.Show,
    exp.Set,
    exp.Use,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    # Statements that name a table the schema holds (#483): sqlglot reads them
    # fully, but they are not exp.DDL, so they used to fall back to a regex
    # scan that found no table in them.
    exp.Alter,
    exp.Drop,
    exp.TruncateTable,
    exp.Analyze,
    exp.Describe,
    exp.Comment,
)

# The kinds of ALTER and DROP that act on a table or a view.
_TABLE_KINDS = frozenset({"TABLE", "VIEW"})

# Pseudo-tables that name no stored data.
_PSEUDO_TABLES = frozenset({"dual"})

ParsedBy = Literal["sqlglot", "regex"]
# ``facade_error`` is never produced here: the offline parser records it when
# this module raises despite its contract, and it uses the regex answer.
FallbackReason = Literal[
    "parse_error", "not_a_statement", "command", "empty", "too_long", "facade_error"
]


@dataclass(frozen=True)
class TableReferences:
    """The table references in one piece of SQL text.

    Attributes:
        tables: Table references in first-seen order, duplicates and CTE
            names removed.
        cte_names: The ``WITH``-defined names in the text (lower-case), so a
            caller can tell a dropped CTE from a missing table.
        parsed_by: ``"sqlglot"``, or ``"regex"`` when the facade fell back.
        fallback_reason: Why it fell back, or None.
        dialect: The sqlglot dialect used (``""`` is sqlglot's generic one).
        truncated: The text looks cut off by a digest length cap (from the
            normalization step).
    """

    tables: tuple[TableRef, ...]
    cte_names: tuple[str, ...]
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None
    dialect: str
    truncated: bool


@dataclass(frozen=True)
class SqlFacts:
    """Query-shape facts about one piece of SQL text (#796).

    Read from the syntax tree, so a comment, a string literal or a column name
    (``count``, ``user_selectable``) is never read as SQL; the regex facts
    (:mod:`src.tools.sql.regex_facts`) when the facade falls back, so a
    statement sqlglot can't read keeps exactly its pre-#796 answer.

    Attributes:
        join_count: Joins to another row source: every ``JOIN`` clause and
            every further item of a ``FROM``/``USING`` list (``FROM a, b`` is
            one join), in every statement and subquery. A join to a table
            function of the row (``CROSS APPLY OPENJSON(...)``, ``CROSS JOIN
            unnest(...)``, ``generate_series(...)``) reads no other table, so
            it is not counted. ``UPDATE ... FROM`` / ``DELETE ... USING``
            count their list's further items, not the first one, the way the
            regex never counted them either.
        has_joins: ``join_count > 0`` (the regex fallback keeps its own
            `` join `` test).
        has_aggregation: A ``GROUP BY``, or an aggregate function -- one that
            combines many rows' values into one (``COUNT``, ``SUM``, ``AVG``,
            ``MIN``, ``MAX``, ``GROUP_CONCAT``, ``STRING_AGG``, ``ARRAY_AGG``,
            ...), windowed (``COUNT(*) OVER ()``) or not. The ranking and
            offset window functions (``ROW_NUMBER``, ``RANK``, ``DENSE_RANK``,
            ``PERCENT_RANK``, ``CUME_DIST``, ``NTILE``, ``LAG``, ``LEAD``,
            ``FIRST_VALUE``, ``LAST_VALUE``, ``NTH_VALUE``) combine nothing,
            so alone they are not one.
        has_subquery: More than one ``SELECT`` in the statement: a subquery,
            a derived table, a CTE body, a second ``UNION``/``EXCEPT`` branch
            (the regex's meaning, without its misreads).
        wildcard_leading: A ``LIKE``/``ILIKE`` (``NOT`` and ``ESCAPE``
            included) whose pattern starts with ``%``: a string literal, or a
            concatenation (``'%' || ?``, ``'%' + @t``, ``CONCAT('%', ?)``)
            whose first part is one. A bind parameter (``LIKE ?``) is not:
            its value is unknown.
        parsed_by: ``"sqlglot"``, or ``"regex"`` when the facade fell back.
        fallback_reason: Why it fell back, or None.
        dialect: The sqlglot dialect used (``""`` is sqlglot's generic one).

    The facts below were added by #893, each the one definition its readers
    share (``src.tools.sql.query_facts``); ``has_aggregation`` is ``group_by``
    or a positive ``aggregate_function_count``, read the same way.

    Attributes:
        group_by: A ``GROUP BY`` clause, in any statement or subquery.
        grouping_sets: Several groupings of one row set in one ``GROUP BY``:
            ``GROUPING SETS``, ``ROLLUP``, ``CUBE`` or MySQL's ``WITH ROLLUP``
            (a faceted aggregation, #968).
        aggregate_function_count: How many aggregate function calls (the
            ``has_aggregation`` functions, windowed or not) the text has.
        like_parameter: A ``LIKE``/``ILIKE`` whose whole pattern is a bind
            parameter (``LIKE ?``, ``LIKE $1``): its value is unknown, so it
            is neither a leading wildcard nor a prefix. A MySQL digest writes
            every literal as ``?``, so its ``LIKE '%x%'`` reads this way.
        fulltext_search: A full-text match: MySQL ``MATCH ... AGAINST``,
            PostgreSQL's ``@@`` and ``tsvector``/``tsquery`` functions
            (``to_tsvector``, ``plainto_tsquery``, ``ts_rank``, ...), SQL
            Server's ``CONTAINS``/``FREETEXT``.
        pattern_match: A regular-expression or ``SIMILAR TO`` match
            (``REGEXP``, ``RLIKE``, ``~``, ``~*``, ``REGEXP_LIKE``).
        prefix_regex: Every regular-expression match in the text is a prefix one: a
            case-sensitive, non-negated match (PostgreSQL's ``~``, not ``!~`` or a
            ``NOT``) of a string literal anchored with ``^`` that always starts with at
            least one literal character (``~ '^abc'``; not ``'^a|b'``, ``'^a*'`` or
            ``'^a?b'``), the shape an index on the column can serve (#982). MySQL's ``REGEXP`` is case-insensitive
            on a nonbinary string and ``~*`` is case-insensitive, so neither is one. On
            the regex fallback it is never set.
        similarity_match: A trigram similarity function (``similarity``,
            ``word_similarity``, ``strict_word_similarity``).
        window_function: A function with an ``OVER`` clause.
        ranking_window_function: ``ROW_NUMBER``, ``RANK`` or ``DENSE_RANK``
            over a window: it numbers rows and combines no values.
        offset_window_function: ``LAG``, ``LEAD`` or ``NTILE`` over a window:
            it reads another row, or buckets rows, and combines no values.
        windowed_aggregate: An aggregate function over a window
            (``SUM(...) OVER``, ``COUNT(*) OVER ()``).
        recursive_cte: ``WITH RECURSIVE``, or a CTE whose own body has a
            ``UNION`` and reads the CTE's own name (SQL Server and Oracle
            have no ``RECURSIVE`` keyword).
        order_by: An ``ORDER BY`` that sorts a query's rows (not one inside
            a window's ``OVER (...)``).
        row_limit: A row limit: ``LIMIT``, ``TOP``, ``FETCH FIRST``/``NEXT``.
        top_n: A query (a ``SELECT`` or a set operation) with both an
            ``ORDER BY`` and a row limit: a top-N read. ``TOP`` with no
            ``ORDER BY`` returns arbitrary rows and is not one.
        columns: Every column the statement reads, writes, or lists in an
            ``INSERT``'s column list, with the stored tables it can belong to
            (:class:`ColumnUse`), duplicates collapsed. On the regex fallback,
            each timestamp name (``regex_facts.TIMESTAMP_COLUMN_NAMES``) the text
            holds as a word, with no tables. ``timestamp_column`` is the name
            reading of these columns: a timestamp-named one (``created_at``,
            ``updated_at``, ``timestamp``, ``event_time``, ``*_date``, ...).
            The timestamp-column fact readers use reads the collection's column
            types instead (``query_facts.QueryFacts.timestamp_column``, #935).
        json_access: The query reads or builds JSON: a JSON function or
            operator (``JSON_EXTRACT``, ``->``, ``->>``, ``#>``,
            ``jsonb_*``, ``JSON_VALUE``, ``OPENJSON``, ``FOR JSON``, ``IS
            JSON``), a cast to ``json``/``jsonb``, or a JSON containment
            (``@>`` against a string literal); not PostgreSQL's ``?`` key test.
        multi_table_write: An ``UPDATE`` or ``DELETE`` whose rows come from a join
            with another table, in any dialect: MySQL's ``UPDATE a JOIN b ON ... SET``
            (or ``UPDATE a, b SET``) and ``DELETE a FROM a JOIN b``, the ``UPDATE ...
            FROM`` of PostgreSQL and SQL Server, PostgreSQL's ``DELETE ... USING`` and
            SQL Server's ``DELETE a FROM a JOIN b``. A subquery in ``SET`` or
            ``WHERE`` is not one (#920: both Aurora engines run it set-based).
        wildcard_likes: Each ``LIKE``/``ILIKE`` that makes ``wildcard_leading``
            true, in order, with the column it tests and whether it is a word
            search (:class:`~src.tools.sql.regex_facts.WildcardLike`, #493);
            ``wildcard_leading`` is ``bool(wildcard_likes)``.
        regex_matches: Each match that makes ``pattern_match`` true, in order, with
            the column it tests and that column's candidate tables
            (:class:`~src.tools.sql.regex_facts.RegexMatch`, #1098). Empty on the regex
            fallback, which never knows the tables.
        write_targets: The tables the text writes, lower-cased bare names in
            first-seen order: the target of every ``INSERT`` (``REPLACE``),
            ``UPDATE``, ``DELETE``, ``MERGE`` and ``TRUNCATE``, a
            data-modifying CTE's included (#992). A target written by its
            alias (SQL Server's ``UPDATE t SET ... FROM [Ticket] t``, MySQL's
            ``DELETE a FROM posts a JOIN ...``) is the aliased table; a MySQL
            multi-table ``UPDATE a JOIN b ... SET a.x = ?, b.y = ?`` writes every
            table its ``SET`` columns name; a table the write only reads (``INSERT ... SELECT ... FROM b``, an
            ``UPDATE ... FROM b``, a ``MERGE``'s ``USING`` source) is not one.
        append_targets: The :attr:`write_targets` the text only appends rows to (#1459),
            lower-cased bare names in first-seen order: each ``INSERT`` target, a
            data-modifying CTE's included, whatever the statement's collected type, but
            not one that can overwrite the row it collides with (``ON CONFLICT ... DO
            UPDATE``, ``ON DUPLICATE KEY UPDATE``, MySQL's ``REPLACE``). ``ON CONFLICT
            DO NOTHING`` and ``INSERT IGNORE`` keep the row there and append. A
            ``MERGE``'s ``WHEN NOT MATCHED THEN INSERT`` is the ``MERGE``'s, an upsert.
    """

    join_count: int
    has_joins: bool
    has_aggregation: bool
    has_subquery: bool
    wildcard_leading: bool
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None
    dialect: str
    group_by: bool = False
    grouping_sets: bool = False
    aggregate_function_count: int = 0
    like_parameter: bool = False
    fulltext_search: bool = False
    pattern_match: bool = False
    similarity_match: bool = False
    prefix_regex: bool = False
    window_function: bool = False
    ranking_window_function: bool = False
    offset_window_function: bool = False
    windowed_aggregate: bool = False
    recursive_cte: bool = False
    order_by: bool = False
    row_limit: bool = False
    top_n: bool = False
    columns: tuple[ColumnUse, ...] = ()
    json_access: bool = False
    multi_table_write: bool = False
    wildcard_likes: tuple[WildcardLike, ...] = ()
    regex_matches: tuple[RegexMatch, ...] = ()
    write_targets: tuple[str, ...] = ()
    append_targets: tuple[str, ...] = ()

    @property
    def timestamp_column(self) -> bool:
        """A column named as a timestamp (the name list, ``regex_facts.TIMESTAMP_COLUMN_NAMES``)."""
        return any(TIMESTAMP_COLUMN_NAME_RE.fullmatch(c.name) for c in self.columns)


@dataclass(frozen=True)
class SqlReading:
    """Everything the facade reads from one piece of SQL text, from one parse: the
    table references, the query-shape facts and the access-pattern facts (the last two
    are what a collector stores on the query pattern, #934)."""

    tables: TableReferences
    facts: SqlFacts
    access: AccessFacts


def sqlglot_dialect(source_engine: str) -> str:
    """The sqlglot dialect for a source engine (``""`` = generic)."""
    return _DIALECTS.get((source_engine or "").strip().lower(), "")


def table_references(sql: str, source_engine: str) -> TableReferences:
    """The table references in ``sql``, read with the source engine's dialect.

    Never raises: unparseable text, a generic ``Command`` node, empty text, or
    text over ``MAX_SQL_CHARS`` falls back to the regex extractor and says so
    in ``fallback_reason``. Over the cap the text is not normalized either, so
    ``truncated`` is False.
    """
    return _tables(_parse(sql, source_engine))


def sql_facts(sql: str, source_engine: str) -> SqlFacts:
    """The query-shape facts of ``sql`` (:class:`SqlFacts`), read with the
    source engine's dialect; the regex facts when it falls back, exactly as
    :func:`table_references` does. Never raises."""
    return _facts(_parse(sql, source_engine))


def read_sql(sql: str, source_engine: str) -> SqlReading:
    """:func:`table_references`, :func:`sql_facts` and :func:`access_facts` from one
    parse."""
    parsed = _parse(sql, source_engine)
    return SqlReading(tables=_tables(parsed), facts=_facts(parsed), access=_access(parsed))


@dataclass(frozen=True)
class SqlTokens:
    """The words and punctuation of a piece of SQL text, read in the source engine's
    dialect (#471): what a feature check matches on, so a string literal, a quoted
    identifier (T-SQL ``[...]`` too) or a comment never counts as a keyword or a call.

    ``tokens`` are upper-cased, in text order: words (``@@IDENTITY`` and ``#tmp`` keep
    their sigils), numbers and single punctuation characters. ``calls`` are the words
    directly followed by ``(``, each also with its dotted qualifier when it has one
    (``DBMS_OUTPUT.PUT_LINE`` and ``PUT_LINE``).
    """

    tokens: tuple[str, ...]
    calls: frozenset[str]
    dialect: str
    # The called names written with no qualifier (``time_bucket(...)``, not
    # ``my_schema.time_bucket(...)``), so a check can tell an extension's function
    # from a user function of the same name.
    unqualified_calls: frozenset[str] = frozenset()


_TOKEN_RE = re.compile(r"@@?[A-Za-z_][\w$#@]*|#{1,2}[A-Za-z_][\w$#@]*|[A-Za-z_][\w$#]*|\d+|\S")
_BACKTICK_CALL_RE = re.compile(r"`([A-Za-z_][\w$]*)`(?=\s*\()")
_DOUBLE_QUOTE_CALL_RE = re.compile(r'"([A-Za-z_][\w$]*)"(?=\s*\()')


def _quoted_call_re(engine: str) -> re.Pattern[str]:
    """A quoted function name in ``engine``'s identifier quotes, followed by "("."""
    return _BACKTICK_CALL_RE if engine in ("mysql", "mariadb") else _DOUBLE_QUOTE_CALL_RE


def _is_word(token: str) -> bool:
    return token[:1].isalpha() or token[:1] in "_@#"


def sql_tokens(sql: str, source_engine: str) -> SqlTokens:
    """The :class:`SqlTokens` of ``sql`` for ``source_engine``. Never raises.

    No parse: the text is scanned with the dialect's own quoting and comment rules
    (:func:`src.tools.sql.normalize.quoted_mask`: backslash escapes and ``#`` comments
    for the MySQL family only, dollar quotes outside it, Oracle ``q'[...]'``), so it
    works on a routine body or a digest sqlglot cannot read.
    """
    text = sql or ""
    engine = (source_engine or "").strip().lower()
    # A SQL Server ``[name]`` is quoted in the same scan, so an apostrophe inside one
    # (``[O'Brien]``) opens no string (#1416).
    mask = quoted_mask(text, engine, brackets=engine == "sqlserver")
    chars = [" " if hidden else ch for ch, hidden in zip(text, mask, strict=True)]
    # A quoted name directly followed by "(" is still a call: a MySQL digest writes a
    # native function such as FOUND_ROWS as `FOUND_ROWS` ( ).
    for m in _quoted_call_re(engine).finditer(text):
        start, end = m.span(0)
        opens_here = mask[start] and (start == 0 or not mask[start - 1])
        if opens_here and all(mask[start:end]):
            chars[start:end] = f" {m.group(1)} "
    visible = "".join(chars)
    tokens = tuple(t.upper() for t in _TOKEN_RE.findall(visible))
    calls: set[str] = set()
    unqualified: set[str] = set()
    for i in range(len(tokens) - 1):
        if tokens[i + 1] != "(" or not _is_word(tokens[i]):
            continue
        name = tokens[i]
        calls.add(name)
        if not (i >= 1 and tokens[i - 1] == "."):
            unqualified.add(name)
        j = i
        while j >= 2 and tokens[j - 1] == "." and _is_word(tokens[j - 2]):
            name = f"{tokens[j - 2]}.{name}"
            j -= 2
        calls.add(name)
    return SqlTokens(
        tokens=tokens,
        calls=frozenset(calls),
        dialect=sqlglot_dialect(engine),
        unqualified_calls=frozenset(unqualified),
    )


@dataclass(frozen=True)
class _Parsed:
    """One parse: the statements, or why there are none (the fallback reason)."""

    sql: str
    dialect: str
    truncated: bool
    statements: tuple[exp.Expr, ...] = ()
    fallback_reason: FallbackReason | None = None
    # A T-SQL ``DELETE``/``UPDATE``/``INSERT TOP (n)`` the bridge dropped before
    # parsing: a row limit the tree no longer shows (``SqlFacts.row_limit``).
    dml_top_dropped: bool = False
    # A MySQL ``REPLACE INTO`` the bridge read as ``INSERT INTO``: the first statement
    # overwrites by key, so it appends nothing (``SqlFacts.append_targets``).
    replace_read_as_insert: bool = False
    # The digest was cut short and does not parse, but its prefix up to the last
    # complete predicate does (:func:`_repair_truncated`); only the access facts
    # read it (#986). Empty when there is none.
    repaired: tuple[exp.Expr, ...] = ()


# MySQL's default ``performance_schema_max_digest_length``: a digest this long was cut at
# the cap, whatever its last token is.
_MYSQL_DIGEST_CAP = 1024


def _cut_short(sql: str, normalized: NormalizedQuery, source_engine: str) -> bool:
    """Whether the collector really cut ``sql`` short, so its prefix may be read (#986): the
    normalizer flags it truncated, and the collector's own signal agrees: it ends on a
    token that cannot end a statement, or (MySQL) it is as long as the digest cap. An
    unterminated quote alone is not enough: a scanner can misread a dialect's string
    syntax, and a complete statement must never be repaired."""
    if not normalized.truncated:
        return False
    if normalized.ends_incomplete:
        return True
    return (source_engine or "").lower() in ("mysql", "mariadb") and len(sql) >= _MYSQL_DIGEST_CAP


def _parse(sql: str, source_engine: str) -> _Parsed:
    """Parse ``sql`` once, or say why the regex fallback answers instead."""
    # A comment after a ';' is no statement and no part of one (#1335, #1340).
    sql = without_comments_between_statements(sql or "", source_engine)
    dialect = sqlglot_dialect(source_engine)
    if len(sql) > MAX_SQL_CHARS:
        return _Parsed(sql, dialect, truncated=False, fallback_reason="too_long")
    normalized = normalize_digest_text(sql, source_engine or "")

    def fallback(reason: FallbackReason) -> _Parsed:
        repaired: tuple[exp.Expr, ...] = ()
        if reason == "parse_error" and _cut_short(sql, normalized, source_engine):
            repaired = _repair_truncated(text, dialect, source_engine)
        return _Parsed(
            sql, dialect, truncated=normalized.truncated, fallback_reason=reason, repaired=repaired
        )

    if not normalized.text.strip():
        return fallback("empty")
    text = normalized.text
    dml_top_dropped = replace_read_as_insert = False
    if dialect == "oracle":
        text = _plain_oracle_strings(text)
        text = _sub_outside_quotes(_ORACLE_POSITIONAL_BIND_RE, r":b\1", text, source_engine)
    elif dialect == "postgres":
        text = _sub_outside_quotes(_PG_PLACEHOLDER_RE, r"\1 ", text, source_engine)
    elif dialect == "tsql":
        bridged = _sub_outside_quotes(_TSQL_DML_TOP_RE, r"\1", text, source_engine)
        dml_top_dropped, text = bridged != text, bridged
    elif dialect == "mysql":
        # After the statement's leading comments (#1178): a tagged REPLACE is one too.
        body = strip_leading_comments(text, hash_comments=True, mysql_executable=True)
        bridged = _MYSQL_REPLACE_RE.sub(r"\1INSERT", body, count=1)
        replace_read_as_insert = bridged != body
        text = text[: len(text) - len(body)] + bridged
    try:
        statements = [s for s in sqlglot.parse(text, read=dialect or None) if s]
    except Exception:  # noqa: BLE001 -- any parser failure falls back, by contract
        return fallback("parse_error")
    if not statements:
        return fallback("empty")
    if any(isinstance(node, exp.Command) for s in statements for node in s.walk()):
        return fallback("command")
    if not all(isinstance(s, _STATEMENT_TYPES) for s in statements):
        return fallback("not_a_statement")
    return _Parsed(
        sql,
        dialect,
        truncated=normalized.truncated,
        statements=tuple(statements),
        dml_top_dropped=dml_top_dropped,
        replace_read_as_insert=replace_read_as_insert,
    )


def _plain_oracle_strings(text: str) -> str:
    """``text`` with each Oracle ``q'[...]'`` (``nq'[...]'``) string written as the plain
    ``'...'`` (``N'...'``) string it is, its quotes doubled (#1481).

    sqlglot's Oracle reader cannot read the alternative quoting, so a statement holding
    one would fall back to the regex reading, which reads the text inside as SQL. The
    spans are the shared Oracle grammar's (:mod:`src.shared.sql_spans`), so a ``q'`` in a
    comment, a plain string or a quoted name is left as written. The string's value is
    unchanged, so no table name or fact can move but the ones the fallback misread.
    """
    out: list[str] = []
    last = 0
    for start, end, opener in spans(text, literals_and_comments_for("oracle")):
        if not is_alternative_quote(opener):
            continue
        body = text[start + len(opener) : end - 2]
        national = "N" if opener[0] in "nN" else ""
        out.append(text[last:start])
        out.append(national + "'" + body.replace("'", "''") + "'")
        last = end
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _parse_or_none(text: str, dialect: str) -> list[exp.Expr] | None:
    """sqlglot's statements for ``text``, or None when it raises (a cut that does not
    parse is just not the one)."""
    try:
        return [s for s in sqlglot.parse(text, read=dialect or None) if s]
    except Exception:  # noqa: BLE001 -- any parser failure is None, by contract
        return None


def _repair_truncated(text: str, dialect: str, source_engine: str) -> tuple[exp.Expr, ...]:
    """The statements of a digest the collector cut short, read up to its last complete
    predicate, or ``()``.

    A long query digest is cut at a length cap, often in the middle of a ``WHERE`` or a
    join's ``ON``. The text is cut back to just before the last ``AND``/``OR`` that
    leaves a statement sqlglot reads (open parentheses closed), trying each from the
    end. A prefix has lost its later predicates, so it shows less than the query does:
    the caller says so wherever it uses it (#986). Text with no table in the prefix
    is not repaired.
    """
    mask = quoted_mask(text, source_engine or "")
    cuts = [m.start() for m in _AND_OR_RE.finditer(text) if not any(mask[m.start() : m.end()])]
    for cut in reversed(cuts[-_MAX_REPAIR_CUTS:]):
        head = text[:cut].rstrip()
        if not head:
            continue
        opened = sum(
            (ch == "(") - (ch == ")")
            for ch, quoted in zip(text[: len(head)], mask, strict=False)
            if not quoted
        )
        if opened < 0:
            continue
        statements = _parse_or_none(head + ")" * opened, dialect)
        if (
            statements
            and all(isinstance(s, _STATEMENT_TYPES) for s in statements)
            and not any(isinstance(n, exp.Command) for s in statements for n in s.walk())
            and any(isinstance(n, exp.Table) for s in statements for n in s.walk())
        ):
            return tuple(statements)
    return ()


def _tables(parsed: _Parsed) -> TableReferences:
    if parsed.fallback_reason is not None:
        return _fallback(
            parsed.sql, parsed.fallback_reason, parsed.dialect, truncated=parsed.truncated
        )
    return _from_statements(list(parsed.statements), parsed.dialect, parsed.truncated)


def _facts(parsed: _Parsed) -> SqlFacts:
    if parsed.fallback_reason is not None:
        return _regex_facts(parsed.sql, parsed.fallback_reason, parsed.dialect)
    return _facts_from_statements(
        parsed.statements,
        parsed.dialect,
        parsed.dml_top_dropped,
        replace_read_as_insert=parsed.replace_read_as_insert,
    )


def regex_answer(sql: str, source_engine: str, reason: FallbackReason) -> TableReferences:
    """The regex extractor's answer as a fallback with ``reason``, without
    trying sqlglot (for a caller whose :func:`table_references` call raised)."""
    return _fallback(sql or "", reason, sqlglot_dialect(source_engine), truncated=False)


def regex_reading(sql: str, source_engine: str, reason: FallbackReason) -> SqlReading:
    """:func:`regex_answer` and the regex facts, for a caller whose
    :func:`read_sql` call raised."""
    dialect = sqlglot_dialect(source_engine)
    return SqlReading(
        tables=_fallback(sql or "", reason, dialect, truncated=False),
        facts=_regex_facts(sql or "", reason, dialect),
        access=_access(_Parsed(sql or "", dialect, truncated=False, fallback_reason=reason)),
    )


def _fallback(
    sql: str, reason: FallbackReason, dialect: str, *, truncated: bool
) -> TableReferences:
    """The regex extractor's answer, labelled as a fallback."""
    refs = [_ref_from_regex(q, table) for q, table in regex_table_refs(sql, dialect)]
    return TableReferences(
        tables=tuple(dict.fromkeys(refs)),
        cte_names=(),
        parsed_by="regex",
        fallback_reason=reason,
        dialect=dialect,
        truncated=truncated,
    )


def _sub_outside_quotes(pattern: re.Pattern[str], repl: str, text: str, engine: str) -> str:
    """``pattern.sub(repl, text)`` for matches wholly outside literals,
    quoted identifiers and comments."""
    if pattern.search(text) is None:
        return text
    mask = quoted_mask(text, engine or "")

    def one(m: re.Match[str]) -> str:
        return m.group(0) if any(mask[m.start() : m.end()]) else m.expand(repl)

    return pattern.sub(one, text)


def _from_statements(statements: list[exp.Expr], dialect: str, truncated: bool) -> TableReferences:
    """The table references in parsed statements (see the module docstring)."""
    found: list[tuple[int, TableRef]] = []
    cte_names: list[str] = []
    for statement in statements:
        cte_names.extend(c.alias_or_name.lower() for c in statement.find_all(exp.CTE))
        aliases = _table_aliases(statement)
        for table in _statement_tables(statement):
            ref = _ref_from_node(table)
            if ref is None or _is_temp_table(table) or _is_cte_reference(table, ref):
                continue
            if _is_alias_reference(table, ref, aliases):
                continue
            found.append((_position(table), ref))
    found.sort(key=lambda item: item[0])
    return TableReferences(
        tables=tuple(dict.fromkeys(ref for _, ref in found)),
        cte_names=tuple(dict.fromkeys(cte_names)),
        parsed_by="sqlglot",
        fallback_reason=None,
        dialect=dialect,
        truncated=truncated,
    )


def _statement_tables(statement: exp.Expr) -> list[exp.Table]:
    """The table nodes of one statement that name a table or view.

    Almost every statement names its tables with every table node in it. ``ALTER``
    and ``DROP`` use table nodes for other names too: the new name of ``ALTER
    TABLE a RENAME TO b``, the index of ``DROP INDEX i ON t`` (sqlglot reads an
    index name as a table node; the table is in the ``ON`` clause), a sequence or a
    function. Only a table or view they act on is a reference (#483).
    """
    if isinstance(statement, exp.Alter):
        target = statement.this
        kind = str(statement.args.get("kind") or "").upper()
        return [target] if isinstance(target, exp.Table) and kind in _TABLE_KINDS else []
    if isinstance(statement, exp.Drop):
        kind = str(statement.args.get("kind") or "").upper()
        if kind in _TABLE_KINDS:
            return [t for t in statement.args.get("tables") or [] if isinstance(t, exp.Table)]
        if kind == "INDEX":
            on = statement.args.get("cluster")  # DROP INDEX i ON t (MySQL, T-SQL)
            target = on.this if isinstance(on, exp.OnProperty) else None
            return [target] if isinstance(target, exp.Table) else []
        return []
    return list(statement.find_all(exp.Table))


def _ref_from_regex(qualifiers: list[str], table: str) -> TableRef:
    if len(qualifiers) == 2:
        return TableRef(name=table, schema=qualifiers[1], catalog=qualifiers[0])
    if len(qualifiers) == 1:
        return TableRef(name=table, schema=qualifiers[0])
    return TableRef(name=table)


def _ref_from_node(table: exp.Table) -> TableRef | None:
    """A TableRef for a stored-table node; None for table functions and DUAL."""
    if not isinstance(table.this, exp.Identifier) or not table.name:
        return None
    ref = TableRef(
        name=table.name,
        schema=table.db or None,
        catalog=table.catalog or None,
        quoted=bool(table.this.args.get("quoted")),
    )
    if not ref.qualifiers and ref.name.lower() in _PSEUDO_TABLES:
        return None
    return ref


def _table_aliases(statement: exp.Expr) -> frozenset[str]:
    """The aliases (lower-case) the statement gives its tables."""
    return frozenset(t.alias.lower() for t in statement.find_all(exp.Table) if t.alias)


def _is_alias_reference(table: exp.Table, ref: TableRef, aliases: frozenset[str]) -> bool:
    """An unqualified, unaliased table node that names another table's alias: the
    target of T-SQL's ``UPDATE t SET ... FROM [Ticket] t`` or ``DELETE t FROM [Ticket]
    t`` (#893 review), which names the table by its alias. A table that really has
    the name keeps it, since no table in the statement is aliased to it then."""
    return not ref.qualifiers and not table.alias and ref.name.lower() in aliases


def _is_temp_table(table: exp.Table) -> bool:
    """A T-SQL ``#local`` or ``##global`` temp table (sqlglot drops the ``#``)."""
    ident = table.this
    return bool(ident.args.get("temporary") or ident.args.get("global_"))


def _position(table: exp.Table) -> int:
    start = table.this.meta.get("start") if isinstance(table.this, exp.Identifier) else None
    return int(start) if isinstance(start, int) else 1 << 62


def _cte_names(ctes: list[exp.CTE]) -> set[str]:
    return {c.alias_or_name.lower() for c in ctes}


def _is_cte_reference(table: exp.Table, ref: TableRef) -> bool:
    """Whether an unqualified table node names a CTE visible where it stands.

    Walks up from the table. A statement that owns a WITH list makes every CTE
    in it visible to its body. Reaching a CTE body instead makes only the CTEs
    listed before it visible (plus itself when recursive); the walk then skips
    that WITH's owning statement and carries on outward, where an enclosing
    query's own WITH list may still apply.
    """
    if ref.qualifiers:
        return False
    name = ref.name.lower()
    node = table.parent
    while node is not None:
        if isinstance(node, exp.CTE) and isinstance(node.parent, exp.With):
            with_node = node.parent
            ctes = list(with_node.expressions)
            index = next(i for i, c in enumerate(ctes) if c is node)
            recursive = bool(with_node.args.get("recursive")) or node.find(exp.Union) is not None
            if name in _cte_names(ctes[: index + 1] if recursive else ctes[:index]):
                return True
            owner = with_node.parent
            node = owner.parent if owner is not None else None
            continue
        owned = node.args.get("with_")
        if isinstance(owned, exp.With) and name in _cte_names(list(owned.expressions)):
            return True
        node = node.parent
    return False


# ---------------------------------------------------------------------------
# Query-shape facts (#796)
# ---------------------------------------------------------------------------

# Window functions that rank or offset rows and combine no values: sqlglot
# models most of them as aggregate functions, but alone they aggregate nothing.
_RANKING_WINDOW_FUNCTIONS: tuple[type[exp.Expr], ...] = (
    exp.RowNumber,
    exp.Rank,
    exp.DenseRank,
    exp.PercentRank,
    exp.CumeDist,
    exp.Ntile,
    exp.Lag,
    exp.Lead,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
)

# The window functions ``SqlFacts.ranking_window_function`` and
# ``offset_window_function`` name.
_RANKING_FUNCTIONS: tuple[type[exp.Expr], ...] = (exp.RowNumber, exp.Rank, exp.DenseRank)
_OFFSET_FUNCTIONS: tuple[type[exp.Expr], ...] = (exp.Lag, exp.Lead, exp.Ntile)

# Pattern-match predicates whose pattern is ``.expression``.
_LIKE_NODES: tuple[type[exp.Expr], ...] = (exp.Like, exp.ILike)


def _regex_facts(sql: str, reason: FallbackReason, dialect: str) -> SqlFacts:
    """The regex facts (:mod:`src.tools.sql.regex_facts`), labelled as a fallback.

    Read from the text without its comments, as sqlglot reads it: a keyword in a
    comment is not part of the statement (#483)."""
    sql = strip_comments(sql, nested=block_comments_nest(dialect), hash_comments=dialect == "mysql")
    return SqlFacts(
        join_count=regex_join_count(sql),
        has_joins=regex_has_joins(sql),
        has_aggregation=regex_has_aggregation(sql),
        has_subquery=regex_has_subquery(sql),
        wildcard_leading=regex_wildcard_leading(sql),
        wildcard_likes=regex_wildcard_likes(sql),
        parsed_by="regex",
        fallback_reason=reason,
        dialect=dialect,
        group_by=regex_group_by(sql),
        grouping_sets=regex_grouping_sets(sql, dialect),
        aggregate_function_count=regex_aggregate_function_count(sql),
        like_parameter=regex_like_parameter(sql),
        fulltext_search=regex_fulltext_search(sql),
        pattern_match=regex_pattern_match(sql),
        similarity_match=regex_similarity_match(sql),
        window_function=regex_window_function(sql),
        ranking_window_function=regex_ranking_window_function(sql),
        offset_window_function=regex_offset_window_function(sql),
        windowed_aggregate=regex_windowed_aggregate(sql),
        recursive_cte=regex_recursive_cte(sql, dialect),
        order_by=regex_order_by(sql),
        row_limit=regex_row_limit(sql),
        top_n=regex_top_n(sql),
        columns=tuple(
            ColumnUse(name, from_regex=True)
            for name in dict.fromkeys(regex_timestamp_column_names(sql))
        ),
        json_access=regex_json_access(sql),
        multi_table_write=regex_multi_table_write(sql),
        write_targets=regex_write_targets(sql, dialect),
        append_targets=regex_append_targets(sql, dialect),
    )


def _is_grouping_sets(node: exp.Expr) -> bool:
    """A ``GROUP BY`` with several groupings: ``GROUPING SETS``, ``ROLLUP``, ``CUBE`` or
    MySQL's ``WITH ROLLUP``."""
    if not isinstance(node, exp.Group):
        return False
    if node.args.get("rollup") or node.args.get("cube") or node.args.get("grouping_sets"):
        return True
    return any(isinstance(e, (exp.GroupingSets, exp.Rollup, exp.Cube)) for e in node.expressions)


def _facts_from_statements(
    statements: tuple[exp.Expr, ...],
    dialect: str,
    dml_top_dropped: bool = False,
    *,
    replace_read_as_insert: bool = False,
) -> SqlFacts:
    """The facts of parsed statements (see :class:`SqlFacts`)."""
    nodes = [node for statement in statements for node in statement.walk()]
    join_count = sum(
        1 for node in nodes if isinstance(node, exp.Join) and _joins_a_row_source(node)
    )
    group_by = any(isinstance(node, exp.Group) for node in nodes)
    aggregate_function_count = sum(1 for node in nodes if _is_aggregate_function(node))
    kinds = {_text_search_kind(node) for node in nodes}
    likes = tuple(like for statement in statements for like in _wildcard_likes(statement))
    regexes = (
        tuple(match for statement in statements for match in _regex_matches(statement))
        if "pattern" in kinds
        else ()
    )
    windows = [node.this for node in nodes if isinstance(node, exp.Window)]
    return SqlFacts(
        join_count=join_count,
        has_joins=join_count > 0,
        has_aggregation=group_by or aggregate_function_count > 0,
        has_subquery=sum(1 for node in nodes if isinstance(node, exp.Select)) > 1,
        wildcard_leading=bool(likes),
        wildcard_likes=likes,
        regex_matches=regexes,
        parsed_by="sqlglot",
        fallback_reason=None,
        dialect=dialect,
        group_by=group_by,
        grouping_sets=any(_is_grouping_sets(node) for node in nodes),
        aggregate_function_count=aggregate_function_count,
        like_parameter="parameter" in kinds,
        fulltext_search="fulltext" in kinds,
        pattern_match="pattern" in kinds,
        similarity_match="similarity" in kinds,
        prefix_regex=_all_prefix_regexes(nodes, dialect),
        window_function=_has_window(nodes),
        ranking_window_function=any(isinstance(w, _RANKING_FUNCTIONS) for w in windows),
        offset_window_function=any(isinstance(w, _OFFSET_FUNCTIONS) for w in windows),
        windowed_aggregate=_has_windowed_aggregate(nodes),
        recursive_cte=any(_is_recursive_with(node) for node in nodes),
        order_by=any(
            isinstance(n, exp.Order) and not isinstance(n.parent, exp.Window) for n in nodes
        ),
        row_limit=dml_top_dropped or any(isinstance(node, _ROW_LIMIT_NODES) for node in nodes),
        top_n=any(
            isinstance(node, exp.Query) and node.args.get("order") and node.args.get("limit")
            for node in nodes
        ),
        columns=_column_uses(statements),
        json_access=any(_is_json_access(node) for node in nodes),
        multi_table_write=any(_is_multi_table_write(node) for node in nodes),
        write_targets=_write_targets(statements),
        append_targets=_append_targets(statements, replace_read_as_insert),
    )


_ROW_LIMIT_NODES: tuple[type[exp.Expr], ...] = (exp.Limit, exp.Fetch)
# sqlglot's JSON functions and operators (``JSONExtract`` for ``->``, ``JSONValue``,
# ...), whatever the dialect names them; the ``JSONPath*`` nodes only appear inside
# one of them. PostgreSQL's ``?``/``?|``/``?&`` key-existence operators are left
# out: sqlglot reads ``?`` as one in every dialect but MySQL, where a digest's ``?``
# is a bind parameter, and none of the detectors this fact replaced read them.
_JSON_KEY_EXISTS_NODES = frozenset(
    {"JSONBContainsTopKey", "JSONBContainsAnyTopKeys", "JSONBContainsAllTopKeys"}
)
_JSON_NODES: tuple[type[exp.Expr], ...] = tuple(
    node
    for name, node in vars(exp).items()
    if name.startswith("JSON")
    and name not in _JSON_KEY_EXISTS_NODES
    and isinstance(node, type)
    and issubclass(node, exp.Expr)
) + (exp.OpenJSON,)


def _column_uses(statements: tuple[exp.Expr, ...]) -> tuple[ColumnUse, ...]:
    """Every column of the statements with its candidate tables (``SqlFacts.columns``)."""
    uses: list[ColumnUse] = []
    for statement in statements:
        # Every CTE name, so a reference to one is a derived source, never a table.
        ctes = {c.alias_or_name.lower(): -1 for c in statement.find_all(exp.CTE)}
        levels: dict[int, _ColumnScope | None] = {}
        for node in statement.walk():
            if isinstance(node, exp.Column):
                if node.name and not isinstance(node.this, exp.Star) and not _is_bind(node):
                    uses.append(ColumnUse(node.name, _column_scopes(node, levels, ctes)))
            elif isinstance(node, exp.Insert) and isinstance(node.this, exp.Schema):
                target = node.this.this
                ref = _ref_from_node(target) if isinstance(target, exp.Table) else None
                scopes = ((ref,),) if ref is not None else ()
                uses.extend(
                    ColumnUse(c.name, scopes)
                    for c in node.this.expressions
                    if isinstance(c, exp.Identifier)
                )
    return tuple(dict.fromkeys(uses))


# The nodes whose sources a column inside them reads.
_COLUMN_SCOPE_NODES: tuple[type[exp.Expr], ...] = (
    exp.Select,
    exp.Update,
    exp.Delete,
    exp.Insert,
    exp.Merge,
)


@dataclass(frozen=True)
class _ColumnScope:
    """One scope's row sources: each alias (and table name) -> its table, or None
    for a derived table, CTE or table function; ``closed``: such a source is read,
    so an unqualified column none of the tables holds is one of its columns."""

    by_alias: dict[str, TableRef | None]
    tables: tuple[TableRef, ...]
    closed: bool


def _column_scope(root: exp.Expr, ctes: dict[str, int]) -> _ColumnScope | None:
    """The sources a column in ``root`` reads, or None when ``root`` names none of its
    own (a ``MERGE``'s ``WHEN NOT MATCHED THEN INSERT``, which writes the target)."""
    if isinstance(root, exp.Insert):
        target = root.this.this if isinstance(root.this, exp.Schema) else root.this
        if not isinstance(target, exp.Table):
            return None
        sources = [s for s in (_source(target, {}, ctes),) if s is not None]
    elif isinstance(root, exp.Merge):
        nodes = [n for n in (root.this, root.args.get("using")) if n is not None]
        sources = [s for n in nodes if (s := _source(n, {}, ctes)) is not None]
    else:
        sources, _joins = _scope_sources(root, {}, ctes)
    # An alias a source declares wins over a bare table name: T-SQL's ``UPDATE t ...
    # FROM [Ticket] t`` names its target by the FROM list's alias.
    aliased = [s for s in sources if s.table is None or s.alias != s.table.name.lower()]
    by_alias: dict[str, TableRef | None] = {}
    for source in aliased + [s for s in sources if s not in aliased]:
        if source.alias:
            by_alias.setdefault(source.alias, source.table)
        if source.table is not None:
            by_alias.setdefault(source.table.name.lower(), source.table)
    tables = tuple(dict.fromkeys(s.table for s in sources if s.table is not None))
    return _ColumnScope(by_alias, tables, closed=any(s.table is None for s in sources))


def _set_target(
    column: exp.Column, levels: dict[int, _ColumnScope | None], ctes: dict[str, int]
) -> TableRef | None:
    """The updated table, for an unqualified column an ``UPDATE``'s ``SET`` assigns: a
    ``SET`` target is the target table's column, whatever the ``FROM`` list reads (the
    target named through its alias, T-SQL's ``UPDATE t ... FROM [Ticket] t``)."""
    assign = column.parent
    update = assign.parent if assign is not None else None
    if (
        column.table
        or not isinstance(assign, exp.EQ)
        or assign.this is not column
        or not isinstance(update, exp.Update)
        or not isinstance(update.this, exp.Table)
    ):
        return None
    if id(update) not in levels:
        levels[id(update)] = _column_scope(update, ctes)
    level = levels[id(update)]
    name = update.this.alias_or_name.lower()
    return level.by_alias.get(name) if level is not None else None


def _column_scopes(
    column: exp.Column, levels: dict[int, _ColumnScope | None], ctes: dict[str, int]
) -> tuple[tuple[TableRef, ...], ...]:
    """``ColumnUse.scopes`` for one column node."""
    target = _set_target(column, levels, ctes)
    if target is not None:
        return ((target,),)
    scopes: list[_ColumnScope] = []
    node = column.parent
    while node is not None:
        if isinstance(node, _COLUMN_SCOPE_NODES):
            if id(node) not in levels:
                levels[id(node)] = _column_scope(node, ctes)
            if (level := levels[id(node)]) is not None:
                scopes.append(level)
        node = node.parent
    qualifier = column.table.lower() if column.table else ""
    if qualifier:
        for scope in scopes:
            if qualifier in scope.by_alias:
                ref = scope.by_alias[qualifier]
                return ((ref,),) if ref is not None else ()
        return ()
    found: list[tuple[TableRef, ...]] = []
    for scope in scopes:
        found.append(scope.tables)
        if scope.closed:
            break
    return tuple(found)


def _is_json_access(node: exp.Expr) -> bool:
    if isinstance(node, _JSON_NODES):
        return True
    if isinstance(node, exp.ForClause):
        return str(node.args.get("kind") or "").upper() == "JSON"
    if isinstance(node, exp.Cast | exp.TryCast):
        return bool(node.to.is_type(exp.DataType.Type.JSON, exp.DataType.Type.JSONB))
    if isinstance(node, exp.ArrayContainsAll):  # PostgreSQL ``@>`` on a JSON document
        return any(_is_json_document_literal(side) for side in node.args.values())
    if isinstance(node, exp.Anonymous):
        # A json* call, or SQL Server's ISJSON (review of #1118).
        name = node.name.lower()
        return bool(name.startswith("json") or name == "isjson")
    return False


def _is_json_document_literal(node: object) -> bool:
    """A string literal that is a JSON document, the operand of a JSON ``@>``. A
    PostgreSQL array literal such as ``'{a,b}'`` on a ``text[]`` column is not JSON
    (review of #1118); ``'{}'`` is both, and stays JSON."""
    if not (isinstance(node, exp.Literal) and node.is_string):
        return False
    try:
        return isinstance(json.loads(node.this), dict | list)
    except ValueError:
        return False


def _is_multi_table_write(node: exp.Expr) -> bool:
    """An UPDATE or DELETE whose rows come from a join with another table
    (``SqlFacts.multi_table_write``)."""
    if isinstance(node, exp.Update):
        joined = isinstance(node.this, exp.Table) and bool(node.this.args.get("joins"))
        source = node.args.get("from_")
        return joined or (source is not None and not _names_only_the_target(node, source))
    if isinstance(node, exp.Delete):
        using = node.args.get("using")
        joined = isinstance(node.this, exp.Table) and bool(node.this.args.get("joins"))
        return bool(node.args.get("tables")) or joined or (isinstance(using, list) and bool(using))
    return False


_WRITE_NODES: tuple[type[exp.Expr], ...] = (exp.Insert, exp.Update, exp.Delete, exp.Merge)


def _write_targets(statements: Iterable[exp.Expr]) -> tuple[str, ...]:
    """The tables the statements write (``SqlFacts.write_targets``)."""
    out: list[str] = []
    for statement in statements:
        aliases = {
            t.alias.lower(): t.name.lower()
            for t in statement.find_all(exp.Table)
            if t.alias and t.name
        }
        for node in statement.walk():
            if isinstance(node, exp.TruncateTable):
                targets = list(node.expressions)
            elif isinstance(node, exp.Update) and _set_qualifiers(node):
                # MySQL's multi-table ``UPDATE a JOIN b ... SET a.x = ?, b.y = ?`` (or
                # ``UPDATE a, b SET ...``) writes each table its SET names.
                out.extend(aliases.get(q, q) for q in _set_qualifiers(node))
                columns = [c for c in map(_set_column, node.expressions) if c is not None]
                if any(not c.table for c in columns):
                    targets = [node.this]
                else:
                    continue
            elif isinstance(node, _WRITE_NODES):
                # MySQL's ``DELETE a, b FROM ...`` names its targets in ``tables``.
                targets = list(node.args.get("tables") or []) or [node.this]
            else:
                continue
            for target in targets:
                if isinstance(target, exp.Schema):
                    target = target.this
                # A MERGE's ``WHEN MATCHED THEN UPDATE`` has no table of its own.
                if not isinstance(target, exp.Table) or not target.name:
                    continue
                name = target.name.lower()
                if not target.db and not target.alias and name in aliases:
                    name = aliases[name]
                out.append(name)
    return tuple(dict.fromkeys(out))


def _append_targets(
    statements: tuple[exp.Expr, ...], replace_read_as_insert: bool
) -> tuple[str, ...]:
    """The tables the statements only append rows to (``SqlFacts.append_targets``)."""
    out: list[str] = []
    for index, statement in enumerate(statements):
        for node in statement.find_all(exp.Insert):
            if replace_read_as_insert and index == 0 and node is statement:
                continue  # MySQL's REPLACE: it deletes the row with the same key first
            if _overwrites_on_conflict(node):
                continue
            target = node.this.this if isinstance(node.this, exp.Schema) else node.this
            # A MERGE's ``WHEN NOT MATCHED THEN INSERT`` names columns, not a table.
            if isinstance(target, exp.Table) and target.name:
                out.append(target.name.lower())
    return tuple(dict.fromkeys(out))


def _overwrites_on_conflict(insert: exp.Insert) -> bool:
    """``ON DUPLICATE KEY UPDATE`` or ``ON CONFLICT ... DO UPDATE``; ``DO NOTHING`` keeps
    the row that is there."""
    conflict = insert.args.get("conflict")
    if not isinstance(conflict, exp.OnConflict):
        return False
    action = str(getattr(conflict.args.get("action"), "this", "") or "").upper()
    return bool(conflict.args.get("duplicate")) or action != "DO NOTHING"


def _set_column(assignment: exp.Expr) -> exp.Column | None:
    """The column an ``UPDATE``'s ``SET`` assignment writes."""
    if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column):
        return assignment.this
    return None


def _set_qualifiers(update: exp.Update) -> list[str]:
    """The table qualifiers (lower-case) of an ``UPDATE``'s ``SET`` columns, in order."""
    out: list[str] = []
    for assignment in update.expressions:
        column = _set_column(assignment)
        if column is not None and column.table and column.table.lower() not in out:
            out.append(column.table.lower())
    return out


def _names_only_the_target(update: exp.Update, source: exp.From) -> bool:
    """A SQL Server ``UPDATE p SET ... FROM [dbo].[Product] p``: the ``FROM`` names the
    target again (by its name or the alias the target is written as) and reads no other
    table, so the write is single-table (#983 review)."""
    table = source.this
    if not isinstance(table, exp.Table) or table.args.get("joins") or source.expressions:
        return False
    target = update.this.name.lower() if isinstance(update.this, exp.Table) else ""
    # The same instance: the FROM's alias is the target, or it has no alias and its
    # name is. ``UPDATE posts SET ... FROM posts p2`` is a self-join, a second instance.
    return bool(target) and target == (table.alias or table.name).lower()


def _has_window(nodes: Iterable[exp.Expr]) -> bool:
    """A function over a window (``SqlFacts.window_function``, ``AccessFacts.window_function``)."""
    return any(isinstance(node, exp.Window) for node in nodes)


def _has_windowed_aggregate(nodes: Iterable[exp.Expr]) -> bool:
    """An aggregate function over a window (``SqlFacts.windowed_aggregate``,
    ``AccessFacts.windowed_aggregate``)."""
    return any(isinstance(node, exp.Window) and _is_aggregation(node.this) for node in nodes)


def _is_recursive_with(node: exp.Expr) -> bool:
    """``WITH RECURSIVE``, or a CTE of this ``WITH`` whose body has a ``UNION``
    and reads the CTE's own (unqualified) name."""
    if not isinstance(node, exp.With):
        return False
    if node.args.get("recursive"):
        return True
    for cte in node.expressions:
        name = cte.alias_or_name.lower()
        body = cte.this
        if body is None or body.find(exp.Union) is None:
            continue
        if any(
            not t.db and not t.catalog and t.name.lower() == name for t in body.find_all(exp.Table)
        ):
            return True
    return False


def _joins_a_row_source(join: exp.Join) -> bool:
    """Whether a join reads a stored table or a subquery, not only a table function."""
    source = join.this
    if isinstance(source, exp.Table):
        return _ref_from_node(source) is not None or source.find(exp.Select) is not None
    return source.find(exp.Select) is not None


def _is_aggregate_function(node: exp.Expr) -> bool:
    """A function that combines many rows' values into one (not a ranking or offset one)."""
    return isinstance(node, exp.AggFunc) and not isinstance(node, _RANKING_WINDOW_FUNCTIONS)


def _is_aggregation(node: exp.Expr) -> bool:
    return isinstance(node, exp.Group) or _is_aggregate_function(node)


def _starts_with_wildcard(pattern: exp.Expr | None) -> bool:
    """Whether a LIKE pattern's first part is a string literal starting with ``%``."""
    while pattern is not None:
        if isinstance(pattern, exp.Literal):
            return bool(pattern.is_string and str(pattern.this).startswith("%"))
        if isinstance(pattern, exp.National):  # SQL Server N'%...' (#493)
            return str(pattern.this).startswith("%")
        if isinstance(pattern, exp.Paren | exp.DPipe | exp.Add):
            pattern = pattern.this
        elif isinstance(pattern, exp.Concat | exp.ConcatWs):
            parts = list(pattern.expressions)
            if isinstance(pattern, exp.ConcatWs):
                parts = parts[1:]  # the first argument is the separator
            pattern = parts[0] if parts else None
        else:
            return False
    return False


# ---------------------------------------------------------------------------
# Access-pattern facts (#728, ADR-031 Decision 14 Amendment 5)
#
# What a statement reads, per SELECT scope, as plain facts: which tables it
# reads (by alias), which columns it pins to a value, which it bounds by a
# range, which column pairs join two of its tables, and its order, row limit,
# grouping and aggregates. No schema knowledge and no class: the caller
# (``src.agents.referee.access_pattern``) adds the keys and decides the class.
# ---------------------------------------------------------------------------

StatementKind = Literal["select", "insert", "update", "delete", "merge", "other"]

# Full-text and similarity functions, whatever the dialect calls them (#893:
# :func:`_text_search_kind` sorts every text-search node into one kind).
_FULLTEXT_FUNCTIONS = frozenset(
    {
        "to_tsvector",
        "to_tsquery",
        "plainto_tsquery",
        "phraseto_tsquery",
        "websearch_to_tsquery",
        "ts_rank",
        "ts_rank_cd",
        "freetext",
        "freetexttable",
        "containstable",
    }
)
_SIMILARITY_FUNCTIONS = frozenset({"similarity", "word_similarity", "strict_word_similarity"})
_FULLTEXT_NODES: tuple[type[exp.Expr], ...] = (exp.MatchAgainst, exp.Contains)
_PATTERN_MATCH_NODES: tuple[type[exp.Expr], ...] = (
    exp.RegexpLike,
    exp.RegexpILike,
    exp.SimilarTo,
)
_RANGE_NODES: tuple[type[exp.Expr], ...] = (exp.GT, exp.GTE, exp.LT, exp.LTE)
# T-SQL table hints that skip rows another transaction holds (like SKIP LOCKED).
_ROW_SKIPPING_HINTS = frozenset({"READPAST"})


@dataclass(frozen=True)
class ColumnFact:
    """A column as written: its qualifier (alias or table, lower-case) and name."""

    qualifier: str | None
    name: str


@dataclass(frozen=True)
class ScopeSource:
    """One table a scope reads, under the alias the scope uses for it.

    ``table`` is None for a derived table, a table function or a CTE; ``derived``
    is then the index (in :attr:`AccessFacts.scopes`) of the scope that produces
    its rows, when there is exactly one. ``optional``: the source is the
    optional side of a ``LEFT`` or ``FULL`` join (its rows may be missing).
    ``function``: a table function or a function of another source's row
    (``unnest(...)``, ``CROSS APPLY OPENJSON(...)``, ``generate_series(...)``),
    which reads no stored table.
    """

    alias: str
    table: TableRef | None
    derived: int | None = None
    optional: bool = False
    function: bool = False


# The text searches :func:`_text_search_kind` tells apart (#893).
TextSearchKind = Literal["leading_wildcard", "parameter", "fulltext", "pattern", "similarity"]


@dataclass(frozen=True)
class ScopeFacts:
    """The facts of one SELECT (or UPDATE/DELETE) scope.

    ``equalities``: columns pinned to a value by a top-level ``AND`` term of the
    WHERE or a JOIN condition (``col = <value>``, ``col IN (...)``, ``col =
    ANY(...)``); the value holds no column of this scope (a literal, a
    placeholder, a subquery or an outer column). ``ranges``: ``<``, ``<=``,
    ``>``, ``>=``, ``BETWEEN`` and a literal-prefix ``LIKE 'abc%'``. ``joins``:
    ``col = col`` terms over two of the scope's columns, plus ``JOIN ... USING``.
    ``other_filters``: every other predicate term (an ``OR`` group, ``NOT IN``,
    ``IS NULL``, a function test, ...). ``order_by``: each sort item's column,
    or None for an expression. ``limit``: the row count as written (digits), or
    ``"?"`` for a placeholder, ``PERCENT`` or ``WITH TIES`` (not a row count);
    None with no limit. ``skip_locked``: ``SKIP LOCKED`` or a ``READPAST`` hint.
    ``text_search``: in this scope's own predicates or projection (not a nested
    query's), a ``LIKE``/``ILIKE`` whose pattern starts with a wildcard
    (``wildcard_leading``, the same definition as :class:`SqlFacts`), a
    regular-expression or ``SIMILAR TO`` match, ``MATCH ... AGAINST``, ``@@``,
    ``CONTAINS`` or a full-text or similarity function. ``like_parameter``: a
    ``LIKE``/``ILIKE`` whose whole pattern is a bind parameter (``LIKE ?``): its
    value is unknown, so it is neither a leading wildcard nor a prefix.
    ``text_search_kinds``: which of those this scope's own predicates and projection
    hold (:data:`TextSearchKind`, the one definition behind ``text_search`` and
    ``like_parameter``), so a class score can rate each kind by its own access path
    (#920).
    ``time_bucket``: a ``GROUP BY`` item (written out, or a projection named by its
    alias or position) that truncates or extracts a date/time: ``date_trunc``,
    ``TRUNC``, ``DATE()``, a cast to ``date``, ``YEAR``/``MONTH``/``DAY``/...,
    ``EXTRACT``, ``date_part``/``DATEPART``, ``to_char``/``DATE_FORMAT``,
    ``strftime`` (#920). Grouping on a raw timestamp is not one.
    ``or_branches``: when the WHERE's only ``OR`` group is a disjunction of
    branches made only of ``col = <value>`` terms (``id = $1 OR id = $2``), the
    columns each branch pins, one tuple per branch (#915); empty otherwise. The
    ``OR`` group itself still counts in ``other_filters``. ``null_tests`` /
    ``not_null_tests``: the columns a top-level ``AND`` term tests with ``IS NULL`` /
    ``IS NOT NULL`` (#1231); each term still counts in ``other_filters``. ``correlated``:
    the ``equalities`` whose value is another scope's column (``p.col = outer.col``, a
    correlated join edge rather than a value, #1231).
    ``count_only``: the scope's own aggregates (``aggregate``) are all plain counts,
    ``COUNT(*)``, ``COUNT(1)`` or ``COUNT(col)`` (non-null values), and no ``COUNT(DISTINCT ...)``,
    filtered count (``FILTER (WHERE ...)``), ``MIN``, ``MAX``, ``SUM`` or ``AVG`` (#377).
    ``returned``: for a SELECT scope, the columns its projection names, in order
    (inside an expression or an aggregate too, not inside a nested query), and
    ``returns_all``: the projection has ``*`` or ``alias.*``. They are what the scope
    returns, not how it reads, so no access-pattern signature holds them (#1146).
    ``group_by_items``: each ``GROUP BY`` item's shape (a projection named by its
    alias or position resolved to its expression), in order; ``having``: each
    top-level ``AND`` term of ``HAVING`` as a shape; ``aggregate_kinds``: the aggregate
    functions of the scope's own projection, ``HAVING`` and ``ORDER BY`` (``count(*)``,
    ``count``, ``sum``, ...), sorted. A shape names a column by its name (no
    qualifier), a value or bind parameter as ``?`` and anything else as its node kind
    over its parts' shapes (``year(post_date)``, ``gt(count(*),?)``), so it holds no
    literal (#1146). They tell two aggregates over the same rows apart; the scope's
    access-pattern signature (``access_signature.scope_signature``) does not read them.
    """

    sources: tuple[ScopeSource, ...]
    equalities: tuple[ColumnFact, ...] = ()
    ranges: tuple[ColumnFact, ...] = ()
    joins: tuple[tuple[ColumnFact, ColumnFact], ...] = ()
    other_filters: int = 0
    order_by: tuple[ColumnFact | None, ...] = ()
    limit: str | None = None
    has_where: bool = False
    group_by: bool = False
    aggregate: bool = False
    count_only: bool = False
    skip_locked: bool = False
    text_search: bool = False
    like_parameter: bool = False
    time_bucket: bool = False
    text_search_kinds: frozenset[TextSearchKind] = frozenset()
    or_branches: tuple[tuple[ColumnFact, ...], ...] = ()
    null_tests: tuple[ColumnFact, ...] = ()
    not_null_tests: tuple[ColumnFact, ...] = ()
    correlated: tuple[ColumnFact, ...] = ()
    returned: tuple[ColumnFact, ...] = ()
    returns_all: bool = False
    group_by_items: tuple[str, ...] = ()
    having: tuple[str, ...] = ()
    aggregate_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessFacts:
    """The access-pattern facts of one piece of SQL text.

    ``kind`` is the first statement's kind (``other`` for SET, SHOW,
    transaction control, DDL, ...). ``inserts_values``: an ``INSERT ... VALUES``
    (or ``DEFAULT VALUES``). ``windowed_aggregate``: a window function that
    combines values (``SUM(...) OVER``, ``COUNT(*) OVER ()``) anywhere; the
    ranking and offset window functions (``ROW_NUMBER``, ``RANK``, ``LAG``, ...)
    combine nothing, so alone they are not one (the ``has_aggregation``
    definition of :class:`SqlFacts`). ``window_function``: any function over a
    window, ranking ones included (``SqlFacts.window_function``). When the facade falls back, ``parsed_by``
    is ``"regex"`` and ``scopes`` is empty: no fact is guessed from the text, except
    for a digest the collector cut short: ``truncated_read`` is True when the facts
    are those of its text up to the last complete predicate (#986); they show less than
    the query does, so a reader says so.
    """

    kind: StatementKind
    scopes: tuple[ScopeFacts, ...]
    inserts_values: bool
    windowed_aggregate: bool
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None
    window_function: bool = False
    truncated_read: bool = False
    # The columns an UPDATE's SET assigns (lower-case names, #1231): a redesign never
    # keys a column its own write rewrites.
    assigned_columns: tuple[str, ...] = ()


def query_readings(sql: str, source_engine: str) -> tuple[SqlFacts, AccessFacts]:
    """:func:`sql_facts` and :func:`access_facts` from one parse (the query facts'
    reader, ``src.tools.sql.query_facts``). Never raises."""
    parsed = _parse(sql, source_engine)
    return _facts(parsed), _access(parsed)


def access_facts(sql: str, source_engine: str) -> AccessFacts:
    """The access-pattern facts of ``sql``, read with the source engine's dialect.

    Never raises; text the facade cannot parse comes back with
    ``parsed_by="regex"``, the reason and no scopes. A ``SHOW`` (which sqlglot
    reads as a generic command) is kind ``other``.
    """
    return _access(_parse(sql, source_engine))


def _access(parsed: _Parsed) -> AccessFacts:
    reason = parsed.fallback_reason
    if reason is not None and parsed.repaired:
        repaired = _read_access(parsed.repaired, truncated_read=True)
        if repaired is not None:
            return repaired
    if reason is not None:
        return AccessFacts(
            kind="other" if reason == "command" else "select",
            scopes=(),
            inserts_values=False,
            windowed_aggregate=False,
            parsed_by="regex",
            fallback_reason=reason,
        )
    read = _read_access(parsed.statements)
    return read or AccessFacts("select", (), False, False, "regex", "parse_error")


def _read_access(
    statements: Iterable[exp.Expr], truncated_read: bool = False
) -> AccessFacts | None:
    """The access facts of parsed statements, or None for a tree the walk does not
    expect (the caller falls back)."""
    try:
        facts = _access_facts(list(statements))
    except Exception:  # noqa: BLE001 -- a tree the walk does not expect falls back too
        return None
    return replace(facts, truncated_read=True) if truncated_read else facts


def _statement_kind(statement: exp.Expr) -> StatementKind:
    if isinstance(statement, exp.Query):
        return "select"
    for node_type, kind in (
        (exp.Insert, "insert"),
        (exp.Update, "update"),
        (exp.Delete, "delete"),
        (exp.Merge, "merge"),
    ):
        if isinstance(statement, node_type):
            return kind  # type: ignore[return-value]
    return "other"


def _access_facts(statements: list[exp.Expr]) -> AccessFacts:
    kind = _statement_kind(statements[0])
    scopes: list[ScopeFacts] = []
    inserts_values = False
    for statement in statements:
        if isinstance(statement, exp.Insert):
            body = statement.expression
            inserts_values = inserts_values or body is None or isinstance(body, exp.Values)
        roots: list[exp.Expr] = (
            [statement] if isinstance(statement, (exp.Update, exp.Delete)) else []
        )
        roots.extend(statement.find_all(exp.Select))
        index_of = {id(root): i + len(scopes) for i, root in enumerate(roots)}
        cte_bodies = _cte_bodies(statement, index_of)
        scopes.extend(_scope_facts(root, index_of, cte_bodies) for root in roots)
    return AccessFacts(
        kind=kind,
        scopes=tuple(scopes),
        inserts_values=inserts_values,
        windowed_aggregate=_has_windowed_aggregate(node for s in statements for node in s.walk()),
        parsed_by="sqlglot",
        fallback_reason=None,
        window_function=_has_window(node for s in statements for node in s.walk()),
        assigned_columns=tuple(
            dict.fromkeys(
                column.name.lower()
                for statement in statements
                if isinstance(statement, exp.Update)
                for assignment in statement.expressions
                if (column := _set_column(assignment)) is not None
            )
        ),
    )


def _cte_bodies(statement: exp.Expr, index_of: dict[int, int]) -> dict[str, int]:
    """CTE name (lower-case) -> scope index of its body, when the body is one SELECT."""
    bodies: dict[str, int] = {}
    for cte in statement.find_all(exp.CTE):
        body = cte.this
        if isinstance(body, exp.Select) and id(body) in index_of:
            bodies[cte.alias_or_name.lower()] = index_of[id(body)]
    return bodies


def _text_search_kind(node: exp.Expr) -> TextSearchKind | None:
    """Which text search ``node`` is, or None (#893: the one definition behind
    :class:`SqlFacts`' text-search facts and :class:`ScopeFacts`' ``text_search``
    and ``like_parameter``).

    ``leading_wildcard`` and ``parameter``: a ``LIKE``/``ILIKE`` (:func:`_like_kind`;
    a prefix or any other pattern is no text search). ``fulltext``: ``MATCH ...
    AGAINST``, ``@@``, ``CONTAINS``, a full-text function or a ``tsvector`` column type. ``pattern``: a
    regular-expression or ``SIMILAR TO`` match. ``similarity``: a trigram
    similarity function.
    """
    if isinstance(node, _LIKE_NODES):
        kind = _like_kind(node)
        return kind if kind in ("leading_wildcard", "parameter") else None  # type: ignore[return-value]
    if isinstance(node, _FULLTEXT_NODES):
        return "fulltext"
    if isinstance(node, exp.DataType) and str(node.args.get("kind") or "").lower() == "tsvector":
        return "fulltext"  # ADD COLUMN search_vec tsvector (#483): sqlglot has no such type
    if isinstance(node, _PATTERN_MATCH_NODES):
        return "pattern"
    if isinstance(node, exp.Func):
        name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
        if name in _FULLTEXT_FUNCTIONS:
            return "fulltext"
        if name in _SIMILARITY_FUNCTIONS:
            return "similarity"
    return None


# The characters a regular expression gives a meaning to; any other is a literal.
_REGEX_META = frozenset(".[]()*+?{}|\\$^")
# A quantifier that can match its atom zero times, so the atom is no fixed prefix.
_OPTIONAL_QUANTIFIERS = ("*", "?", "{0")


def _has_top_level_alternation(pattern: str) -> bool:
    """A ``|`` outside every group and bracket: the pattern is several alternatives, so
    the ``^`` anchors only the first one."""
    depth, in_class, i = 0, False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == "|" and depth == 0:
            return True
        i += 1
    return False


def _literal_prefix_length(pattern: str) -> int:
    """How many literal characters a ``^``-anchored pattern always starts with: each one
    not a metacharacter (or an escaped non-word character) and not made optional by the
    quantifier after it (``^a*``, ``^a?b`` have none). 0 when it is not anchored."""
    if not pattern.startswith("^") or _has_top_level_alternation(pattern):
        return 0
    count, i = 0, 1
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern) and not pattern[i + 1].isalnum():
            step = 2
        elif ch not in _REGEX_META:
            step = 1
        else:
            break
        if pattern.startswith(_OPTIONAL_QUANTIFIERS, i + step):
            break
        count += 1
        i += step
    return count


def _is_negated(node: exp.Expr) -> bool:
    """Any ``NOT`` above the match in its predicate (PostgreSQL's ``!~`` reads as
    ``NOT (a ~ b)``; ``NOT (s = 'x' AND a ~ '^b')`` and ``... IS NOT TRUE`` too), or an
    ``IS`` test that is not ``IS TRUE`` (``IS FALSE``, ``IS NULL``): no index serves the
    rows the prefix does not match. The walk stops at the clause that holds the predicate."""
    parent = node.parent
    while parent is not None and not isinstance(parent, _PREDICATE_CLAUSES):
        if isinstance(parent, exp.Not):
            return True
        if isinstance(parent, exp.Is):
            truth = parent.expression
            if not (isinstance(truth, exp.Boolean) and truth.this is True):
                return True
        parent = parent.parent
    return False


# The clauses a predicate tree ends at.
_PREDICATE_CLAUSES: tuple[type[exp.Expr], ...] = (
    exp.Where,
    exp.Having,
    exp.Join,
    exp.Select,
    exp.Qualify,
)


def _all_prefix_regexes(nodes: list[exp.Expr], dialect: str) -> bool:
    """Every pattern match in ``nodes`` is a case-sensitive, non-negated match of a string
    literal that always starts with a literal prefix (``SqlFacts.prefix_regex``): ``^``,
    at least one literal character no quantifier makes optional, and no top-level
    alternation. False when there is none."""
    matches = [n for n in nodes if isinstance(n, _PATTERN_MATCH_NODES)]
    if not matches or dialect != "postgres":
        return False
    for node in matches:
        if type(node) is not exp.RegexpLike or _is_negated(node):
            return False
        pattern = node.expression
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            return False
        if _literal_prefix_length(pattern.this) < 1:
            return False
    return True


def _is_text_search(node: exp.Expr) -> bool:
    """A text search other than a parameterised ``LIKE`` (``ScopeFacts.text_search``)."""
    kind = _text_search_kind(node)
    return kind is not None and kind != "parameter"


def _pattern_pieces(pattern: exp.Expr) -> list[exp.Expr]:
    """The parts of a concatenated pattern (``||``, ``+``, ``CONCAT(...)``), in order."""
    while isinstance(pattern, exp.Paren):
        pattern = pattern.this
    if isinstance(pattern, (exp.DPipe, exp.Add)):
        return _pattern_pieces(pattern.this) + _pattern_pieces(pattern.expression)
    if isinstance(pattern, exp.Concat):
        return [p for e in pattern.expressions for p in _pattern_pieces(e)]
    return [pattern]


# A bind parameter. ``NULL`` too: the digest step writes a MySQL ``?`` as ``NULL``
# (:mod:`src.tools.sql.normalize`), and a literal ``LIKE NULL`` matches nothing.
_BIND_NODES: tuple[type[exp.Expr], ...] = (exp.Placeholder, exp.Parameter, exp.Null)
# sqlglot's generic dialect (no source engine known) reads PostgreSQL's ``$1`` as
# a column named ``$1``; no real column has that name.
_POSITIONAL_PARAMETER_NAME_RE = re.compile(r"\$\d+")


def _is_bind(node: exp.Expr) -> bool:
    if isinstance(node, _BIND_NODES):
        return True
    return (
        isinstance(node, exp.Column)
        and not node.table
        and bool(_POSITIONAL_PARAMETER_NAME_RE.fullmatch(node.name))
    )


def _string_value(node: exp.Expr) -> str | None:
    """The text of a string literal (``N'...'`` included), else None."""
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this or "")
    if isinstance(node, exp.National):
        return str(node.this or "")
    return None


def _wildcard_likes(statement: exp.Expr) -> list[WildcardLike]:
    """Each leading-wildcard ``LIKE`` of a statement, with the column it tests and that
    column's candidate tables (:class:`~src.tools.sql.regex_facts.WildcardLike`)."""
    ctes = {c.alias_or_name.lower(): -1 for c in statement.find_all(exp.CTE)}
    levels: dict[int, _ColumnScope | None] = {}
    return [
        _wildcard_like(node, levels, ctes)
        for node in statement.walk()
        if isinstance(node, _LIKE_NODES) and _like_kind(node) == "leading_wildcard"
    ]


def _wildcard_like(
    node: exp.Expr, levels: dict[int, _ColumnScope | None], ctes: dict[str, int]
) -> WildcardLike:
    """One leading-wildcard ``LIKE``: its column, whether it is a word search and the
    column's candidate tables (:class:`~src.tools.sql.regex_facts.WildcardLike`)."""
    column = node.this.name.lower() if isinstance(node.this, exp.Column) else None
    pieces = _pattern_pieces(node.expression)
    values = [_string_value(p) for p in pieces]
    if len(pieces) == 1 and values[0] is not None:
        value = values[0]
        word = len(value) > 2 and value.endswith("%") and bool(WORDS_RE.match(value[1:-1]))
    else:
        word = len(pieces) == 3 and values[0] == "%" and _is_bind(pieces[1]) and values[2] == "%"
    return WildcardLike(
        column=column, word_search=word, use=_tested_column(node.this, levels, ctes)
    )


def _tested_column(
    subject: exp.Expr, levels: dict[int, _ColumnScope | None], ctes: dict[str, int]
) -> ColumnUse | None:
    """The one column a predicate tests: the bare column, or the only column inside a
    function of it (``lower(title)``); an expression over several names none."""
    tested = {id(c): c for c in subject.find_all(exp.Column) if c.name and not _is_bind(c)}
    if len(tested) != 1:
        return None
    (only,) = tested.values()
    return ColumnUse(only.name, _column_scopes(only, levels, ctes))


def _regex_matches(statement: exp.Expr) -> list[RegexMatch]:
    """Each regular-expression or ``SIMILAR TO`` match of a statement, with the column it
    tests and that column's candidate tables (:class:`~src.tools.sql.regex_facts.RegexMatch`)."""
    ctes = {c.alias_or_name.lower(): -1 for c in statement.find_all(exp.CTE)}
    levels: dict[int, _ColumnScope | None] = {}
    out = []
    for node in statement.walk():
        if not isinstance(node, _PATTERN_MATCH_NODES):
            continue
        use = _tested_column(node.this, levels, ctes)
        column_pattern = use is None and _tested_column(node.expression, levels, ctes) is not None
        out.append(RegexMatch(use=use, column_pattern=column_pattern))
    return out


LikeKind = Literal["leading_wildcard", "prefix", "parameter", "other"]


def _like_kind(node: exp.Expr) -> LikeKind:
    """How a ``LIKE``/``ILIKE`` pattern addresses rows.

    ``leading_wildcard``: the pattern's first part is a literal starting with ``%``
    (``wildcard_leading``). ``prefix``: it is anchored at its start, a range on the
    column (``'abc%'``, ``? || '%'``). ``parameter``: the whole pattern is a bind
    parameter, so its value is unknown. ``other``: anything else (``'_bc%'``, a
    column).
    """
    pattern = node.expression
    if _starts_with_wildcard(pattern):
        return "leading_wildcard"
    pieces = _pattern_pieces(pattern)
    first, last = pieces[0], pieces[-1]
    if isinstance(first, exp.Literal) and first.is_string:
        text = first.this or ""
        return "prefix" if text and text[0] not in "%_" else "other"
    if (
        len(pieces) > 1
        and isinstance(last, exp.Literal)
        and last.is_string
        and str(last.this or "").endswith("%")
    ):
        return "prefix"
    if len(pieces) == 1 and _is_bind(first):
        return "parameter"
    return "other"


# Nodes that truncate or extract a date/time (sqlglot's names; a name a sqlglot
# version lacks is skipped), and the functions it leaves unnamed.
_TIME_BUCKET_NODES: tuple[type[exp.Expr], ...] = tuple(
    node
    for name in (
        "DateTrunc",
        "TimestampTrunc",
        "DatetimeTrunc",
        "TimeTrunc",
        "Extract",
        "TimeToStr",
        "TsOrDsToDate",
        "Date",
        "Year",
        "Quarter",
        "Month",
        "Week",
        "WeekOfYear",
        "Day",
        "DayOfWeek",
        "DayOfMonth",
        "DayOfYear",
        "Hour",
        "Minute",
    )
    if isinstance(node := getattr(exp, name, None), type)
)
_TIME_BUCKET_FUNCTIONS = frozenset(
    {"strftime", "date_part", "datepart", "trunc", "to_char", "date_format", "date_bin"}
)


def _is_time_bucket_node(node: exp.Expr) -> bool:
    if isinstance(node, _TIME_BUCKET_NODES):
        return True
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return bool(node.to.is_type(exp.DataType.Type.DATE))
    if isinstance(node, exp.Convert):  # T-SQL CONVERT(date, x)
        return isinstance(node.this, exp.DataType) and bool(
            node.this.is_type(exp.DataType.Type.DATE)
        )
    if isinstance(node, exp.Anonymous):
        return node.name.lower() in _TIME_BUCKET_FUNCTIONS
    return False


def _has_time_bucket(root: exp.Expr) -> bool:
    """Whether ``root``'s ``GROUP BY`` groups on a date/time truncation or extraction."""
    group = root.args.get("group")
    if not isinstance(group, exp.Group):
        return False
    projections = list(root.args.get("expressions") or [])
    by_alias = {p.alias.lower(): p for p in projections if isinstance(p, exp.Alias)}
    for item in group.expressions:
        target: exp.Expr = item
        if isinstance(item, exp.Literal) and not item.is_string and str(item.this).isdigit():
            position = int(item.this) - 1
            if 0 <= position < len(projections):
                target = projections[position]
        elif isinstance(item, exp.Column) and not item.table and item.name.lower() in by_alias:
            target = by_alias[item.name.lower()]
        if any(_is_time_bucket_node(n) for n in target.walk()):
            return True
    return False


def _own_nodes(root: exp.Expr) -> list[exp.Expr]:
    """``root`` and its descendants, not looking inside a nested query."""
    found: list[exp.Expr] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node is not root and isinstance(node, (exp.Select, exp.Subquery)):
            continue
        found.append(node)
        stack.extend(node.iter_expressions())
    return found


def _scope_sources(
    root: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> tuple[list[ScopeSource], list[exp.Join]]:
    """The tables ``root`` reads directly and every JOIN node that adds one."""
    nodes: list[exp.Expr] = []
    joins: list[exp.Join] = []
    if isinstance(root, (exp.Update, exp.Delete)) and root.this is not None:
        target = root.this
        target_ref = _ref_from_node(target) if isinstance(target, exp.Table) else None
        if not (
            isinstance(target, exp.Table)
            and target_ref is not None
            and _is_alias_reference(target, target_ref, _table_aliases(root))
        ):
            nodes.append(target)
    from_ = root.args.get("from_")
    if isinstance(from_, exp.From):
        nodes.append(from_.this)
        nodes.extend(from_.expressions or [])
    using = root.args.get("using")
    if isinstance(using, list):
        nodes.extend(using)
    for join in root.args.get("joins") or []:
        joins.append(join)
    sources: list[ScopeSource] = []
    pending = [(n, False) for n in nodes] + [(j.this, _optional_side(j)) for j in joins]
    seen: set[int] = set()
    while pending:
        node, optional = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        for join in node.args.get("joins") or []:
            joins.append(join)
            pending.append((join.this, _optional_side(join)))
        source = _source(node, index_of, cte_bodies)
        if source is not None:
            sources.append(replace(source, optional=optional) if optional else source)
    return sources, joins


def _optional_side(join: exp.Join) -> bool:
    return str(join.args.get("side") or "").upper() in ("LEFT", "FULL")


def _source(
    node: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> ScopeSource | None:
    alias = node.alias_or_name.lower() if hasattr(node, "alias_or_name") else ""
    if isinstance(node, exp.Table):
        ref = _ref_from_node(node)
        if ref is None:
            if isinstance(node.this, exp.Identifier) and node.name.lower() in _PSEUDO_TABLES:
                return None
            return ScopeSource(alias=alias or "", table=None, function=True)
        if not ref.qualifiers and ref.name.lower() in cte_bodies and _is_cte_reference(node, ref):
            return ScopeSource(alias=alias, table=None, derived=cte_bodies[ref.name.lower()])
        return ScopeSource(alias=alias, table=ref)
    if isinstance(node, exp.Subquery):
        body = node.this
        derived = index_of.get(id(body)) if isinstance(body, exp.Select) else None
        return ScopeSource(alias=alias, table=None, derived=derived)
    return ScopeSource(alias=alias, table=None, function=True)


def _conjuncts(condition: exp.Expr | None) -> list[exp.Expr]:
    if condition is None:
        return []
    if isinstance(condition, exp.Where):
        condition = condition.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return _conjuncts(condition.this) + _conjuncts(condition.expression)
    return [condition]


def _disjuncts(condition: exp.Expr) -> list[exp.Expr]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.Or):
        return _disjuncts(condition.this) + _disjuncts(condition.expression)
    return [condition]


def _or_branches(terms: list[exp.Expr], aliases: set[str]) -> tuple[tuple[ColumnFact, ...], ...]:
    """The columns each branch of the WHERE's only ``OR`` group pins, when every
    branch is made only of ``col = <value>`` terms (:class:`ScopeFacts`)."""
    return _or_reading(terms, aliases)[0]


def _or_correlated(terms: list[exp.Expr], aliases: set[str]) -> tuple[ColumnFact, ...]:
    """The ``or_branches`` columns whose value is another scope's column (#1231)."""
    return _or_reading(terms, aliases)[1]


def _or_reading(
    terms: list[exp.Expr], aliases: set[str]
) -> tuple[tuple[tuple[ColumnFact, ...], ...], tuple[ColumnFact, ...]]:
    groups = [t for t in terms if len(_disjuncts(t)) > 1]
    if len(groups) != 1:
        return (), ()
    branches: list[tuple[ColumnFact, ...]] = []
    correlated: list[ColumnFact] = []
    for branch in _disjuncts(groups[0]):
        reader = _ScopeReader(aliases)
        for term in _conjuncts(branch):
            stripped = term
            while isinstance(stripped, exp.Paren):
                stripped = stripped.this
            if not isinstance(stripped, (exp.EQ, exp.NullSafeEQ)):
                return (), ()
            reader.term(stripped)
        if reader.other or reader.ranges or reader.joins or not reader.equalities:
            return (), ()
        branches.append(tuple(reader.equalities))
        correlated.extend(reader.correlated)
    return tuple(branches), tuple(correlated)


def _own_columns(node: exp.Expr) -> list[exp.Column]:
    """Columns in ``node``, not looking inside a nested query."""
    found: list[exp.Column] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Column):
            found.append(current)
            continue
        if isinstance(current, (exp.Subquery, exp.Select)) and current is not node:
            continue
        stack.extend(current.iter_expressions())
    return found


def _column_fact(column: exp.Column) -> ColumnFact | None:
    if isinstance(column.this, exp.Star) or not column.name:
        return None
    qualifier = column.table.lower() if column.table else None
    return ColumnFact(qualifier=qualifier, name=column.name.lower())


class _ScopeReader:
    """Sorts one scope's predicate terms into the :class:`ScopeFacts` fields."""

    def __init__(self, aliases: set[str]) -> None:
        self.aliases = aliases
        self.equalities: list[ColumnFact] = []
        self.ranges: list[ColumnFact] = []
        self.joins: list[tuple[ColumnFact, ColumnFact]] = []
        self.null_tests: list[ColumnFact] = []
        self.not_null_tests: list[ColumnFact] = []
        self.correlated: list[ColumnFact] = []
        self.other = 0

    def _in_scope(self, column: exp.Column) -> bool:
        qualifier = column.table.lower() if column.table else None
        return qualifier is None or qualifier in self.aliases

    def _scope_column(self, node: exp.Expr) -> ColumnFact | None:
        """``node`` as one of this scope's columns, or None."""
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Column) and self._in_scope(node):
            return _column_fact(node)
        return None

    def _is_value(self, node: exp.Expr) -> bool:
        """Holds none of this scope's columns (a literal, placeholder, outer column, ...)."""
        return not any(self._in_scope(c) for c in _own_columns(node))

    def term(self, node: exp.Expr) -> None:
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Escape):
            node = node.this
        if isinstance(node, (exp.EQ, exp.NullSafeEQ)):
            left, right = self._scope_column(node.this), self._scope_column(node.expression)
            if left and right:
                self.joins.append((left, right))
            elif left and self._is_value(node.expression):
                self.equalities.append(left)
                if _own_columns(node.expression):
                    self.correlated.append(left)
            elif right and self._is_value(node.this):
                self.equalities.append(right)
                if _own_columns(node.this):
                    self.correlated.append(right)
            else:
                self.other += 1
            return
        if isinstance(node, exp.In):
            column = self._scope_column(node.this)
            values = list(node.expressions or [])
            if column and all(self._is_value(v) for v in values):
                self.equalities.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, exp.Between):
            column = self._scope_column(node.this)
            if column:
                self.ranges.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, _RANGE_NODES):
            column = self._scope_column(node.this) or self._scope_column(node.expression)
            if column:
                self.ranges.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, (exp.Like, exp.ILike)) and _like_kind(node) == "prefix":
            column = self._scope_column(node.this)
            if column:
                self.ranges.append(column)
                return
        # ``col IS NULL`` / ``col IS NOT NULL``: still another predicate term, and also
        # recorded as a null test on the column (#1231).
        negated = isinstance(node, exp.Not)
        test = node.this if negated else node
        while isinstance(test, exp.Paren):
            test = test.this
        if isinstance(test, exp.Is) and isinstance(test.expression, exp.Null):
            column = self._scope_column(test.this)
            if column:
                negated = negated != bool(test.args.get("negate"))
                (self.not_null_tests if negated else self.null_tests).append(column)
        self.other += 1

    def using(self, join: exp.Join, left_aliases: list[str], right_alias: str) -> None:
        for identifier in join.args.get("using") or []:
            name = identifier.name.lower()
            for alias in left_aliases:
                self.joins.append((ColumnFact(alias, name), ColumnFact(right_alias, name)))


def _row_limit(root: exp.Expr) -> str | None:
    limit = root.args.get("limit")
    if limit is None:
        return None
    options = limit.args.get("limit_options")
    if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
        return "?"
    count = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.expression
    if count is None:
        return "1" if isinstance(limit, exp.Fetch) else None
    if isinstance(count, exp.Literal) and not count.is_string and str(count.this).isdigit():
        return str(int(count.this))
    return "?"


def _skips_locked(root: exp.Expr, sources_nodes: list[exp.Expr]) -> bool:
    for lock in root.args.get("locks") or []:
        if lock.args.get("wait") is False:
            return True
    for node in sources_nodes:
        for hint in node.args.get("hints") or []:
            if isinstance(hint, exp.WithTableHint) and any(
                h.name.upper() in _ROW_SKIPPING_HINTS for h in hint.expressions
            ):
                return True
    return False


def _own_aggregates(root: exp.Expr) -> list[exp.Expr]:
    """The aggregate functions in ``root``'s own projection, HAVING or ORDER BY."""
    parts: list[exp.Expr] = list(root.args.get("expressions") or [])
    for key in ("having", "order"):
        if root.args.get(key) is not None:
            parts.append(root.args[key])
    found: list[exp.Expr] = []
    stack = parts
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Subquery, exp.Select, exp.Window)):
            continue
        # ``COUNT(*) FILTER (WHERE ...)``: the filtered aggregate is one aggregate.
        if isinstance(node, exp.Filter) or _is_aggregation(node):
            found.append(node)
            continue
        stack.extend(node.iter_expressions())
    return found


def _counts_only(aggregates: list[exp.Expr]) -> bool:
    """Every aggregate is a plain count: ``COUNT(*)`` or ``COUNT(1)`` (rows) or
    ``COUNT(col)`` (the rows whose ``col`` is not null), never ``COUNT(DISTINCT ...)`` or
    a filtered ``COUNT(*) FILTER (WHERE ...)`` (``ScopeFacts.count_only``, #377)."""
    return bool(aggregates) and all(
        isinstance(node, exp.Count) and not isinstance(node.this, exp.Distinct)
        for node in aggregates
    )


def _projection(root: exp.Expr) -> tuple[tuple[ColumnFact, ...], bool]:
    """A SELECT scope's returned columns and whether it returns ``*`` (``ScopeFacts``)."""
    if not isinstance(root, exp.Select):
        return (), False
    columns: list[ColumnFact] = []
    every = False
    for item in root.expressions:
        target = item.this if isinstance(item, exp.Alias) else item
        if isinstance(target, exp.Star) or (
            isinstance(target, exp.Column) and isinstance(target.this, exp.Star)
        ):
            every = True
            continue
        columns += [f for c in _own_columns(item) if (f := _column_fact(c)) is not None]
    return tuple(dict.fromkeys(columns)), every


def _shape(node: exp.Expr) -> str:
    """An expression's shape (``ScopeFacts.group_by_items``): no literal, no qualifier."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Star) or (
        isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
    ):
        return "*"
    if _is_bind(node) or isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        return "?"
    if isinstance(node, exp.Column):
        return str(node.name).lower()
    if isinstance(node, exp.Var):  # a date part or keyword (``'day'`` in date_trunc)
        return str(node.name).lower()
    if isinstance(node, (exp.Subquery, exp.Select)):
        return "subquery"
    name = node.name.lower() if isinstance(node, exp.Anonymous) else node.key
    parts = [_shape(child) for child in node.iter_expressions()]
    return f"{name}({','.join(parts)})"


def _group_by_items(root: exp.Expr) -> tuple[str, ...]:
    group = root.args.get("group")
    if not isinstance(group, exp.Group):
        return ()
    projections = list(root.args.get("expressions") or [])
    by_alias = {p.alias.lower(): p.this for p in projections if isinstance(p, exp.Alias)}
    items = []
    for item in group.expressions:
        target: exp.Expr = item
        if isinstance(item, exp.Literal) and not item.is_string and str(item.this).isdigit():
            position = int(item.this) - 1
            if 0 <= position < len(projections):
                target = projections[position]
                target = target.this if isinstance(target, exp.Alias) else target
        elif isinstance(item, exp.Column) and not item.table and item.name.lower() in by_alias:
            target = by_alias[item.name.lower()]
        items.append(_shape(target))
    return tuple(items)


def _aggregate_kinds(root: exp.Expr) -> tuple[str, ...]:
    """The aggregate functions of ``root``'s own projection, HAVING and ORDER BY."""
    stack: list[exp.Expr] = list(root.args.get("expressions") or [])
    for key in ("having", "order"):
        if root.args.get(key) is not None:
            stack.append(root.args[key])
    kinds: set[str] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Subquery, exp.Select, exp.Window)):
            continue
        if _is_aggregate_function(node):
            star = isinstance(node.this, exp.Star) if isinstance(node, exp.Count) else False
            name = node.name.lower() if isinstance(node, exp.Anonymous) else node.key
            kinds.add(f"{name}(*)" if star else name)
        stack.extend(node.iter_expressions())
    return tuple(sorted(kinds))


def _scope_facts(
    root: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> ScopeFacts:
    own = _own_nodes(root)
    sources, joins = _scope_sources(root, index_of, cte_bodies)
    aliases = {s.alias for s in sources if s.alias}
    aliases |= {s.table.name.lower() for s in sources if s.table is not None}
    reader = _ScopeReader(aliases)
    where = root.args.get("where")
    where_terms = _conjuncts(where)
    for term in where_terms:
        reader.term(term)
    seen_aliases = [s.alias for s in sources[: len(sources) - len(joins)]]
    for join in joins:
        for term in _conjuncts(join.args.get("on")):
            reader.term(term)
        right = join.this.alias_or_name.lower() if hasattr(join.this, "alias_or_name") else ""
        if join.args.get("using"):
            reader.using(join, seen_aliases, right)
        seen_aliases.append(right)
    order = root.args.get("order")
    order_by = tuple(
        _column_fact(o.this) if isinstance(o.this, exp.Column) else None
        for o in (order.expressions if isinstance(order, exp.Order) else [])
    )
    returned, returns_all = _projection(root)
    source_nodes = [j.this for j in joins]
    if isinstance(root.args.get("from_"), exp.From):
        source_nodes.append(root.args["from_"].this)
    aggregates = _own_aggregates(root)
    return ScopeFacts(
        sources=tuple(sources),
        equalities=tuple(reader.equalities),
        ranges=tuple(reader.ranges),
        joins=tuple(reader.joins),
        other_filters=reader.other,
        order_by=order_by,
        limit=_row_limit(root),
        has_where=where is not None,
        group_by=root.args.get("group") is not None,
        aggregate=bool(aggregates),
        count_only=_counts_only(aggregates),
        skip_locked=_skips_locked(root, source_nodes),
        text_search=any(_is_text_search(n) for n in own),
        like_parameter=any(_text_search_kind(n) == "parameter" for n in own),
        time_bucket=_has_time_bucket(root),
        text_search_kinds=frozenset(k for n in own if (k := _text_search_kind(n)) is not None),
        or_branches=_or_branches(where_terms, aliases),
        null_tests=tuple(reader.null_tests),
        not_null_tests=tuple(reader.not_null_tests),
        correlated=tuple(reader.correlated) + _or_correlated(where_terms, aliases),
        returned=returned,
        returns_all=returns_all,
        group_by_items=_group_by_items(root),
        having=tuple(
            _shape(t)
            for t in (
                _conjuncts(root.args["having"].this)
                if isinstance(root.args.get("having"), exp.Having)
                else []
            )
        ),
        aggregate_kinds=_aggregate_kinds(root),
    )
