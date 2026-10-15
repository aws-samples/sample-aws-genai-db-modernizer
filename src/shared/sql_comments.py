"""Linear-time SQL comment stripping (#808).

A regex that repeats a comment alternation, such as
``(?:\\s*(?:--[^\\n]*|/\\*.*?\\*/))+\\s*$``, backtracks exponentially: a run of
``--`` can be read as one line comment or as many, and a run of ``*//*`` as one
block comment or as many, so when the match fails the engine tries every
split. These helpers scan the text once, left to right, with ``str.find`` to
skip each comment body, so each character is looked at a bounded number of
times and time grows linearly with the text's length.

A ``--`` line comment runs to the end of its line. A ``/* ... */`` block ends at
its first ``*/``, unless ``nested`` is set: PostgreSQL and SQL Server nest block
comments, so ``/* a /* b */ c */`` is one comment there, while MySQL and Oracle
end it at the first ``*/`` (:func:`block_comments_nest` says which a source
dialect does, #814). An unterminated ``/*`` is not a comment. String literals
are not recognised: callers that need ``'--'`` inside a literal to stay data
blank literals first.

This is the one comment rule for the Python side; ``src/ui/src/utils/sqlComments.js``
is its JavaScript copy, held to the same cases by
``tests/unit/shared/sql_comment_cases.json``.
"""

from __future__ import annotations

import re

__all__ = ["block_comments_nest", "strip_leading_comments", "strip_trailing_comments"]

# Source dialects whose block comments nest: PostgreSQL (and Aurora
# PostgreSQL), and SQL Server's T-SQL. MySQL/MariaDB and Oracle end a block
# comment at its first ``*/``.
_NESTING_DIALECTS = frozenset(
    {"postgresql", "postgres", "aurora_postgresql", "sqlserver", "mssql", "tsql"}
)

# Inside a nested block comment, the next opener or closer. A fixed two-way
# alternation of two-character literals: each search moves forward and never
# backtracks.
_BLOCK_TOKEN_RE = re.compile(r"/\*|\*/")


def block_comments_nest(dialect: str | None) -> bool:
    """Whether block comments nest in the source ``dialect`` (PostgreSQL, SQL Server)."""
    return (dialect or "").strip().lower() in _NESTING_DIALECTS


def _block_end(text: str, start: int, nested: bool) -> int:
    """The index just past the ``*/`` closing the block comment opened at
    ``start``, or ``-1`` when it is unterminated.

    Without ``nested`` that is the first ``*/``. With it, each ``/*`` inside
    opens one more level and each ``*/`` closes one, read left to right as
    PostgreSQL's lexer does (so ``/*/`` opens, ``*/*`` closes). One forward
    pass over the comment's own text.
    """
    if not nested:
        close = text.find("*/", start + 2)
        return -1 if close < 0 else close + 2
    depth = 1
    # finditer resumes after each match, so tokens are read left to right
    # without overlapping, as the lexer reads them.
    for token in _BLOCK_TOKEN_RE.finditer(text, start + 2):
        depth += 1 if token.group() == "/*" else -1
        if depth == 0:
            return token.end()
    return -1


def strip_leading_comments(
    text: str,
    *,
    hash_comments: bool = False,
    strip_following_space: bool = False,
    nested: bool = False,
) -> str:
    """``text`` without the comments it opens with.

    Removes every leading ``/* ... */`` block and ``--`` line comment (and the
    whitespace before each). A line comment counts only with its newline, so a
    statement that is nothing but ``-- note`` is kept. ``hash_comments`` also
    treats a ``#`` line comment as one (MySQL). ``strip_following_space`` also
    removes the whitespace after the last comment; without it that whitespace
    is kept. ``nested`` reads block comments the PostgreSQL/SQL Server way.
    Text that opens with no comment is returned unchanged.
    """
    n = len(text)
    i = cut = 0
    while True:
        while i < n and text[i].isspace():
            i += 1
        if text.startswith("--", i) or (hash_comments and text.startswith("#", i)):
            newline = text.find("\n", i)
            if newline < 0:
                break
            i = cut = newline + 1
        elif text.startswith("/*", i):
            end = _block_end(text, i, nested)
            if end < 0:
                break
            i = cut = end
        else:
            break
    if cut == 0:
        return text
    rest = text[cut:]
    return rest.lstrip() if strip_following_space else rest


def strip_trailing_comments(text: str, *, nested: bool = False) -> str:
    """``text`` without the comments it ends with.

    Removes every trailing ``-- ...`` line comment and ``/* ... */`` block, and
    the whitespace around them, so ``SELECT 1 /* a */ -- b`` becomes
    ``SELECT 1``. A comment in the middle of the statement is kept. ``nested``
    reads block comments the PostgreSQL/SQL Server way. Text that ends with no
    comment is returned unchanged, trailing whitespace included.
    """
    n = len(text)
    i = code_end = 0
    comment_after_code = False
    # Once a block comment turns out unterminated, later /* are not searched
    # again (that would rescan the rest of the text for each one). Without
    # nesting no */ follows, so none of them could close anyway; with nesting
    # the text is one PostgreSQL rejects, and treating the rest as code keeps
    # the scan linear.
    any_close_left = True
    while i < n:
        if text.startswith("--", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
            comment_after_code = True
        elif any_close_left and text.startswith("/*", i):
            end = _block_end(text, i, nested)
            if end < 0:  # unterminated: not a comment
                any_close_left = False
                continue
            i = end
            comment_after_code = True
        elif text[i].isspace():
            i += 1
        else:
            i += 1
            code_end = i
            comment_after_code = False
    return text[:code_end] if comment_after_code else text
