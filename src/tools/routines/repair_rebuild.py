# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Put a routine's literals back into a model's redacted repair, token by token (#1941).

The model repairs the collection's redacted ``CREATE`` (#1625): every string literal it
sees reads ``'?'`` (``X'00'`` for a hex, bit or byte string), and a few it sees as they
are (a string with no letter or digit, an alias, a date part). Its candidate keeps those
placeholders in the same count and quote form (``repair_screen``). The exact text, the
routines file's (ADR-034 Decision 4), is rebuilt here from the candidate by putting each
literal of the exact ``CREATE`` back in the place of the candidate's literal of the same
position, in the order the tokenizer reads them (:func:`literal_spans`, the walk of
``proof_screen.literal_sequence``: three levels of strings deep, a PostgreSQL routine's
own body read through). The text is never searched for ``'?'``: a ``'?'`` inside a
comment is no literal, and a source literal can itself read ``?``.

A routine is not rebuilt, and stays *needs attention* with the reason
(:class:`RebuildRefused`), when its literals cannot be put back by position alone:

- ``literal_reads_placeholder``: a literal of the exact text reads ``?`` itself;
- ``literal_partly_redacted``: the model saw a literal only in part (a pattern's shape,
  a statement inside a string redacted as SQL), or a literal's quote form differs;
- ``literal_holds_sql``: a redacted literal holds a statement the model never saw, or
  the routine builds a statement at run time while a literal is redacted;
- ``literal_count``: the exact text and the redacted one hold different numbers of
  literals;
- ``literal_in_quoted_text``: the candidate holds a placeholder inside a string whose
  quotes escape it, so its place in the text cannot be told;
- ``not_rebuilt``: a text does not tokenize, or the rebuilt text does not read back to
  the exact text's literals in order.

Pure: no I/O, no model. No reason holds routine text or a literal; the rebuilt text is
returned to the caller, which keeps it in memory only (it never goes into an artifact,
a log or a model input).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.tools.routines.proof_screen import dynamic_sql
from src.tools.sql.facade import RoutineToken, RoutineTokenError, routine_tokens

__all__ = [
    "REBUILD_WORDS",
    "Literal_",
    "RebuildCode",
    "RebuildRefused",
    "changed_lines",
    "literal_spans",
    "rebuild_exact",
    "rebuild_refusal",
]

RebuildCode = Literal[
    "literal_reads_placeholder",
    "literal_partly_redacted",
    "literal_holds_sql",
    "literal_count",
    "literal_in_quoted_text",
    "not_rebuilt",
]
#: Each code in words, for the artifact, the risk and the model's next attempt.
REBUILD_WORDS: dict[str, str] = {
    "literal_reads_placeholder": (
        "a literal of the routine reads '?' itself, so the redacted text cannot say which "
        "placeholder it is"
    ),
    "literal_partly_redacted": (
        "the model saw a literal of the routine only in part (a pattern's shape or a "
        "statement inside a string), so it cannot be put back by position"
    ),
    "literal_holds_sql": (
        "a redacted literal holds SQL the model never saw, or the routine builds a "
        "statement at run time from redacted literals"
    ),
    "literal_count": "the routine's exact text and its redacted text hold different literals",
    "literal_in_quoted_text": (
        "the candidate holds a placeholder inside a string whose quotes escape it, so its "
        "place in the text cannot be told"
    ),
    "not_rebuilt": "the candidate's text does not rebuild into the routine's exact literals",
}
_DEPTH = 3
#: The placeholder each redacted quote form reads (``redact.redact_definition``).
_PLACEHOLDER = "?"
_ZEROED = {"HEX_STRING", "BIT_STRING", "BYTE_STRING"}
#: The first word of a literal that is itself a statement.
_STATEMENT_WORDS = frozenset(
    {
        "SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "MERGE", "CREATE", "ALTER", "DROP",
        "TRUNCATE", "GRANT", "REVOKE", "SET", "RESET", "CALL", "DO", "EXECUTE", "PREPARE",
        "COPY", "LOCK", "VALUES", "TABLE", "REPLACE", "RENAME", "DECLARE", "BEGIN", "SHOW",
        "COMMENT", "NOTIFY", "LISTEN", "DISCARD", "VACUUM", "ANALYZE", "REFRESH", "PERFORM",
    }
)  # fmt: skip


