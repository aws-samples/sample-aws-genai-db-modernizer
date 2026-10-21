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

__all__ = [
    "block_comments_nest",
    "leading_comments",
    "strip_comments",
    "strip_leading_comments",
    "strip_trailing_comments",
    "unwrap_executable_comment",
]

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

# MySQL's executable comment, ``/*! body */`` with an optional server version
# (``/*!40101 SET NAMES utf8 */``), and MariaDB's ``/*M! ... */``: the server
# runs the body, so it is code, not a comment (#780). An optimizer hint
# (``/*+ ... */``) is a comment.
_EXECUTABLE_COMMENT_OPEN_RE = re.compile(r"/\*M?!\d*")

# A character that can open a string literal or a comment (strip_comments).
_COMMENT_OR_QUOTE_START_RE = re.compile(r"['/#-]")


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


def _leading_comment_run(
    text: str, hash_comments: bool, nested: bool, mysql_executable: bool
) -> tuple[int, list[tuple[int, int]]]:
    """Where the comments ``text`` opens with end, and each one's body.

    Returns ``(cut, bodies)``: ``cut`` is the index just past the last leading
    comment (0 when there is none) and ``bodies`` the ``(start, end)`` span of
    each comment's text inside its markers, in order. The one scan behind
    :func:`strip_leading_comments` and :func:`leading_comments`; see the former
    for which comments count.
    """
    n = len(text)
    i = cut = 0
    bodies: list[tuple[int, int]] = []
    while True:
        while i < n and text[i].isspace():
            i += 1
        line = 2 if text.startswith("--", i) else 0
        if not line and hash_comments and text.startswith("#", i):
            line = 1
        if line:
            newline = text.find("\n", i)
            if newline < 0:
                break
            bodies.append((i + line, newline))
            i = cut = newline + 1
        elif mysql_executable and _EXECUTABLE_COMMENT_OPEN_RE.match(text, i):
            break
        elif text.startswith("/*", i):
            end = _block_end(text, i, nested)
            if end < 0:
                break
            bodies.append((i + 2, end - 2))
            i = cut = end
        else:
            break
    return cut, bodies


def strip_leading_comments(
    text: str,
    *,
    hash_comments: bool = False,
    strip_following_space: bool = False,
    nested: bool = False,
    mysql_executable: bool = False,
) -> str:
    """``text`` without the comments it opens with.

    Removes every leading ``/* ... */`` block and ``--`` line comment (and the
    whitespace before each). A line comment counts only with its newline, so a
    statement that is nothing but ``-- note`` is kept. ``hash_comments`` also
    treats a ``#`` line comment as one (MySQL). ``strip_following_space`` also
    removes the whitespace after the last comment; without it that whitespace
    is kept. ``nested`` reads block comments the PostgreSQL/SQL Server way.
    ``mysql_executable`` stops at a MySQL executable comment (``/*! ... */``),
    which is code (see :func:`unwrap_executable_comment`). Text that opens with
    no comment is returned unchanged.
    """
    cut, _ = _leading_comment_run(text, hash_comments, nested, mysql_executable)
    if cut == 0:
        return text
    rest = text[cut:]
    return rest.lstrip() if strip_following_space else rest


def leading_comments(
    text: str,
    *,
    hash_comments: bool = False,
    nested: bool = False,
    mysql_executable: bool = False,
) -> list[str]:
    """The text of each comment ``text`` opens with, in order, markers removed.

    The comments :func:`strip_leading_comments` removes, with the same options:
    ``-- a`` gives ``" a"``, ``/* b */`` gives ``" b "`` (whitespace is kept;
    callers trim). A nested block's inner markers are part of its text. A
    comment after the first code character is not leading, so it is not listed.
    """
    _, bodies = _leading_comment_run(text, hash_comments, nested, mysql_executable)
    return [text[start:end] for start, end in bodies]


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


def unwrap_executable_comment(text: str) -> str:
    """``text`` with the MySQL executable comment it opens with read as code.

    ``/*!40101 SET NAMES utf8 */;`` becomes `` SET NAMES utf8 ;``: the marker,
    its optional version number and the closing ``*/`` go, the body stays
    (MySQL's executable comments do not nest, so the body ends at the first
    ``*/``). MariaDB's ``/*M! ... */`` is read the same way. Only a comment
    that is the whole statement counts: nothing but whitespace or ``;`` may
    follow it, as in every mysqldump line. Otherwise, or when the text does
    not open with a closed executable comment (leading whitespace aside), it
    is returned unchanged.
    """
    start = len(text) - len(text.lstrip())
    marker = _EXECUTABLE_COMMENT_OPEN_RE.match(text, start)
    if marker is None:
        return text
    close = text.find("*/", marker.end())
    if close < 0:
        return text
    rest = text[close + 2 :]
    if rest.replace(";", "").strip():
        # Code follows, so this is a plain comment that starts with ``!``: MySQL
        # would run ``body code``, which is never one statement (#829 review).
        return text
    return text[marker.end() : close] + rest


def strip_comments(text: str, *, nested: bool = False, hash_comments: bool = False) -> str:
    """``text`` with every comment replaced by one space, wherever it is.

    A single-quoted string literal (``''`` escapes a quote) is data, so a
    ``--`` or ``/*`` inside one is kept, and so is a quote inside a comment. A
    ``--`` comment runs to its newline (the newline is kept), and so does a
    ``#`` comment with ``hash_comments`` (MySQL; elsewhere ``#`` is an
    operator or part of a name). ``nested`` reads block comments the
    PostgreSQL/SQL Server way. An unterminated block comment is not a comment,
    and an unterminated literal runs to the end of the text.
    """
    n = len(text)
    out: list[str] = []
    i = start = 0
    any_close_left = True  # as in strip_trailing_comments
    while i < n:
        ch = text[i]
        if ch == "'":
            close = i
            while True:
                close = text.find("'", close + 1)
                if close < 0 or not text.startswith("'", close + 1):
                    break
                close += 1  # an escaped '' quote: keep looking
            i = n if close < 0 else close + 1
        elif text.startswith("--", i) or (hash_comments and ch == "#"):
            newline = text.find("\n", i)
            end = n if newline < 0 else newline
            out.append(text[start:i])
            out.append(" ")
            i = start = end
        elif any_close_left and text.startswith("/*", i):
            end = _block_end(text, i, nested)
            if end < 0:
                any_close_left = False
                i += 2
                continue
            out.append(text[start:i])
            out.append(" ")
            i = start = end
        else:
            # Jump to the next character that can open a literal or a comment.
            following = _COMMENT_OR_QUOTE_START_RE.search(text, i + 1)
            i = n if following is None else following.start()
    out.append(text[start:])
    return "".join(out)
