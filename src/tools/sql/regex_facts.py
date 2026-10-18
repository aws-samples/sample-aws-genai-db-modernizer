"""The regex query facts: the facade's fallback when sqlglot can't read a statement.

These are the scans the collectors ran on raw query text before #796, moved
here unchanged so the facade (:func:`src.tools.sql.facade.sql_facts`) falls
back to exactly the same answer when it can't parse a statement, the way
:mod:`src.tools.sql.regex_tables` backs its table references. They read
comments, string literals and column names as SQL (a column named ``count``
is an aggregate, ``user_selectable`` is a second ``SELECT``, a ``-- join``
comment is a join), which is why the facade reads the syntax tree first.

- ``has_joins``: the text contains `` join `` (surrounded by spaces);
- ``join_count``: the number of ``join`` words;
- ``has_aggregation``: a ``count``/``sum``/``avg``/``min``/``max`` word, a
  ``group_concat``/``string_agg``/``array_agg`` word (#893) or ``GROUP BY`` anywhere;
- ``has_subquery``: more than one ``select`` substring;
- ``wildcard_leading``: ``LIKE`` followed by a quote and ``%`` (the
  capability gate's and the DynamoDB exclusion's own detector, which reads
  ``ILIKE`` and ``NOT LIKE`` too), a SQL Server ``N'%...'`` literal, or a
  ``CONCAT('%', ...)`` whose first part is one (#493); ``wildcard_likes`` is each
  of them, with its column and whether it is a word search.

The facts #893 added to the facade fall back to the text detector each
replaced, one per fact; ``group_by`` and ``aggregate_function_count`` are
read so that ``has_aggregation`` is still exactly ``group_by`` or a positive
count:

- ``group_by``: ``GROUP BY``;
- ``aggregate_function_count``: how many ``count``/``sum``/``avg``/``min``/``max``/
  ``group_concat``/``string_agg``/``array_agg`` words;
- ``like_parameter``: ``LIKE``/``ILIKE`` followed by a bind parameter (``?``,
  ``$1``, ``:name``, ``@name``) that is not concatenated (``@p + '%'`` is a prefix);
- ``fulltext_search``: ``MATCH (...) AGAINST (``, a PostgreSQL ``tsvector``/
  ``tsquery`` function, ``tsvector``, ``ts_rank(``, ``@@``, or ``CONTAINS(``/
  ``CONTAINSTABLE(``/``FREETEXT(``/``FREETEXTTABLE(``;
- ``pattern_match``: ``REGEXP``/``RLIKE`` as a word, ``REGEXP_LIKE(``, ``~`` or
  ``~*`` followed by a space, ``SIMILAR TO``;
- ``similarity_match``: ``similarity(``, ``word_similarity(`` or
  ``strict_word_similarity(``;
- ``window_function``: ``)`` followed by ``OVER (``;
- ``ranking_window_function``: ``ROW_NUMBER()``/``RANK()``/``DENSE_RANK()``
  followed by ``OVER``;
- ``offset_window_function``: a ``LAG``/``LEAD``/``NTILE`` call followed by
  ``OVER``;
- ``windowed_aggregate``: a ``SUM``/``AVG``/``MIN``/``MAX``/``COUNT`` call followed
  by ``OVER``;
- ``grouping_sets``: ``GROUPING SETS (``, ``ROLLUP (`` or ``CUBE (`` after ``GROUP BY``, or
  ``WITH ROLLUP`` / ``WITH CUBE``;
- ``recursive_cte``: ``WITH RECURSIVE``, or a CTE whose body has a ``UNION`` and
  names the CTE again, read on :func:`scan_sql`'s output (the Aurora engine
  choice's detector, #381);
- ``order_by``: ``ORDER BY``, not one inside ``OVER (...)``;
- ``row_limit``: ``LIMIT``, ``FETCH FIRST``/``NEXT``, or ``TOP`` followed by a
  number, ``(`` or a variable;
- ``top_n``: ``ORDER BY ... LIMIT``/``FETCH FIRST|NEXT``, or ``TOP (n) ... ORDER
  BY`` (the top-N regex triage and the cache overlay read before, #296/#663);
- ``columns``: each :data:`TIMESTAMP_COLUMN_NAMES` name as a word, with no
  tables (``timestamp_column`` is whether there is one): the fallback can't tell
  a column's table, so the timestamp-column fact reads these by name (#935);
- ``json_access``: a ``json_*``/``jsonb_*`` call, ``->``/``->>``, ``#>``,
  ``::json``/``::jsonb``, ``@> '``, ``OPENJSON(``, ``FOR JSON``, ``IS JSON``;
- ``multi_table_write``: outside parentheses, ``UPDATE`` with a ``JOIN`` or a comma
  before the first ``SET`` or a ``FROM`` after it that names another table than the
  target, or ``DELETE`` followed by ``USING`` or ``JOIN``;
- ``write_targets``: the table named after ``INSERT [IGNORE] [INTO]``, ``REPLACE
  [INTO]``, ``UPDATE`` (not ``FOR UPDATE`` or ``KEY UPDATE``), ``DELETE [FROM]``,
  ``MERGE [INTO]`` or ``TRUNCATE [TABLE]`` (past a ``TOP (n)``), on :func:`scan_sql`'s
  output, as a bare lower-cased name, and each table a multi-table ``UPDATE``'s ``SET``
  columns name; a MySQL ``DELETE a FROM t a`` or a SQL Server ``UPDATE a SET ...
  FROM t a`` names the aliased table;
- ``range_predicate`` (the fallback of the access facts' ranges): ``BETWEEN``,
  ``<``, ``<=``, ``>``, ``>=`` (not ``<>``, ``->``, ``@>`` or ``<@``).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from src.tools.sql.refs import ColumnUse

_JOIN_WORD_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_AGGREGATION_WORD_RE = re.compile(
    r"\b(count|sum|avg|min|max|group_concat|string_agg|array_agg|group\s+by)\b", re.IGNORECASE
)
# ``GROUP BY``, ``ORDER BY``, a pattern-match predicate, a literal-prefix ``LIKE`` and
# the ``SELECT`` word: the regex readings ``query_shape``'s shape check runs on
# comment- and literal-stripped text, defined here once (#893 review).
GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
PATTERN_PREDICATE_RE = re.compile(r"\b(?:I?LIKE|SIMILAR\s+TO|REGEXP|RLIKE)\b", re.IGNORECASE)
LITERAL_PREFIX_LIKE_RE = re.compile(r"\bI?LIKE\s+'[^%_']", re.IGNORECASE)
SELECT_WORD_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
# ``IN (`` (the key predicate's IN list, and the IN list whose values the transaction
# shape counts), a ``[NOT] EXISTS (`` condition (the ``exists_or_in_subquery`` fact's,
# and the one the transaction shape blanks) and the ``FROM`` word (the multi-table
# write's, and the source of an INSERT ... SELECT), each defined here once (#930).
IN_LIST_RE = re.compile(r"\bIN\s*\(", re.IGNORECASE)
EXISTS_CONDITION_RE = re.compile(r"\b(?:NOT\s+)?EXISTS\s*\(", re.IGNORECASE)
FROM_WORD_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
# A LIKE/ILIKE whose pattern starts with ``%``: a quoted literal (``N'%...'``
# included), or a ``CONCAT(...)`` whose first part is one (#493).
_LEADING_WILDCARD_RE = re.compile(
    r"\b(?:NOT\s+)?I?LIKE\s+(?=N?['\"]%|CONCAT\s*\(\s*N?['\"]%)", re.IGNORECASE
)
# The whole pattern after LIKE: a CONCAT call, or literals and binds joined by + or ||.
_SQL_STRING = r"N?(?:'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")"
_LIKE_PATTERN_RE = re.compile(
    r"(?P<pattern>CONCAT\s*\([^()]*\)|"
    + _SQL_STRING
    + r"(?:\s*(?:\+|\|\|)\s*(?:[@:$?]?\w+|\?|"
    + _SQL_STRING
    + r"))*)",
    re.IGNORECASE,
)
_IDENT = r'(?:[A-Za-z_][\w$]*|"[^"]+"|\[[^\]]+\]|`[^`]+`)'
_COLUMN_BEFORE_RE = re.compile(rf"({_IDENT}(?:\s*\.\s*{_IDENT})*)\s*$")
_CONCAT_ARGS_RE = re.compile(r"^CONCAT\s*\((?P<args>[^()]*)\)$", re.IGNORECASE)
_CONCAT_SPLIT_RE = re.compile(r"\s*(?:\+|\|\|)\s*")
# A word search's term: letters and digits, single spaces between words.
WORDS_RE = re.compile(r"^[^\W_]+(?: [^\W_]+)*$")
_NOT_A_COLUMN = frozenset({"and", "or", "not", "where", "on", "when", "then", "else", "case"})
_AGGREGATE_WORD_RE = re.compile(
    r"\b(?:count|sum|avg|min|max|group_concat|string_agg|array_agg)\b", re.IGNORECASE
)
# Not when the parameter is concatenated (``@Prefix + '%'``, ``$1 || '%'``): a prefix.
_LIKE_PARAMETER_RE = re.compile(
    r"\bI?LIKE\s+(?:\?|\$\d+\b|:\w+\b|@\w+\b)(?!\s*(?:\+|\|\|))", re.IGNORECASE
)
# ``MATCH (...) AGAINST (`` is :func:`has_match_against`, a linear scan (#902).
_FULLTEXT_SEARCH_RE = re.compile(
    r"""
    \b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(
    |
    \btsvector\b
    |
    \bts_rank(?:_cd)?\s*\(
    |
    @@
    |
    \b(?:contains|containstable|freetext|freetexttable)\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PATTERN_MATCH_RE = re.compile(
    r"\b(?:regexp|rlike)\b|\bregexp_like\s*\(|~\*?\s|\bsimilar\s+to\b", re.IGNORECASE
)
_SIMILARITY_MATCH_RE = re.compile(r"\b(?:strict_word_|word_)?similarity\s*\(", re.IGNORECASE)
# A grouping list (GROUPING SETS, ROLLUP, CUBE) inside a GROUP BY, or MySQL's and SQL
# Server's WITH ROLLUP / WITH CUBE; not a PostgreSQL cube(...) function call elsewhere.
_GROUPING_SETS_RE = re.compile(
    r"\bGROUP\s+BY\b[^;]*?\b(?:GROUPING\s+SETS|ROLLUP|CUBE)\s*\(|\bWITH\s+(?:ROLLUP|CUBE)\b",
    re.IGNORECASE,
)
_WINDOW_FUNCTION_RE = re.compile(r"\)\s*OVER\s*\(", re.IGNORECASE)
_ROW_LIMIT_RE = re.compile(
    r"\blimit\b|\bfetch\s+(?:first|next)\b|\btop\s*(?:\(|\d|@)", re.IGNORECASE
)
# A top-N read: ``ORDER BY ... LIMIT n``, or its T-SQL and ANSI spellings (#663),
# ``SELECT TOP (n) ... ORDER BY`` and ``ORDER BY ... [OFFSET ...] FETCH {FIRST|NEXT}``.
# TOP takes a number, a ``(expression)`` or a variable, so a column named ``top``
# is not one; TOP with no ORDER BY returns arbitrary rows and is not a top-N.
# Read as two ordered word pairs rather than one ``X.*Y`` regex, which retried its
# gap from every ``ORDER BY`` (quadratic on repeated ones with no row limit, #1009):
# the first ``ORDER BY`` is followed by a row limit anywhere after it, or the first
# ``TOP`` with a count is followed by an ``ORDER BY`` anywhere after it.
_ORDER_BY_WORDS_RE = re.compile(r"order\s+by\b", re.IGNORECASE)
_LIMIT_WORD_RE = re.compile(r"\b(?:limit|fetch\s+(?:first|next))\b", re.IGNORECASE)
_TOP_COUNT_RE = re.compile(r"\btop\s*(?:\(|\d|@)", re.IGNORECASE)
_ORDER_BY_WORD_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
# The names of a timestamp column: every name the DynamoDB, OpenSearch,
# ElastiCache and triage detectors read before #893, as whole names. Since #935 a
# column's collected type decides; these names are read only for a collection with
# no column types, or a column whose table can't be told
# (``query_facts.QueryFacts.timestamp_column``).
TIMESTAMP_COLUMN_NAMES = (
    r"created_at|updated_at|timestamp|date|time|expires?_at|ttl|last_\w+_at|event_time|"
    r"log_time|ingested_at|open_date|close_date|created_date|modified_date|event_date"
)
TIMESTAMP_COLUMN_NAME_RE = re.compile(rf"(?:{TIMESTAMP_COLUMN_NAMES})", re.IGNORECASE)
_TIMESTAMP_COLUMN_RE = re.compile(rf"\b(?:{TIMESTAMP_COLUMN_NAMES})\b", re.IGNORECASE)
# <, >, <=, >= or BETWEEN -- but not <>, ->, ->>, @>, <@, <<, >>, <=> (the range
# fallback, and ``query_shape``'s shape check on comment- and literal-stripped text).
RANGE_PREDICATE_RE = re.compile(r"\bBETWEEN\b|(?<![<>=!@\-])(?:<=|>=|<|>)(?![<>=@])", re.IGNORECASE)
_JSON_ACCESS_RE = re.compile(
    r"\bjsonb?_\w+\s*\(|->>?|#>|::jsonb?\b|@>\s*'|\bopenjson\s*\(|\bfor\s+json\b|\bis\s+json\b",
    re.IGNORECASE,
)
_RANKING_WINDOW_FUNCTION_RE = re.compile(
    r"\b(?:ROW_NUMBER|RANK|DENSE_RANK)\s*\(\s*\)\s*OVER\b", re.IGNORECASE
)
# A call's arguments: anything but parentheses, or one level of nested ones.
_ARGUMENTS = r"\((?:[^()]|\([^()]*\))*\)"
_OFFSET_WINDOW_FUNCTION_RE = re.compile(
    rf"\b(?:LAG|LEAD|NTILE)\s*{_ARGUMENTS}\s*OVER\b", re.IGNORECASE
)
_WINDOWED_AGGREGATE_RE = re.compile(
    rf"\b(?:SUM|AVG|MIN|MAX|COUNT)\s*{_ARGUMENTS}\s*OVER\b", re.IGNORECASE
)


def regex_has_joins(query_text: str) -> bool:
    """`` join `` with a space on each side, case-insensitive."""
    return " join " in (query_text or "").lower()


def regex_join_count(query_text: str) -> int:
    """How many times the word ``join`` appears."""
    return len(_JOIN_WORD_RE.findall(query_text or ""))


def regex_has_aggregation(query_text: str) -> bool:
    """An aggregate function name or ``GROUP BY`` anywhere in the text."""
    return bool(_AGGREGATION_WORD_RE.search(query_text or ""))


def regex_has_subquery(query_text: str) -> bool:
    """More than one ``select`` substring, case-insensitive."""
    return (query_text or "").lower().count("select") > 1


@dataclass(frozen=True)
class WildcardLike:
    """One ``LIKE``/``ILIKE`` whose pattern starts with ``%`` (``SqlFacts.wildcard_likes``).

    ``column``: the bare, lower-case name of the column it tests, or None when the
    left side is an expression. ``word_search``: the pattern is ``%term%`` with one
    word-like term, a literal of letters and digits (``'%red shoe%'``) or a bind
    parameter (``'%' + @t + '%'``, ``CONCAT('%', ?, '%')``): an engine's word index
    can serve it after a rewrite. Any other shape (``'%.com'``, ``'%a_b%'``) is a
    substring match (#493).

    ``use``: the column the predicate tests (the bare column, or the only column inside
    a function of it such as ``lower(title)``) with the tables it can belong to, so a
    reader can look up the scanned table's rows and the column's collected type (#942).
    None when the parser could not name one column (an expression over several, a
    constant), and for every regex-fallback fact, which never knows the tables.
    """

    column: str | None
    word_search: bool
    use: ColumnUse | None = None


def _literal_value(part: str) -> str | None:
    part = part.strip()
    if part[:1] in ("N", "n") and part[1:2] in ("'", '"'):
        part = part[1:]
    if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
        return part[1:-1].replace(part[0] * 2, part[0])
    return None


def _regex_word_search(pattern: str) -> bool:
    concat = _CONCAT_ARGS_RE.match(pattern.strip())
    parts = (
        [a.strip() for a in concat.group("args").split(",")]
        if concat
        else _CONCAT_SPLIT_RE.split(pattern.strip())
    )
    values = [_literal_value(p) for p in parts]
    if len(parts) == 1 and values[0] is not None:
        value = values[0]
        return len(value) > 2 and value.endswith("%") and bool(WORDS_RE.match(value[1:-1]))
    return len(parts) == 3 and values[0] == "%" and values[1] is None and values[2] == "%"


def regex_wildcard_likes(query_text: str) -> tuple[WildcardLike, ...]:
    """Each ``LIKE '%...``, ``LIKE N'%...`` or ``LIKE CONCAT('%', ...`` (``ILIKE``,
    ``NOT LIKE`` included), with the column before it and whether it is a word search."""
    text = query_text or ""
    found = []
    for match in _LEADING_WILDCARD_RE.finditer(text):
        pattern = _LIKE_PATTERN_RE.match(text, match.end())
        word = bool(pattern) and _regex_word_search(pattern.group("pattern"))  # type: ignore[union-attr]
        column_match = _COLUMN_BEFORE_RE.search(text[: match.start()])
        column = None
        if column_match:
            name = re.split(r"\s*\.\s*", column_match.group(1))[-1].strip('"[]`').lower()
            column = None if name in _NOT_A_COLUMN else name
        found.append(WildcardLike(column=column, word_search=word))
    return tuple(found)


def regex_wildcard_leading(query_text: str) -> bool:
    """A leading-wildcard ``LIKE`` (:func:`regex_wildcard_likes` finds at least one)."""
    return bool(regex_wildcard_likes(query_text))


def regex_group_by(query_text: str) -> bool:
    """``GROUP BY``, case-insensitive."""
    return bool(GROUP_BY_RE.search(query_text or ""))


def regex_aggregate_function_count(query_text: str) -> int:
    """How many aggregate function words (``count``, ..., ``group_concat``) the text has."""
    return len(_AGGREGATE_WORD_RE.findall(query_text or ""))


def regex_like_parameter(query_text: str) -> bool:
    """``LIKE``/``ILIKE`` followed by a bind parameter."""
    return bool(_LIKE_PARAMETER_RE.search(query_text or ""))


def regex_fulltext_search(query_text: str) -> bool:
    """A MySQL, PostgreSQL or SQL Server full-text match."""
    text = query_text or ""
    return bool(_FULLTEXT_SEARCH_RE.search(text)) or has_match_against(text)


# The two ends of a MySQL full-text match, ``MATCH (`` and ``) AGAINST (``.
_MATCH_OPEN_RE = re.compile(r"MATCH\s*\(", re.IGNORECASE)
_AGAINST_OPEN_RE = re.compile(r"\)\s*AGAINST\s*\(", re.IGNORECASE)


def has_match_against(text: str) -> bool:
    """``MATCH (<at least one character>) AGAINST (``, the argument on one line: exactly
    what ``MATCH\\s*\\(.+?\\)\\s*AGAINST\\s*\\(`` (no ``re.DOTALL``) finds, in linear time.

    That regex retried the rest of the line from every ``MATCH (``, which is quadratic
    on a line of them with no ``AGAINST`` (#902). Here each ``MATCH (`` looks for the
    first ``) AGAINST (`` at least one character after it, and the match holds when that
    one starts before the line ends (the whitespace around ``AGAINST`` may still cross
    lines, as in the regex).
    """
    againsts = [m.start() for m in _AGAINST_OPEN_RE.finditer(text)]
    if not againsts:
        return False
    line_end = -1
    for opening in _MATCH_OPEN_RE.finditer(text):
        start = opening.end()
        if start > line_end:
            line_end = text.find("\n", start)
            if line_end == -1:
                line_end = len(text)
        first = bisect_left(againsts, start + 1)
        if first < len(againsts) and againsts[first] < line_end:
            return True
    return False


def regex_pattern_match(query_text: str) -> bool:
    """A regular-expression or ``SIMILAR TO`` match."""
    return bool(_PATTERN_MATCH_RE.search(query_text or ""))


def regex_similarity_match(query_text: str) -> bool:
    """A trigram similarity function call."""
    return bool(_SIMILARITY_MATCH_RE.search(query_text or ""))


def regex_window_function(query_text: str) -> bool:
    """``)`` followed by ``OVER (``."""
    return bool(_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_ranking_window_function(query_text: str) -> bool:
    """``ROW_NUMBER()``/``RANK()``/``DENSE_RANK()`` followed by ``OVER``."""
    return bool(_RANKING_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_offset_window_function(query_text: str) -> bool:
    """A ``LAG``/``LEAD``/``NTILE`` call followed by ``OVER``."""
    return bool(_OFFSET_WINDOW_FUNCTION_RE.search(query_text or ""))


def regex_windowed_aggregate(query_text: str) -> bool:
    """A ``SUM``/``AVG``/``MIN``/``MAX``/``COUNT`` call followed by ``OVER``."""
    return bool(_WINDOWED_AGGREGATE_RE.search(query_text or ""))


def regex_grouping_sets(query_text: str) -> bool:
    """``GROUPING SETS (``, ``ROLLUP (`` or ``CUBE (`` after ``GROUP BY``, or ``WITH ROLLUP``
    / ``WITH CUBE``."""
    return bool(_GROUPING_SETS_RE.search(scan_sql(query_text or "")))


def regex_recursive_cte(query_text: str) -> bool:
    """``WITH RECURSIVE`` or a self-referencing ``UNION`` CTE, on :func:`scan_sql`'s output."""
    return _has_recursive_cte(scan_sql(query_text or ""))


# ---------------------------------------------------------------------------
# Comments, strings and quoted identifiers neutralised in one pass (moved here
# from ``query_facts`` with the recursive-CTE detector it feeds, #893).
# ---------------------------------------------------------------------------

# Matches a CTE's own name definition, whether it is the first one after ``WITH`` or a
# later one introduced by a comma (``WITH a AS (...), b AS (...)``) -- #381 review: the
# pre-review version only matched the first, so a recursive CTE named anywhere but first
# in the list went undetected. ``[#\w]+`` (#381 review round 2) rather than
# ``[a-zA-Z_]\w*``: by the time this runs, ``scan_sql`` has already turned a bracketed
# or double-quoted CTE name (``[Tree]``, ``"Tree"``) into its bare identifier, so the
# name itself is always a plain word here regardless of how the query originally quoted
# it.
_CTE_NAME_RE = re.compile(r"(?:\bwith\b|,)\s*([#\w]+)\s*(?:\([^)]*\))?\s+as\s*\(", re.IGNORECASE)


# The characters :func:`scan_sql` acts on: ``--``, ``/*``, a quote (with an optional
# ``N`` prefix), ``[`` and ``"``. Everything between two of them passes through.
_SCAN_SPECIAL_RE = re.compile(r"--|/\*|[Nn]?'|\[|\"")


def scan_sql(text: str) -> str:
    """One left-to-right pass over ``text`` neutralizing comments, strings and
    quoted identifiers, so every feature regex below runs against SQL as the engine
    would actually parse it, never raw source text (#381 review round 2).

    Two independent regex passes (strip strings, then strip comments) can each
    mis-handle what the other produces: a comment containing an apostrophe
    (``-- don't touch``) made the string-literal pass, if it ran first, swallow
    everything up to the *next* quote -- which could be an entire real statement
    later in the query. Scanning once, left to right, in the order these characters
    are actually encountered removes that ordering bug entirely.

    Each construct below is replaced the way the engine would resolve it, not simply
    deleted (so a real token on either side is never glued to its neighbour):

    - ``--`` to end of line: a space (the newline itself is kept, so later-line
      offsets are not shifted and a line comment never merges two lines).
    - ``/* ... */``: a space, with nesting support (SQL Server supports nested
      block comments) -- the whole span up to the matching outer ``*/`` is one
      comment, so text between an inner ``*/`` and the outer one is never
      mistaken for live code.
    - ``'...'`` string literals, with ``''``-doubled escapes and an optional
      ``N``/``n`` (Unicode) prefix: a space. Content is never a real keyword
      match, only text a customer or generator happened to write.
    - ``[ident]`` (T-SQL brackets, ``]]``-escaped) and ``"ident"`` (ANSI/Oracle/
      PostgreSQL double quotes, ``""``-escaped): the bare identifier, with the
      quoting removed. These name a column, table or CTE -- not a string -- so a
      feature regex must still be able to see and match the name inside
      (``MERGE [dbo].[Target]``, ``WITH "Tree" AS (...)``).

    Every other character passes through unchanged.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        # Copy the plain run up to the next character that starts a construct.
        hit = _SCAN_SPECIAL_RE.search(text, i)
        if hit is None:
            out.append(text[i:])
            break
        if hit.start() > i:
            out.append(text[i : hit.start()])
            i = hit.start()
        c = text[i]
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if text[i : i + 2] == "/*":
                    depth += 1
                    i += 2
                elif text[i : i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and text[i + 1] == "'"):
            if c in "Nn":
                i += 1
            i += 1  # opening quote
            while i < n:
                if text[i] == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if c == "[":
            j = i + 1
            body: list[str] = []
            while j < n:
                if text[j] == "]" and j + 1 < n and text[j + 1] == "]":
                    body.append("]")
                    j += 2
                    continue
                if text[j] == "]":
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        if c == '"':
            j = i + 1
            body = []
            while j < n:
                if text[j] == '"' and j + 1 < n and text[j + 1] == '"':
                    body.append('"')
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_recursive_cte(text: str) -> bool:
    """A CTE whose own body both ``UNION``s and refers back to its own name.

    SQL Server and Oracle have no ``WITH RECURSIVE`` keyword (unlike PostgreSQL and
    MySQL 8): any ``name AS (...)`` CTE can be recursive if its body unions the anchor
    member with one that references ``name`` again -- checked for every CTE in the
    ``WITH`` list, not only the first (#381 review). Requiring both a self-reference and
    a ``UNION`` inside the CTE's own (paren-balanced) body avoids counting a CTE that is
    merely referenced again later in the main query -- normal, non-recursive CTE use.
    """
    lower = text.lower()
    if "recursive" in lower and re.search(r"\bwith\s+recursive\b", text, re.IGNORECASE):
        return True
    if "union" not in lower:  # a self-referencing CTE body must UNION its anchor member
        return False
    for m in _CTE_NAME_RE.finditer(text):
        name = m.group(1)
        depth = 1
        i = m.end()
        start = i
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        body = text[start:i]
        if re.search(r"\bunion\b", body, re.IGNORECASE) and re.search(
            rf"\b{re.escape(name)}\b", body, re.IGNORECASE
        ):
            return True
    return False


_OVER_RE = re.compile(r"\bover\s*\(", re.IGNORECASE)


def _without_windows(text: str) -> str:
    """``text`` with each ``OVER (...)`` window blanked (balanced parentheses)."""
    out, i = [], 0
    for match in _OVER_RE.finditer(text):
        if match.start() < i:
            continue
        out.append(text[i : match.start()])
        depth, j = 1, match.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        out.append(" ")
        i = j
    out.append(text[i:])
    return "".join(out)


def regex_order_by(query_text: str) -> bool:
    """``ORDER BY``, not one inside a window's ``OVER (...)``."""
    return bool(ORDER_BY_RE.search(_without_windows(query_text or "")))


def regex_row_limit(query_text: str) -> bool:
    """``LIMIT``, ``FETCH FIRST``/``NEXT``, or ``TOP`` with a count."""
    return bool(_ROW_LIMIT_RE.search(query_text or ""))


def regex_top_n(query_text: str) -> bool:
    """``ORDER BY ... LIMIT``/``FETCH``, or ``TOP (n) ... ORDER BY``, not counting an
    ``ORDER BY`` inside a window's ``OVER (...)``."""
    text = _without_windows(query_text or "")
    order_by = _ORDER_BY_WORDS_RE.search(text)
    if order_by and _LIMIT_WORD_RE.search(text, order_by.end()):
        return True
    top = _TOP_COUNT_RE.search(text)
    return bool(top and _ORDER_BY_WORD_RE.search(text, top.end()))


def regex_timestamp_column_names(query_text: str) -> list[str]:
    """Each timestamp column name the text holds as a word, in order."""
    return [m.group(0) for m in _TIMESTAMP_COLUMN_RE.finditer(query_text or "")]


def regex_timestamp_column(query_text: str) -> bool:
    """A timestamp column name as a word."""
    return bool(regex_timestamp_column_names(query_text))


def regex_json_access(query_text: str) -> bool:
    """A JSON function, operator, cast or containment."""
    return bool(_JSON_ACCESS_RE.search(query_text or ""))


_UPDATE_RE = re.compile(r"\bupdate\b", re.IGNORECASE)
_SET_RE = re.compile(r"\bset\b", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)


_DELETE_RE = re.compile(r"\bdelete\b", re.IGNORECASE)
_USING_RE = re.compile(r"\busing\b", re.IGNORECASE)


def _top_level(text: str) -> str:
    """``text`` with every parenthesised part blanked, so a subquery's words are not read."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            out.append(" ")
            continue
        out.append(" " if depth else char)
    return "".join(out)


_FROM_SOURCE_RE = re.compile(
    r"^\s*([\w\[\]\"`.]+)(?:\s+(?:as\s+)?(?!where\b|option\b)(\w+))?\s*(?:where\b|option\b|;|$)",
    re.IGNORECASE,
)


def _bare(name: str) -> str:
    """An identifier without its schema, brackets or quotes, lower-cased."""
    return name.split(".")[-1].strip('[]"`').lower()


def _from_names_only_the_target(head: str, after_from: str) -> bool:
    """A SQL Server ``UPDATE p SET ... FROM [dbo].[Product] p WHERE ...``: the FROM names
    only the target again (its table or alias), with no join and no other table."""
    target = _bare(head.strip())
    match = _FROM_SOURCE_RE.match(after_from)
    if not target or match is None:
        return False
    # The same instance: the FROM's alias is the target, or it has no alias and its name
    # is (``UPDATE posts SET ... FROM posts p2`` is a self-join, a second instance).
    return target == ((match.group(2) or "").lower() or _bare(match.group(1)))


def regex_multi_table_write(query_text: str) -> bool:
    """An UPDATE or DELETE whose rows come from a join with another table, outside any
    parentheses: ``UPDATE a JOIN b ... SET`` or ``UPDATE a, b SET`` (MySQL), ``UPDATE
    ... SET ... FROM`` (PostgreSQL, SQL Server), ``DELETE ... USING`` (PostgreSQL) or
    ``DELETE a FROM a JOIN b`` (MySQL, SQL Server)."""
    text = _top_level(query_text or "")
    update = _UPDATE_RE.search(text)
    if update is not None:
        set_ = _SET_RE.search(text, update.end())
        if set_ is None:
            return False
        head = text[update.end() : set_.start()]
        if _JOIN_RE.search(head) or "," in head:
            return True
        from_ = FROM_WORD_RE.search(text, set_.end())
        if from_ is None:
            return False
        return not _from_names_only_the_target(head, text[from_.end() :])
    delete = _DELETE_RE.search(text)
    if delete is None:
        return False
    rest = text[delete.end() :]
    return bool(_USING_RE.search(rest) or _JOIN_RE.search(rest))


_WRITE_TARGET_RE = re.compile(
    rf"\b(?P<pre>for|key)?\s*\b(?:(?:insert|replace)(?:\s+(?:ignore|low_priority|delayed|"
    rf"high_priority))*(?:\s+into)?|update(?:\s+(?:low_priority|ignore))*|delete"
    rf"(?:\s+top\s*\([^)]*\)(?:\s+percent)?)?(?:\s+(?:low_priority|quick|ignore))*"
    rf"(?:\s+from)?|merge(?:\s+into)?|truncate(?:\s+table)?)"
    rf"(?:\s+top\s*\([^)]*\)(?:\s+percent)?)?"
    rf"\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})*)(?:\s+(?:as\s+)?(?P<alias>{_IDENT}))?",
    re.IGNORECASE,
)
_SET_END_RE = re.compile(r"\b(?:where|from|order|limit|output)\b|;", re.IGNORECASE)
_SET_COLUMN_RE = re.compile(rf"\s*(?:(?P<table>{_IDENT})\s*\.\s*)?{_IDENT}\s*=", re.IGNORECASE)
_ALIASED_TABLE_RE = re.compile(
    rf"\b(?:from|join|using|into|update)\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})*)"
    rf"\s+(?:as\s+)?(?P<alias>{_IDENT})",
    re.IGNORECASE,
)
_NOT_AN_ALIAS = frozenset(
    {
        "set",
        "where",
        "values",
        "select",
        "from",
        "join",
        "on",
        "using",
        "with",
        "default",
        "output",
        "returning",
        "when",
        "inner",
        "left",
        "right",
        "full",
        "cross",
        "outer",
    }
)


def regex_write_targets(query_text: str) -> tuple[str, ...]:
    """The tables a statement writes, by the keyword before each table name (the
    fallback of ``SqlFacts.write_targets``)."""
    text = scan_sql(query_text or "")
    aliases: dict[str, str] = {}
    for m in _ALIASED_TABLE_RE.finditer(text):
        alias = _bare(m.group("alias"))
        if alias not in _NOT_AN_ALIAS:
            aliases.setdefault(alias, _bare(m.group("table")))
    out: list[str] = []
    for m in _WRITE_TARGET_RE.finditer(text):
        if m.group("pre"):
            continue  # FOR UPDATE (a lock) or ON DUPLICATE KEY UPDATE (the same write)
        name = _bare(m.group("table"))
        if name in _NOT_AN_ALIAS:
            continue
        has_schema = "." in m.group("table")
        written = name if has_schema else aliases.get(name, name)
        qualifiers, unqualified = _set_qualifiers(text, m.end()) if _is_update(m) else ([], True)
        out.extend(aliases.get(q, q) for q in qualifiers)
        if unqualified or not qualifiers:
            out.append(written)
    return tuple(dict.fromkeys(out))


def _is_update(match: re.Match[str]) -> bool:
    return match.group(0).lstrip().lower().startswith("update")


def _set_qualifiers(text: str, start: int) -> tuple[list[str], bool]:
    """A multi-table ``UPDATE``'s ``SET`` columns: the table qualifiers they name, and
    whether any is unqualified (the fallback of ``SqlFacts.write_targets``)."""
    set_ = _SET_RE.search(text, start)
    if set_ is None:
        return [], True
    end = _SET_END_RE.search(text, set_.end())
    body = text[set_.end() : end.start() if end else len(text)]
    qualifiers: list[str] = []
    unqualified = False
    for part in _top_level(body).split(","):
        column = _SET_COLUMN_RE.match(part)
        if column is None:
            continue
        if column.group("table"):
            q = _bare(column.group("table"))
            if q not in qualifiers:
                qualifiers.append(q)
        else:
            unqualified = True
    return qualifiers, unqualified


def regex_range_predicate(query_text: str) -> bool:
    """``BETWEEN`` or a ``<``/``<=``/``>``/``>=`` comparison."""
    return bool(RANGE_PREDICATE_RE.search(query_text or ""))


def aggregate_function_names(query_text: str) -> frozenset[str]:
    """The aggregate function words (``count``, ``sum``, ``avg``, ``min``, ``max``,
    ``group_concat``, ``string_agg``, ``array_agg``) the text names, lower-cased: the
    same words as the aggregate fallback's count."""
    return frozenset(m.lower() for m in _AGGREGATE_WORD_RE.findall(query_text or ""))