class RebuildRefused(ValueError):
    """The exact text cannot be rebuilt by position: ``code`` and its words, no text."""

    def __init__(self, code: RebuildCode) -> None:
        super().__init__(REBUILD_WORDS[code])
        self.code: RebuildCode = code


@dataclass(frozen=True)
class Literal_:  # noqa: N801 -- "Literal" is typing's
    """One literal of a text, in the tokenizer's order: its token kind, its content as the
    tokenizer reads it, its raw text as written at its own level, and its span in the
    whole text (inclusive offsets), or None when it sits inside a string whose quotes
    escape it (its place in the whole text cannot be told)."""

    kind: str
    content: str
    raw: str
    span: tuple[int, int] | None


def _inner_base(level: str, tok: RoutineToken, previous: RoutineToken | None) -> int | None:
    """Where the text of ``tok``'s inner level starts in ``level``, when that text is
    written there as it is (a dollar-quoted string, a quoted string with no escape, a
    command's rest); None when its quotes escape it."""
    if tok.command_rest:
        after = previous.end + 1 if previous is not None else 0
        at = level.find(tok.text, after)
        if at < 0 or level[after:at].strip():
            return None
        return at
    raw = level[tok.start : tok.end + 1]
    if tok.kind == "HEREDOC_STRING":
        close = raw.find("$", 1)
        if close < 0:
            return None
        tag = raw[: close + 1]
        if raw.endswith(tag) and raw[len(tag) : len(raw) - len(tag)] == tok.text:
            return tok.start + len(tag)
        return None
    quote = min((i for i in (raw.find("'"), raw.find('"')) if i >= 0), default=-1)
    if quote < 0:
        return None
    if raw[quote + 1 : -1] == tok.text and len(raw) == quote + 2 + len(tok.text):
        return tok.start + quote + 1
    return None


def literal_spans(text: str, engine: str) -> list[Literal_]:
    """Every literal of ``text`` in the order ``proof_screen.literal_sequence`` reads them
    (the same walk: an introducer and each string, three levels of strings deep, a
    PostgreSQL routine's own body read through), each with its raw text and its span.
    Raises ``RoutineTokenError`` when ``text`` does not tokenize."""
    out: list[Literal_] = []

    def walk(level: str, base: int | None, depth: int, top: bool) -> None:
        tokens = list(routine_tokens(level, engine))
        previous: RoutineToken | None = None
        for tok in tokens:
            body = (
                top
                and engine == "postgresql"
                and previous is not None
                and previous.text.upper() == "AS"
            )
            literal = tok.kind.endswith("STRING") and not body and not tok.command_rest
            if tok.kind == "INTRODUCER" or literal:
                span = (
                    None if base is None or tok.command_rest else (base + tok.start, base + tok.end)
                )
                out.append(Literal_(tok.kind, tok.text, level[tok.start : tok.end + 1], span))
            if depth < _DEPTH and (tok.command_rest or tok.kind.endswith("STRING")):
                inner = _inner_base(level, tok, previous)
                start = len(out)
                try:
                    walk(tok.text, None if base is None or inner is None else base + inner,
                         depth + 1, False)  # fmt: skip
                except RoutineTokenError:
                    del out[start:]  # a string that is not SQL holds no literal
            previous = tok

    walk(text, 0, 0, True)
    return out


def _placeholder(seen: Literal_, exact: Literal_) -> bool:
    """Whether ``seen`` is the redacted form of a different ``exact`` literal."""
    if seen.content == _PLACEHOLDER:
        return True
    return seen.kind in _ZEROED and set(seen.content) <= {"0"} and seen.content != exact.content


def _holds_sql(content: str, engine: str) -> bool:
    try:
        tokens = routine_tokens(content, engine)
    except RoutineTokenError:
        return False
    return bool(tokens) and tokens[0].text.upper() in _STATEMENT_WORDS


