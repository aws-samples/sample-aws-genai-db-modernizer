"""The scanner ``normalize._scan`` was before it read its spans from ``sql_spans`` (#1451).

Kept as the reference the equivalence tests compare the new scan with; it is not used by
any product code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.shared.sql_comments import block_comments_nest

_MYSQL_FAMILY_DIALECTS = frozenset({"mysql", "mariadb"})

_QUOTE_CHARS = ("'", '"', "`")

_POSTGRES_DIALECTS = frozenset({"postgresql", "postgres"})
# SQL Server quotes a name in brackets: [a name]; ``]]`` is a literal ``]``.
_BRACKET_DIALECTS = frozenset({"sqlserver", "tsql", "mssql"})
# Oracle alternative quoting: q'[...]', q'(...)', q'{...}', q'<...>' or q'!...!' (and the
# national nq'...' form); the text runs until the closing delimiter and a quote.
_ORACLE_Q_QUOTE_RE = re.compile(r"(?:[nN])?[qQ]'(?P<d>[^\s])")
_Q_CLOSERS = {"[": "]", "(": ")", "{": "}", "<": ">"}


def _starts_token(text: str, i: int) -> bool:
    """Whether ``text[i]`` begins a token (is not inside an identifier like ``name'``)."""
    return i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$#")


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


def _scan(text: str, dialect: str, brackets: bool = False) -> _Scan:
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
    ``#`` for the MySQL family only. A block comment nests for PostgreSQL and
    SQL Server (``/* a /* b */ c */`` is one comment, so an inner opener leaves
    it open) and ends at its first ``*/`` elsewhere (#825), as
    :func:`src.shared.sql_comments.block_comments_nest` says; the depth is one
    counter on the same forward pass. A line comment (``--``/``#``) that
    runs to the end of the text is complete by grammar (no closing token
    is required); only an unterminated ``/* ... */`` counts as a
    truncation signal, the same way an unterminated quote does.

    ``brackets`` also reads a SQL Server ``[name]`` as quoted (off by default, so the
    other callers' masks stay as they were).
    """
    is_mysql = dialect.lower() in _MYSQL_FAMILY_DIALECTS
    is_oracle = dialect.lower() == "oracle"
    is_pg = dialect.lower() in _POSTGRES_DIALECTS
    nests = block_comments_nest(dialect)
    comment_depth = 0
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
                comment_depth = 1
                mask[i] = mask[i + 1] = True
                comment_mask[i] = comment_mask[i + 1] = True
                i += 2
                continue
            if is_oracle and (m := _ORACLE_Q_QUOTE_RE.match(text, i)) and _starts_token(text, i):
                # q'[...]' / nq'{...}': ends at the closing delimiter then a quote.
                state = "q" + _Q_CLOSERS.get(m.group("d"), m.group("d")) + "'"
                for j in range(i, m.end()):
                    mask[j] = True
                i = m.end()
                continue
            if is_pg and ch in "eE" and text[i + 1 : i + 2] == "'" and _starts_token(text, i):
                # E'...': a backslash escapes the next character.
                state = "E'"
                mask[i] = mask[i + 1] = True
                i += 2
                continue
            if ch in _QUOTE_CHARS or (brackets and ch == "["):
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
            pair = text[i : i + 2]
            if pair == "/*" and nests:
                mask[i + 1] = True
                comment_mask[i + 1] = True
                comment_depth += 1
                i += 2
                continue
            if pair == "*/":
                mask[i + 1] = True
                comment_mask[i + 1] = True
                comment_depth -= 1
                if comment_depth == 0:
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

        if state[0] == "q" and len(state) == 3:
            if text[i : i + 2] == state[1:]:
                mask[i + 1] = True
                state = None
                i += 2
                continue
            i += 1
            continue

        if state == "E'":
            if ch == "\\" and i + 1 < n:
                mask[i + 1] = True
                i += 2
                continue
            if ch == "'":
                if text[i + 1 : i + 2] == "'":
                    mask[i + 1] = True
                    i += 2
                    continue
                state = None
            i += 1
            continue

        # Inside a '/"/` quoted span.
        if is_mysql and ch == "\\" and state != "`" and i + 1 < n:
            mask[i + 1] = True
            i += 2
            continue
        if ch == ("]" if state == "[" else state):
            if i + 1 < n and text[i + 1] == ch:
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
