"""Linear-time SQL comment stripping (#808).

A regex that repeats a comment alternation, such as
``(?:\\s*(?:--[^\\n]*|/\\*.*?\\*/))+\\s*$``, backtracks exponentially: a run of
``--`` can be read as one line comment or as many, and a run of ``*//*`` as one
block comment or as many, so when the match fails the engine tries every
split. These helpers scan the text once, left to right, with ``str.find`` to
skip each comment body, so each character is looked at a bounded number of
times and time grows linearly with the text's length.

A ``--`` line comment runs to the end of its line; a ``/* ... */`` block ends at
its first ``*/`` (no nesting); an unterminated ``/*`` is not a comment. String
literals are not recognised: callers that need ``'--'`` inside a literal to stay
data blank literals first.
"""

from __future__ import annotations

__all__ = ["strip_leading_comments", "strip_trailing_comments"]


def strip_leading_comments(
    text: str, *, hash_comments: bool = False, strip_following_space: bool = False
) -> str:
    """``text`` without the comments it opens with.

    Removes every leading ``/* ... */`` block and ``--`` line comment (and the
    whitespace before each). A line comment counts only with its newline, so a
    statement that is nothing but ``-- note`` is kept. ``hash_comments`` also
    treats a ``#`` line comment as one (MySQL). ``strip_following_space`` also
    removes the whitespace after the last comment; without it that whitespace
    is kept. Text that opens with no comment is returned unchanged.
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
            close = text.find("*/", i + 2)
            if close < 0:
                break
            i = cut = close + 2
        else:
            break
    if cut == 0:
        return text
    rest = text[cut:]
    return rest.lstrip() if strip_following_space else rest


def strip_trailing_comments(text: str) -> str:
    """``text`` without the comments it ends with.

    Removes every trailing ``-- ...`` line comment and ``/* ... */`` block, and
    the whitespace around them, so ``SELECT 1 /* a */ -- b`` becomes
    ``SELECT 1``. A comment in the middle of the statement is kept. Text that
    ends with no comment is returned unchanged, trailing whitespace included.
    """
    n = len(text)
    i = code_end = 0
    comment_after_code = False
    # Once a search for */ fails, none follows, so later /* are not searched
    # again (that would rescan the rest of the text for each one).
    any_close_left = True
    while i < n:
        if text.startswith("--", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
            comment_after_code = True
        elif any_close_left and text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:  # unterminated: not a comment
                any_close_left = False
                continue
            i = close + 2
            comment_after_code = True
        elif text[i].isspace():
            i += 1
        else:
            i += 1
            code_end = i
            comment_after_code = False
    return text[:code_end] if comment_after_code else text
