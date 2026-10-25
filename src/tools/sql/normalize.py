"""
Normalizes collector digest text before it reaches a SQL parser.

MySQL's ``performance_schema`` records a *normalized digest* of each query,
not the SQL that ran: a value list collapses to ``(...)``, a run of
literals collapses to ``?, ...`` wherever it occurs (not only at the end of
the statement, e.g. ``LIMIT ?, ...`` or ``SELECT ?, ... FROM DUAL``), and
every remaining literal becomes a bare ``?``. None of that is valid SQL on
its own, so a grammar-based parser rejects it outright. PostgreSQL's
``pg_stat_statements`` instead rewrites literals as ``$1``-style
placeholders, which already parse cleanly.

This module is the one shared step every SQL-parsing call site should run
digest text through first, so the digest syntax is handled once instead of
being re-learned by each caller (today's regex detectors, and the sqlglot
facade, :mod:`src.tools.sql.facade`, #557). It is a pure function with no
I/O and no new dependency; the facade is its only caller, so it does not
change any regex detector's behavior. sqlglot's own dialect names mostly, but
not always, match ``DatabaseEngine`` below (``postgresql`` is ``postgres`` and
``sqlserver`` is ``tsql`` in sqlglot) -- that mapping lives with the facade,
not here.

Rules applied for MySQL-family dialects (``mysql``, ``mariadb``):

- ``(...)`` -- and spacing variants such as ``( ... )`` -- becomes
  ``(NULL)``.
- ``?, ...`` (an elided run of literals) becomes ``NULL``, wherever it
  appears in the text, not only at the end.
- A remaining bare ``?`` placeholder becomes ``NULL``. A ``?`` inside a
  single-quoted string literal, a quoted identifier (backtick- or
  double-quoted), or a comment is left untouched, and so is a
  ``$1``-style placeholder (it never occurs in real MySQL digest text, but
  the rule must not mistake it for one).

Every other dialect (``postgresql``, ``sqlserver``, ``oracle``, ``db2``) is
returned unchanged aside from truncation detection: their digest/placeholder
format does not use the forms above, and for PostgreSQL a bare ``?`` can be
a real operator (the jsonb ``?``/``?|``/``?&`` operators), not a placeholder.

Truncation detection: a digest's length is capped
(``performance_schema_max_digest_length`` on MySQL), so a long query can be
cut off mid-statement. Rather than guess how to repair it, this module flags
that case as ``truncated`` using independent, structural signals computed on
the original text: it ends inside an unterminated string literal, quoted
identifier, or block comment; parentheses are unbalanced outside of quotes
and comments; or -- once comments are stripped -- it ends on a token that
can never end a valid statement (a comma, a binary operator, or a clause
keyword such as ``FROM``, ``WHERE``, ``AND``, ``JOIN``, ``BY``, ``IN``, or
``VALUES``). The deliberate ``...`` ellipsis marker used by the rules above
is not itself treated as evidence of truncation, and a handful of tokens
that *can* legitimately end a statement (``FOR UPDATE``, ``LIMIT ALL``,
``RETURNING *``, a boolean ``= on``) are deliberately not flagged.

Known gap: MySQL 8 is reported to render some multi-row ``INSERT ...
VALUES`` digests with a trailing ``/* , ... */`` comment to mark elided
rows. The MySQL reference manual's statement-digest page documents generic
``...`` truncation but not this specific comment form, so this module does
not special-case it -- a comment there is scanned like any other comment
(ignored for quote/paren purposes, stripped before the trailing-token
check) but its elision is not rewritten to ``NULL``. Revisit if a
digest from a real server confirms the exact form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.shared.sql_comments import block_comment_end, block_comments_nest
from src.shared.sql_spans import comments_literals_and_identifiers_for, scan_grammar_for, spans

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedQuery:
    """Result of normalizing one piece of collector digest text.

    Attributes:
        text: The normalized query text (unchanged from the input for
            dialects outside the MySQL family).
        truncated: True if the original text looks like it was cut off
            mid-statement by a digest length cap, rather than a complete
            (if digest-mangled) statement.
        substitutions: One entry per individual substitution applied, named
            by rule (``"paren_ellipsis"``, ``"list_ellipsis"``,
            ``"bare_placeholder"``). Empty when nothing was substituted,
            including for every non-MySQL-family dialect.
        ends_incomplete: The text ends on a token that can never end a
            statement (a trailing ``AND``, a comma, an operator, an open
            parenthesis, a dangling ``.``): the collector's own cut-off
            signal, which an unterminated quote is not (a quote scanner can
            misread a dialect's string syntax; #986).
    """

    text: str
    truncated: bool
    substitutions: tuple[str, ...] = ()
    ends_incomplete: bool = False


# ---------------------------------------------------------------------------
# Dialect gating
# ---------------------------------------------------------------------------

# Source dialects whose collector digest text uses the MySQL
# performance_schema conventions this module normalizes. Matches
# ``DatabaseEngine`` in src/contracts/collector_input.py.
_MYSQL_FAMILY_DIALECTS = frozenset({"mysql", "mariadb"})


_EXECUTABLE_OPEN_RE = re.compile(r"/\*M?!")


def without_comments_after_terminator(text: str, source_engine: str | None) -> str:
    """``text`` cut after its last statement-ending ``;`` when only comments follow it.

    ``MERGE ...; /*a='b'*/`` becomes ``MERGE ...;``: a parser reads the comment as one
    more (empty) statement, so it must not reach one (#1335). The text is read with the
    scan that knows the quote forms of ``source_engine`` (a source engine or dialect
    name): a ``;`` in a string literal, a quoted name, a dollar quote, ``E'...'``,
    Oracle ``q'{...}'``, a SQL Server ``[...]`` name or a comment ends nothing.
    Only comments and whitespace may follow that ``;``. Code after it (a second
    statement, a MySQL ``/*! ... */`` executable comment, an unterminated ``/*`` or
    quote) leaves the text unchanged, and so does text with no ``;``, or none followed
    by a comment. A ``--`` (or MySQL ``#``) comment with no newline runs to the end.
    """
    if ";" not in text:
        return text
    engine = (source_engine or "").strip().lower()
    scan = _scan(text, engine, brackets=engine in _BRACKET_DIALECTS)
    if scan.unterminated_quote or scan.unterminated_comment:
        return text
    mask, comments = scan.mask, scan.comment_mask
    last = -1
    for m in re.finditer(";", text):
        if not mask[m.start()]:
            last = m.end()
    if last < 0:
        return text
    tail = range(last, len(text))
    if not any(comments[k] for k in tail):
        return text
    if any(not (text[k].isspace() or comments[k]) for k in tail):
        return text
    if engine in _MYSQL_FAMILY_DIALECTS and _EXECUTABLE_OPEN_RE.search(text, last):
        return text
    return text[:last]


def without_comments_between_statements(text: str, source_engine: str | None) -> str:
    """``text`` without the comments that stand alone after a statement-ending ``;``.

    ``SELECT 1; /* x */ SELECT 2`` becomes ``SELECT 1; SELECT 2`` and
    ``a; /* x */ ; /* y */`` becomes ``a; ;``: a parser reads such a comment as a
    statement of its own (#1340), so it must not reach one. Comments after the last
    ``;`` are cut with :func:`without_comments_after_terminator`. The text is read
    with the same dialect-aware scan, so a ``;`` or a comment marker inside a string,
    a quoted name, a dollar quote, ``E'...'``, ``q'{...}'`` or a ``[name]`` is data
    and stays. Only comments are removed, never code: a comment that sits inside a
    statement, or before the first one, is left alone, and so is a MySQL executable
    comment (``/*! ... */``), which is code. Text with an unterminated quote or
    comment is returned unchanged.
    """
    text = without_comments_after_terminator(text, source_engine)
    if ";" not in text:
        return text
    engine = (source_engine or "").strip().lower()
    scan = _scan(text, engine, brackets=engine in _BRACKET_DIALECTS)
    if scan.unterminated_quote or scan.unterminated_comment:
        return text
    comments = scan.comment_mask
    mysql = engine in _MYSQL_FAMILY_DIALECTS
    nested = block_comments_nest(engine)
    out: list[str] = []
    after_terminator = False
    i, n = 0, len(text)
    while i < n:
        if comments[i]:
            # One comment at a time: a run of adjacent comments can mix a plain one
            # with a MySQL executable one.
            if text.startswith("--", i) or (mysql and text[i] == "#"):
                newline = text.find("\n", i)
                end = n if newline < 0 else newline + 1
            else:
                end = block_comment_end(text, i, nested)
            executable = mysql and _EXECUTABLE_OPEN_RE.match(text, i) is not None
            if not (after_terminator and not executable):
                out.append(text[i:end])
                after_terminator = False
            i = end
            continue
        ch = text[i]
        if not ch.isspace():
            after_terminator = ch == ";"  # a quoted ";" is followed by its closing quote
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Quote, comment and paren scanning
# ---------------------------------------------------------------------------

# SQL Server quotes a name in brackets: [a name]; ``]]`` is a literal ``]``.
_BRACKET_DIALECTS = frozenset({"sqlserver", "tsql", "mssql"})


# The closer a doubled copy of which is an escape; a span that "ends" right before one
# has not ended (see ``_scan``).
_DOUBLED_CLOSER = {"'": "'", '"': '"', "E'": "'", "e'": "'", "[": "]"}


@dataclass(frozen=True)
class _Scan:
    mask: list[bool]  # True inside a quote, identifier or comment
    comment_mask: list[bool]  # True inside a comment only
    paren_depth: int  # net paren depth outside quotes/comments
    unterminated_quote: bool
    unterminated_comment: bool


def _scan(text: str, dialect: str, brackets: bool = False) -> _Scan:
    """Single pass over ``text`` tracking quoted/comment spans and parens.

    ``mask[i]`` is True wherever character ``i`` sits inside a string
    literal, a quoted identifier, or a comment -- none of that text is a
    candidate for a substitution rule, and a paren inside it never counts
    toward the balance check. ``comment_mask[i]`` narrows that to comments
    only, so a truncation check can look at the text with comments removed
    without also discarding quoted content.

    The spans are the shared grammar's (:func:`src.shared.sql_spans.scan_grammar_for`,
    #1451), so this scan and the text facts read each dialect's comment, string and
    identifier forms from one place: a doubled quote is a literal quote in every
    dialect, a backslash escapes only for the MySQL family (PostgreSQL's default
    ``standard_conforming_strings=on`` gives it no meaning, and ``E'...'`` is the
    exception), PostgreSQL and the other non-MySQL dialects have dollar-quoted
    strings, Oracle has ``q'[...]'``, and ``#`` starts a comment for the MySQL
    family only. A block comment nests for PostgreSQL and SQL Server and ends at
    its first ``*/`` elsewhere (#825). A line comment (``--``/``#``) that runs to
    the end of the text is complete by grammar (no closing token is required);
    a span with no closer runs to the end of the text, and an unterminated
    ``/* ... */`` or quote counts as a truncation signal.

    ``brackets`` also reads a SQL Server ``[name]`` as quoted (off by default, so the
    other callers' masks stay as they were).
    """
    grammar = scan_grammar_for(dialect, brackets)
    n = len(text)
    mask = [False] * n
    comment_mask = [False] * n
    depth = 0
    unterminated_quote = unterminated_comment = False
    last = 0
    i = 0
    while (match := grammar.opener.search(text, i)) is not None:
        start, opener = match.start(), match.group()
        depth += text.count("(", last, start) - text.count(")", last, start)
        end = grammar.end(text, start, opener)
        comment = opener in ("--", "#", "/*")
        if end >= 0 and opener in _DOUBLED_CLOSER and text.startswith(_DOUBLED_CLOSER[opener], end):
            # The text ends inside the span, on a doubled closer (``[a]]``): the grammar
            # closes it at the first of the pair, as a regex would; a digest cut there is
            # cut inside the span.
            end = -1
        if end < 0:
            # No closer: the span runs to the end of the text.
            end = n
            unterminated_comment = opener == "/*"
            unterminated_quote = not comment
            last = n
            mask[start:] = [True] * (n - start)
            if comment:
                comment_mask[start:] = [True] * (n - start)
            break
        if opener in ("--", "#") and end < n:
            end += 1  # the newline that ends a line comment is part of it
        mask[start:end] = [True] * (end - start)
        if comment:
            comment_mask[start:end] = [True] * (end - start)
        last = i = end
    else:
        depth += text.count("(", last) - text.count(")", last)
    return _Scan(
        mask=mask,
        comment_mask=comment_mask,
        paren_depth=depth,
        unterminated_quote=unterminated_quote,
        unterminated_comment=unterminated_comment,
    )


def quoted_mask(text: str, dialect: str, *, brackets: bool = False) -> list[bool]:
    """Per character of ``text``: True inside a string literal, a quoted
    identifier or a comment (the same scan the rules below use), so a caller
    can rewrite SQL syntax without touching quoted content. ``brackets`` also
    reads a SQL Server ``[name]`` as quoted (``]]`` escaping a ``]``)."""
    return _scan(text, dialect, brackets=brackets).mask


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------

# Clause keywords and operators that always expect something to follow --
# a statement cannot grammatically end on one of these. "ON", "ALL" and
# "SET" are deliberately excluded: each one also ends real, complete
# statements (a boolean "= on", "LIMIT ALL"/"SHOW ALL", a config "SET x =
# ..." style assignment), and none of the three truncated wordpress digests
# needs them to be flagged. "UPDATE" gets a dedicated check below instead
# of a plain membership test, because "... FOR UPDATE" is a complete
# locking clause.
_DANGLING_KEYWORDS = frozenset(
    {
        "AND",
        "OR",
        "WHERE",
        "FROM",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "OUTER",
        "CROSS",
        "IN",
        "NOT",
        "IS",
        "LIKE",
        "BETWEEN",
        "AS",
        "SELECT",
        "VALUES",
        "INTO",
        "BY",
        "ORDER",
        "GROUP",
        "HAVING",
        "UNION",
        "THEN",
        "WHEN",
        "CASE",
        "ELSE",
        "LIMIT",
        "OFFSET",
        "DELETE",
        "INSERT",
        "DISTINCT",
        "EXISTS",
    }
)

# A trailing token that can never end a statement: a comma, a binary
# operator, or an unmatched "(" (already caught by the paren check too, but
# a direct signal is cheap). "*" is deliberately excluded -- "SELECT *" and
# "RETURNING *" both end a complete statement on it.
_DANGLING_TRAILING_CHARS = frozenset(",=<>!+-/%:(")

_WORD_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")


def _trailing_word(text: str) -> tuple[str, int] | None:
    r"""The run of ASCII letters/underscores that ends ``text`` (trailing
    whitespace allowed), and where it starts; None if there is none.

    Same answer as ``re.search(r"([A-Za-z_]+)\s*\Z", text)`` but a backward
    scan, so it is linear: that regex is quadratic on a long run of letters
    that doesn't end the text (a 1 MB string literal hung it, #557 review).
    """
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    start = end
    while start > 0 and text[start - 1] in _WORD_CHARS:
        start -= 1
    return (text[start:end], start) if start < end else None


def _strip_comments(text: str, comment_mask: list[bool]) -> str:
    """Collapse every comment span to a single space.

    So a trailing-token check never mistakes comment content -- including
    its own closing ``*/``, or a quote character written in English inside
    it -- for part of the statement.
    """
    out: list[str] = []
    in_comment = False
    for ch, is_comment in zip(text, comment_mask, strict=True):
        if is_comment:
            if not in_comment:
                out.append(" ")
            in_comment = True
        else:
            out.append(ch)
            in_comment = False
    return "".join(out)


def _is_truncated(text: str, scan: _Scan) -> bool:
    if scan.unterminated_quote or scan.unterminated_comment or scan.paren_depth != 0:
        return True
    return _ends_incomplete(text, scan)


def _ends_incomplete(text: str, scan: _Scan) -> bool:
    """The text, with comments removed, ends on a token that can never end a statement."""
    stripped = _strip_comments(text, scan.comment_mask).rstrip()
    if not stripped:
        return False

    last_char = stripped[-1]
    if last_char in _DANGLING_TRAILING_CHARS:
        return True
    # A lone trailing "." is a dangling column/identifier reference. The
    # literal "..." ellipsis marker (two or more dots) is a deliberate
    # digest convention, not evidence of truncation.
    if last_char == "." and not stripped.endswith(".."):
        return True

    match = _trailing_word(stripped)
    if match is None:
        return False
    word = match[0].upper()

    if word == "UPDATE":
        before = stripped[: match[1]].rstrip()
        prev = _trailing_word(before)
        # "... FOR UPDATE" is a complete locking clause; a bare "UPDATE"
        # with nothing in front of it was cut off right after its own
        # keyword.
        return prev is None or prev[0].upper() != "FOR"

    return word in _DANGLING_KEYWORDS


# ---------------------------------------------------------------------------
# Substitution rules
# ---------------------------------------------------------------------------

_PAREN_ELLIPSIS_RE = re.compile(r"\(\s*\.\.\.\s*\)")
# An elided run of literals: "?, ..." -- can appear anywhere in the text
# (e.g. "SELECT ?, ... FROM DUAL", "LIMIT ?, ... FOR UPDATE"), not only at
# the end of the statement.
_LIST_ELLIPSIS_RE = re.compile(r"\?\s*,\s*\.\.\.")
_PLACEHOLDER_RE = re.compile(r"\?")


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _apply_rule(
    text: str, mask: list[bool], pattern: re.Pattern[str], rule_name: str, replacement: str
) -> tuple[str, list[bool], list[str]]:
    """Replace every match of ``pattern`` that lies outside a quoted or
    commented span.

    A match that overlaps any masked character is left untouched. Returns
    the new text, a mask rebuilt to the new text's length (replacement
    characters are never masked), and one ``rule_name`` entry per
    replacement actually made.
    """
    text_parts: list[str] = []
    mask_parts: list[list[bool]] = []
    applied: list[str] = []
    last_end = 0

    for m in pattern.finditer(text):
        start, end = m.span()
        if any(mask[start:end]):
            continue
        text_parts.append(text[last_end:start])
        mask_parts.append(mask[last_end:start])
        # Keep the replacement a separate token: "?AS" must become "NULL AS",
        # not "NULLAS", or a second pass would read a different statement.
        piece = replacement
        if start > 0 and _is_word_char(text[start - 1]):
            piece = " " + piece
        if end < len(text) and _is_word_char(text[end]):
            piece = piece + " "
        text_parts.append(piece)
        mask_parts.append([False] * len(piece))
        applied.append(rule_name)
        last_end = end

    text_parts.append(text[last_end:])
    mask_parts.append(mask[last_end:])

    new_text = "".join(text_parts)
    new_mask = [flag for part in mask_parts for flag in part]
    return new_text, new_mask, applied


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def normalize_digest_text(query_text: str, dialect: str) -> NormalizedQuery:
    """Normalize one piece of collector digest/query text.

    Args:
        query_text: The raw text as collected (a MySQL
            ``performance_schema`` digest, a PostgreSQL
            ``pg_stat_statements`` query, or similar).
        dialect: The source dialect, e.g. ``"mysql"``, ``"mariadb"``,
            ``"postgresql"``, ``"sqlserver"``, ``"oracle"``, ``"db2"``
            (matches ``DatabaseEngine`` in
            ``src/contracts/collector_input.py``). Only the MySQL family
            gets the substitution rules; every other dialect is returned
            unchanged aside from truncation detection.

    Returns:
        A :class:`NormalizedQuery` with the normalized text, a
        ``truncated`` flag, and the substitutions applied.
    """
    scan = _scan(query_text, dialect)
    truncated = _is_truncated(query_text, scan)
    incomplete = _ends_incomplete(query_text, scan)

    if dialect.lower() not in _MYSQL_FAMILY_DIALECTS:
        return NormalizedQuery(query_text, truncated, (), incomplete)

    text, mask = query_text, scan.mask
    substitutions: list[str] = []

    text, mask, applied = _apply_rule(text, mask, _PAREN_ELLIPSIS_RE, "paren_ellipsis", "(NULL)")
    substitutions.extend(applied)

    text, mask, applied = _apply_rule(text, mask, _LIST_ELLIPSIS_RE, "list_ellipsis", "NULL")
    substitutions.extend(applied)

    text, mask, applied = _apply_rule(text, mask, _PLACEHOLDER_RE, "bare_placeholder", "NULL")
    substitutions.extend(applied)

    return NormalizedQuery(text, truncated, tuple(substitutions), incomplete)


# ---------------------------------------------------------------------------
# Literal shape (#412)
# ---------------------------------------------------------------------------

# A numeric literal: digits with an optional fraction or exponent and an Oracle
# ``f``/``d`` suffix, or a leading-dot fraction. Not part of a name (``t1``, ``c$2``),
# a bind variable (``:1``) or a qualified name.
_NUMBER = r"(?<![\w$#:.])(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[fFdD]?(?![\w$#])"
# An ``ORDER BY`` / ``GROUP BY`` list of plain names and positions: ``ORDER BY 1`` and
# ``ORDER BY 2`` sort by different columns, so the digits there are structure.
_ORDER_ITEM = r"[\w.\"$#]+(?:\s+(?:ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?"
_ORDERING = (
    r"\b(?:ORDER|GROUP)\s+(?:SIBLINGS\s+)?BY\s+" + _ORDER_ITEM + r"(?:\s*,\s*" + _ORDER_ITEM + r")*"
)
_LITERAL_OR_ORDERING_RE = re.compile(f"(?P<keep>{_ORDERING})|(?P<num>{_NUMBER})", re.IGNORECASE)
_IN_LIST_RE = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)")
_NATIONAL_PREFIX_RE = re.compile(r"(?<![\w$#])[nN]$")
_PLACEHOLDER = "?"


def _mask_numbers(code: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return match.group("keep") or _PLACEHOLDER

    return _LITERAL_OR_ORDERING_RE.sub(repl, code).upper()


def literal_shape(query_text: str, dialect: str) -> str:
    """The statement with every literal value masked, so literal variants share one text.

    String literals (in the dialect's own forms, via :mod:`src.shared.sql_spans`: doubled
    quotes, Oracle ``q'[...]'``, ``N'...'``), numbers, and dates (``DATE '...'``, the
    strings in ``TO_DATE('...', '...')``) become ``?``; an ``IN`` list of literals becomes
    ``IN (?, ...)`` whatever its length; keyword case, whitespace, plain comments and a
    trailing ``;`` are dropped. Everything that makes a statement a different shape is
    kept: names (quoted identifiers keep their case), operators, the number of values
    outside an ``IN`` list (``VALUES (?, ?)`` is not ``VALUES (?, ?, ?)``), ``ORDER BY`` /
    ``GROUP BY`` positions, optimizer hints (``/*+ ... */``) and bind variables.

    Oracle's ``SQL_ID`` hashes the exact text, so it cannot do this; its
    ``FORCE_MATCHING_SIGNATURE`` does it in the database, and this is the same step for a
    collection made without it.
    """
    grammar = scan_grammar_for(dialect)
    out: list[str] = []
    last = 0

    def code(segment: str) -> None:
        out.append(_mask_numbers(segment))

    for start, end, opener in spans(query_text, grammar):
        segment = query_text[last:start]
        text = query_text[start:end]
        is_comment = opener in ("--", "#", "/*")
        if is_comment:
            code(segment)
            if text.startswith(("/*+", "--+")):
                out.append(" " + text + " ")
            else:
                out.append(" ")
        elif opener in grammar.identifiers:
            code(segment)
            out.append(text)
        else:
            out.append(_mask_numbers(_NATIONAL_PREFIX_RE.sub("", segment)))
            out.append(_PLACEHOLDER)
        last = end
    code(query_text[last:])

    shaped = " ".join("".join(out).split())
    shaped = _IN_LIST_RE.sub("IN (?, ...)", shaped)
    return shaped.rstrip("; ")


def _follows_in_keyword(code: str, paren: int) -> bool:
    """Whether the ``(`` at ``paren`` directly follows an ``IN`` keyword."""
    before = code[:paren].rstrip()
    return before[-2:].upper() == "IN" and not _is_word_char(before[-3:-2] or " ")


def open_in_list_start(shape: str, dialect: str) -> int | None:
    """Where the outermost ``IN (`` list left open at the end of ``shape`` begins, or None.

    A statement cut inside an ``IN`` list (``... id IN (?, ?, ?``) cannot collapse that list
    the way :func:`literal_shape` does for a whole one, so a comparison with a variant stops
    before the list (#1541). Strings, comments and quoted names are read with the
    dialect's own spans (:mod:`src.shared.sql_spans`), so a parenthesis inside one is no
    parenthesis. The index is that of the list's opening parenthesis.
    """
    grammar = comments_literals_and_identifiers_for(dialect)
    chars = list(shape)
    for start, end, _opener in spans(shape, grammar):
        chars[start:end] = "_" * (end - start)
    code = "".join(chars)
    stack: list[int | None] = []  # per open parenthesis: its index if an IN list opens it
    for i, ch in enumerate(code):
        if ch == "(":
            stack.append(i if _follows_in_keyword(code, i) else None)
        elif ch == ")" and stack:
            stack.pop()
    return next((start for start in stack if start is not None), None)


def literal_shape_digest(query_text: str, dialect: str) -> str:
    """A short stable digest of :func:`literal_shape`: equal for literal variants."""
    import hashlib

    return hashlib.sha256(literal_shape(query_text, dialect).encode()).hexdigest()[:16]
