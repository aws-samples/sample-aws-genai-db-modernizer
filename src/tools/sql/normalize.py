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
facade planned for #557). It is a pure function with no I/O and no new
dependency, and nothing calls it yet -- it does not change any existing
detector's behavior on its own. For the facade: sqlglot's own dialect names
mostly, but not always, match ``DatabaseEngine`` below (``postgresql`` is
``postgres`` and ``sqlserver`` is ``tsql`` in sqlglot) -- that mapping lives
with the facade, not here.

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
    """

    text: str
    truncated: bool
    substitutions: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Dialect gating
# ---------------------------------------------------------------------------

# Source dialects whose collector digest text uses the MySQL
# performance_schema conventions this module normalizes. Matches
# ``DatabaseEngine`` in src/contracts/collector_input.py.
_MYSQL_FAMILY_DIALECTS = frozenset({"mysql", "mariadb"})

# ---------------------------------------------------------------------------
# Quote, comment and paren scanning
# ---------------------------------------------------------------------------

_QUOTE_CHARS = ("'", '"', "`")

# PostgreSQL dollar-quoting: $$...$$ or $tag$...$tag$. A tag, if present,
# follows identifier rules (starts with a letter or underscore), which is
# exactly what keeps this from ever matching a "$1"-style placeholder.
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


@dataclass(frozen=True)
class _Scan:
    mask: list[bool]  # True inside a quote, identifier or comment
    comment_mask: list[bool]  # True inside a comment only
    paren_depth: int  # net paren depth outside quotes/comments
    unterminated_quote: bool
    unterminated_comment: bool


def _scan(text: str, dialect: str) -> _Scan:
    """Single pass over ``text`` tracking quoted/comment spans and parens.

    ``mask[i]`` is True wherever character ``i`` sits inside a string
    literal, a quoted identifier, or a comment -- none of that text is a
    candidate for a substitution rule, and a paren inside it never counts
    toward the balance check. ``comment_mask[i]`` narrows that to comments
    only, so a truncation check can look at the text with comments removed
    without also discarding quoted content.

    Escaping conventions are dialect-gated: a doubled quote character
    (``''``, ``""``, `` `` ``) is a literal quote inside the span for every
    dialect (the ANSI SQL rule), but a backslash before the closing quote
    only escapes it for the MySQL family -- PostgreSQL's default
    ``standard_conforming_strings=on`` gives backslash no special meaning.
    PostgreSQL (and anything outside the MySQL family) also gets
    dollar-quoted strings (``$$...$$`` / ``$tag$...$tag$``), which have no
    escaping at all: the span runs verbatim until the same delimiter
    reappears.

    Comments are recognized for every dialect (``--`` and ``/* */``), plus
    ``#`` for the MySQL family only. A line comment (``--``/``#``) that
    runs to the end of the text is complete by grammar (no closing token
    is required); only an unterminated ``/* ... */`` counts as a
    truncation signal, the same way an unterminated quote does.
    """
    is_mysql = dialect.lower() in _MYSQL_FAMILY_DIALECTS
    n = len(text)
    mask = [False] * n
    comment_mask = [False] * n
    depth = 0
    # None, or one of: a quote char ('/"/`), "--", "#", "/*", or a dollar
    # delimiter such as "$$" / "$tag$".
    state: str | None = None
    i = 0

    while i < n:
        ch = text[i]

        if state is None:
            if text[i : i + 2] == "--":
                state = "--"
                mask[i] = mask[i + 1] = True
                comment_mask[i] = comment_mask[i + 1] = True
                i += 2
                continue
            if ch == "#" and is_mysql:
                state = "#"
                mask[i] = comment_mask[i] = True
                i += 1
                continue
            if text[i : i + 2] == "/*":
                state = "/*"
                mask[i] = mask[i + 1] = True
                comment_mask[i] = comment_mask[i + 1] = True
                i += 2
                continue
            if ch in _QUOTE_CHARS:
                state = ch
                mask[i] = True
                i += 1
                continue
            if ch == "$" and not is_mysql:
                m = _DOLLAR_TAG_RE.match(text, i)
                if m:
                    state = m.group(0)
                    for j in range(i, m.end()):
                        mask[j] = True
                    i = m.end()
                    continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
            continue

        # Inside a quote or comment span.
        mask[i] = True

        if state == "--" or state == "#":
            comment_mask[i] = True
            if ch == "\n":
                state = None
            i += 1
            continue

        if state == "/*":
            comment_mask[i] = True
            if text[i : i + 2] == "*/":
                mask[i + 1] = True
                comment_mask[i + 1] = True
                state = None
                i += 2
                continue
            i += 1
            continue

        if state.startswith("$"):
            delim = state
            if text[i : i + len(delim)] == delim:
                for j in range(i, i + len(delim)):
                    mask[j] = True
                state = None
                i += len(delim)
                continue
            i += 1
            continue

        # Inside a '/"/` quoted span.
        if is_mysql and ch == "\\" and state != "`" and i + 1 < n:
            mask[i + 1] = True
            i += 2
            continue
        if ch == state:
            if i + 1 < n and text[i + 1] == state:
                mask[i + 1] = True
                i += 2
                continue
            state = None
        i += 1

    unterminated_quote = state is not None and state not in ("--", "#", "/*")
    unterminated_comment = state == "/*"
    return _Scan(
        mask=mask,
        comment_mask=comment_mask,
        paren_depth=depth,
        unterminated_quote=unterminated_quote,
        unterminated_comment=unterminated_comment,
    )


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

_TRAILING_WORD_RE = re.compile(r"([A-Za-z_]+)\s*\Z")


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

    match = _TRAILING_WORD_RE.search(stripped)
    if match is None:
        return False
    word = match.group(1).upper()

    if word == "UPDATE":
        before = stripped[: match.start()].rstrip()
        prev = _TRAILING_WORD_RE.search(before)
        # "... FOR UPDATE" is a complete locking clause; a bare "UPDATE"
        # with nothing in front of it was cut off right after its own
        # keyword.
        return prev is None or prev.group(1).upper() != "FOR"

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

    if dialect.lower() not in _MYSQL_FAMILY_DIALECTS:
        return NormalizedQuery(text=query_text, truncated=truncated, substitutions=())

    text, mask = query_text, scan.mask
    substitutions: list[str] = []

    text, mask, applied = _apply_rule(text, mask, _PAREN_ELLIPSIS_RE, "paren_ellipsis", "(NULL)")
    substitutions.extend(applied)

    text, mask, applied = _apply_rule(text, mask, _LIST_ELLIPSIS_RE, "list_ellipsis", "NULL")
    substitutions.extend(applied)

    text, mask, applied = _apply_rule(text, mask, _PLACEHOLDER_RE, "bare_placeholder", "NULL")
    substitutions.extend(applied)

    return NormalizedQuery(text=text, truncated=truncated, substitutions=tuple(substitutions))