def _pairs(exact: str, seen: str, engine: str) -> list[tuple[Literal_, Literal_, bool]]:
    """Each literal of ``exact`` with its ``seen`` (redacted) one and whether it was
    redacted. Raises :class:`RebuildRefused`."""
    try:
        mine, theirs = literal_spans(exact, engine), literal_spans(seen, engine)
    except RoutineTokenError:
        raise RebuildRefused("not_rebuilt") from None
    if len(mine) != len(theirs):
        raise RebuildRefused("literal_count")
    out: list[tuple[Literal_, Literal_, bool]] = []
    for e, s in zip(mine, theirs, strict=True):
        if e.kind != s.kind:
            raise RebuildRefused("literal_partly_redacted")
        if e.kind != "INTRODUCER" and e.content == _PLACEHOLDER:
            raise RebuildRefused("literal_reads_placeholder")
        if e.content == s.content:
            out.append((e, s, False))
            continue
        if not _placeholder(s, e):
            raise RebuildRefused("literal_partly_redacted")
        if _holds_sql(e.content, engine):
            raise RebuildRefused("literal_holds_sql")
        out.append((e, s, True))
    if any(redacted for _, _, redacted in out) and dynamic_sql([exact], engine):
        raise RebuildRefused("literal_holds_sql")
    return out


def rebuild_refusal(exact: str, seen: str, engine: str) -> RebuildCode | None:
    """Why the routine's exact text could not be rebuilt from a repair of ``seen`` (the
    redacted text the model is given), whatever the repair: None when it can. Asked
    before the model is, so a routine that cannot be rebuilt is never sent."""
    try:
        _pairs(exact, seen, engine)
    except RebuildRefused as exc:
        return exc.code
    return None


def _shape(text: str, engine: str) -> list[str]:
    return [t.kind for t in routine_tokens(text, engine)]


def rebuild_exact(exact: str, seen: str, candidate: str, engine: str) -> str:
    """The exact text of the repaired routine: ``candidate`` (a repair of ``seen``) with
    each redacted literal replaced, by position, by the literal of ``exact`` it stands for
    (module docstring). Raises :class:`RebuildRefused` with the reason."""
    pairs = _pairs(exact, seen, engine)
    try:
        theirs = literal_spans(candidate, engine)
    except RoutineTokenError:
        raise RebuildRefused("not_rebuilt") from None
    if len(theirs) != len(pairs):
        raise RebuildRefused("literal_count")
    edits: list[tuple[tuple[int, int], str]] = []
    for (e, s, redacted), c in zip(pairs, theirs, strict=True):
        if c.kind != s.kind or c.content != s.content:
            raise RebuildRefused("not_rebuilt")
        if not redacted:
            continue
        if c.span is None:
            raise RebuildRefused("literal_in_quoted_text")
        edits.append((c.span, e.raw))
    out = candidate
    spans = sorted(edits, key=lambda x: x[0][0], reverse=True)
    for (a, _b), (c_, _d) in zip(spans, spans[1:], strict=False):
        if c_[1] >= a[0]:
            raise RebuildRefused("literal_in_quoted_text")  # overlapping places
    for (start, end), raw in spans:
        out = out[:start] + raw + out[end + 1 :]
    _verify(out, exact, candidate, engine)
    return out


def _verify(rebuilt: str, exact: str, candidate: str, engine: str) -> None:
    """The rebuilt text reads back to the exact text's literals, in order, and to the
    candidate's tokens otherwise."""
    try:
        got = [(x.kind, x.content) for x in literal_spans(rebuilt, engine)]
        want = [(x.kind, x.content) for x in literal_spans(exact, engine)]
        same_shape = _shape(rebuilt, engine) == _shape(candidate, engine)
    except RoutineTokenError:
        raise RebuildRefused("not_rebuilt") from None
    if got != want or not same_shape:
        raise RebuildRefused("not_rebuilt")


def changed_lines(before: str, after: str) -> Sequence[str]:
    """A unified diff of two redacted texts (the routine's and the model's candidate),
    with no context lines: what the model changed, in the text the model saw."""
    import difflib

    return [
        line.rstrip("\n")
        for line in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            "routine",
            "repair",
            n=0,
        )
    ]
