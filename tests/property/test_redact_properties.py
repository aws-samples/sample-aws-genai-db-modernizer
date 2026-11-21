# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property tests for literal redaction (#1625, ``src.tools.sql.redact``).

Redaction is idempotent for every input: redacting redacted text changes nothing, so
a collector output read many times, or written and read back, keeps one text. A tag
read from redacted text is already redacted, so the contract and the read of a
stored output leave it as it is.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from src.shared.sql_spans import SpanSettings, literals_and_comments_for, span_dialect, spans
from src.tools.sql.query_tags import statement_tags
from src.tools.sql.redact import redact_comment, redact_definition, redact_literals
from tests.hypothesis_examples import scaled

_PIECES = list("ab1 2'\"%_$-/*#\n()[],.@:?=xNXq{}\\^~|+0eE;") + [
    "LIKE ",
    "TOP ",
    "DATE ",
    "$$",
    "$a$",
    "/* ",
    " */",
    "-- ",
    "q'[",
    "]'",
    "N'",
    "X'",
    "0x1F",
    "0x1F.1",
    "1.5e3.2",
    "0b101",
    "0b101.1",
    "b'",
    "EXEC ",
    "ORDER BY ",
    " AS ",
    "COUNT(",
    "SELECT ",
    "date_trunc(",
    "jane@example.com",
    "4111111111111111",
    "user='jane%40example.com'",
    "controller='cart',",
]
_TEXT = st.lists(st.sampled_from(_PIECES), max_size=40).map("".join)
_ENGINE = st.sampled_from(["mysql", "postgresql", "sqlserver", "oracle", "db2", ""])
_SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))


@_SETTINGS
@given(_TEXT, _ENGINE)
@example("SELECT 0x1F.1", "mysql")
@example("SELECT 1.5e3.2", "mysql")
@example("SELECT 0x1F.1e3", "sqlserver")
@example("$1.5e3.2", "sqlserver")
@example("1ORDER BY ORDER BY 1", "postgresql")
@example("ORDER BY 0x1F, 2", "mysql")
@example("ORDER BY 0x1F,4111TOP", "sqlserver")
@example("$1.5x", "sqlserver")
@example("$1.5e3x", "sqlserver")
@example("1EXEC X'a'", "postgresql")
def test_redaction_is_idempotent(text: str, engine: str) -> None:
    for redact in (redact_literals, redact_definition, redact_comment):
        once = redact(text, engine)
        assert redact(once, engine) == once


@_SETTINGS
@given(_TEXT, _ENGINE)
def test_tags_of_redacted_text_are_already_redacted(text: str, engine: str) -> None:
    for tag in statement_tags(redact_literals(text, engine), engine):
        assert redact_comment(tag, engine) == tag


_DIGITS = st.text("0123456789", min_size=4, max_size=16)
_STRICT_FORMS = st.one_of(
    _DIGITS,
    st.builds("{}.{}".format, _DIGITS, _DIGITS),
    st.builds("{}e{}".format, _DIGITS, _DIGITS),
    st.builds("{}e-{}.{}".format, _DIGITS, _DIGITS, _DIGITS),
    st.builds("0x{}".format, _DIGITS),
    st.builds("0x{}.{}".format, _DIGITS, _DIGITS),
    st.builds("0b{}".format, st.text("01", min_size=4, max_size=16)),
    st.builds("0b{}.{}".format, st.text("01", min_size=4, max_size=16), _DIGITS),
)
# Everything but MySQL also reads separators, octal and a number glued to a word.
_GLUING_FORMS = st.one_of(
    _STRICT_FORMS,
    st.builds("{}_{}".format, _DIGITS, _DIGITS),
    st.builds("{}_{}_{}.{}".format, _DIGITS, _DIGITS, _DIGITS, _DIGITS),
    st.builds("{}e{}_{}".format, _DIGITS, _DIGITS, _DIGITS),
    st.builds("0x{}_{}".format, _DIGITS, _DIGITS),
    st.builds("0o{}".format, st.text("01234567", min_size=4, max_size=16)),
    st.builds("0o{}_{}".format, _DIGITS, _DIGITS),
    st.builds("{}abc".format, _DIGITS),
    st.builds("{}.{}x".format, _DIGITS, _DIGITS),
    st.builds("0x{}.{}abc".format, _DIGITS, _DIGITS),
)
# SQL Server's money literal: only there is ``$1234`` a number and not a parameter.
_MONEY_FORMS = st.one_of(
    st.builds("${}".format, _DIGITS),
    st.builds("${}.{}".format, _DIGITS, _DIGITS),
    st.builds("${}e{}".format, _DIGITS, _DIGITS),
    st.builds("${}_{}".format, _DIGITS, _DIGITS),
    st.builds("$ {}".format, _DIGITS),
)
_TEMPLATES = st.sampled_from(
    [
        "SELECT {} FROM t",
        "SELECT a FROM t WHERE c = {}",
        "SELECT f({}, 1)",
        "SELECT a FROM t ORDER BY {}, 2",
    ]
)
# What can stand right after a literal: nothing, a space, a word, ``$`` or ``#``.
_AFTER = st.sampled_from(["", " ", ",", "x", "abc", "TOP", "ORDER BY a", "$", "#", "$1", "#x"])


@_SETTINGS
@given(
    st.data(),
    _TEMPLATES,
    st.sampled_from(["mysql", "postgresql", "sqlserver", "oracle", "db2", ""]),
    _AFTER,
)
@example(None, "WHERE c = {}", "postgresql", "")
@example(None, "SELECT {} FROM t", "sqlserver", "abc")
@example(None, "SELECT {} FROM t", "oracle", "#")
def test_no_digit_run_of_a_number_literal_survives(
    data, template: str, engine: str, after: str
) -> None:
    forms = _STRICT_FORMS if engine == "mysql" else _GLUING_FORMS
    if engine == "sqlserver":
        forms = st.one_of(forms, _MONEY_FORMS)
    literal = data.draw(forms) if data is not None else "4111_1111_1111_1111"
    if after[:1] in "$#" and after and "." not in literal:
        return  # a number right against a ``$`` or ``#`` is kept whole, as it always was
    if engine == "mysql" and (after[:1].isalpha() or "_" in literal):
        return  # a name on MySQL, not a number
    if "ORDER BY" in template and re.fullmatch(r"\d{1,9}", literal):
        return  # a position, kept by design
    out = redact_literals(template.format(literal + after), engine)
    runs = re.findall(r"\d{4,}", literal)
    if after[:1] in "$#" and after:
        runs = runs[:1]  # the number before the ``.`` (the shorter reading main had)
    text = template.format(literal + after)
    for run in runs:
        if after[:1] in "$#" and after:
            assert out.count(run) < text.count(run), (literal, after, out)
        else:
            assert run not in out, (literal, after, out)
    assert redact_literals(out, engine) == out


# -- no reading of a setting is trusted to mask less (#1482, #2331) --------------------
#
# Where a backslash escapes a quote depends on a setting (MySQL's NO_BACKSLASH_ESCAPES,
# PostgreSQL's standard_conforming_strings) that the statement may have run under with
# another value. Redaction reads the text both ways and masks every character either
# reading puts in a string: a run the readings split differently is one value, nothing
# of it kept. The expectation below is built from the tokenizer alone
# (``src.shared.sql_spans``), never from the redaction: it models that rule, and a
# position it names must be masked whatever the redaction decides about the text (cut
# or whole, a definition or a query, any reported setting).

_DEFAULT = SpanSettings()
_READINGS = {
    "mysql": (_DEFAULT, SpanSettings(no_backslash_escapes=True)),
    "postgresql": (_DEFAULT, SpanSettings(standard_conforming_strings=False)),
    "oracle": (_DEFAULT,),
    "sqlserver": (_DEFAULT,),
}
_REPORTED = {
    "mysql": _READINGS["mysql"]
    + (
        SpanSettings(ansi_quotes=True),
        SpanSettings(no_backslash_escapes=True, ansi_quotes=True),
    ),
    "postgresql": _READINGS["postgresql"],
    "oracle": _READINGS["oracle"],
    "sqlserver": _READINGS["sqlserver"],
}
_DOLLAR_RE = re.compile(r"(?<![\w$])\$(?:[^\W\d]\w*)?\$")
# Redaction keeps a string after ``AS`` (a column alias) and redacts a dollar quote after
# ``AS`` or ``DO`` (a routine body) as SQL. Read loosely here (a string prefix or an
# introducer may stand between), so the expectation names fewer positions, never more.
_ALIAS_BEFORE_RE = re.compile(r"AS\s*(?:[A-Z]{1,2}|_\w+)?$", re.IGNORECASE)
_BODY_BEFORE_RE = re.compile(r"(?:DO|AS)\s*(?:LANGUAGE\s+\w+\s+)?$", re.IGNORECASE)
# Inside a body a string that is itself a statement is code: any of these words in a
# string lets it go unchecked.
_STATEMENT_WORD_RE = re.compile(
    r"\b(?:select|insert|update|delete|with|merge|call|create|alter|drop|values|"
    r"replace|truncate|grant|revoke|set|begin|declare|exec|execute|lock|show|explain|"
    r"do|copy|perform|return|raise|if|for|loop|end)\b",
    re.IGNORECASE,
)


def _one_reading(text: str, engine: str, reading: SpanSettings) -> list[tuple[int, int, str]]:
    """The spans one reading finds, a quote, ``/*`` or dollar quote it leaves open
    running to the end of the text (a text cut there)."""
    grammar = literals_and_comments_for(engine, reading)
    found = list(spans(text, grammar))
    code = list(text)
    for start, end, _opener in found:
        code[start:end] = " " * (end - start)
    blanked = "".join(code)
    quotes = "'\"" if span_dialect(engine) == "mysql" else "'"
    starts = [i for i, ch in enumerate(blanked) if ch in quotes][:1]
    if "/*" in blanked:
        starts.append(blanked.find("/*"))
    dollar = _DOLLAR_RE.search(blanked) if span_dialect(engine) == "postgresql" else None
    if dollar is not None:
        starts.append(dollar.start())
    if not starts:
        return found
    cut = min(starts)
    if dollar is not None and cut == dollar.start():
        opener = dollar.group()
    else:
        opener = "/*" if blanked.startswith("/*", cut) else blanked[cut]
    return [s for s in found if s[1] <= cut] + [(cut, len(text), opener)]


def _hidden_runs(
    text: str, engine: str
) -> tuple[list[list[int]], list[dict[tuple[int, int], str]]]:
    """Each maximal run of characters some reading puts in a string or a comment, and
    each reading's own spans (quoted names left out)."""
    keyed: list[dict[tuple[int, int], str]] = []
    hidden: list[tuple[int, int]] = []
    for reading in _READINGS[span_dialect(engine)]:
        identifiers = literals_and_comments_for(engine, reading).identifiers
        mine = {
            (s, e): o for s, e, o in _one_reading(text, engine, reading) if o not in identifiers
        }
        keyed.append(mine)
        hidden.extend(mine)
    runs: list[list[int]] = []
    for start, end in sorted(hidden):
        if runs and start < runs[-1][1]:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    return runs, keyed


def _both_readings_mask(text: str, engine: str, *, body: bool = False) -> set[int]:
    """The positions of ``text`` the both-readings rule masks: every run of characters
    some reading hides that the readings split differently, and a string both readings
    read the same way that is not kept (an alias, a body)."""
    runs, keyed = _hidden_runs(text, engine)
    grammars = [literals_and_comments_for(engine, r) for r in _READINGS[span_dialect(engine)]]
    code = list(text)
    for start, end in runs:
        code[start:end] = " " * (end - start)
    for start, end, _opener in spans(text, grammars[0]):
        code[start:end] = " " * (end - start)
    code_text = "".join(code)
    masked: set[int] = set()
    for start, end in runs:
        openers = {mine.get((start, end)) for mine in keyed}
        if len(openers) != 1 or None in openers:
            masked.update(range(start, end))  # the readings split it: one value
            continue
        opener = openers.pop()
        assert opener is not None
        if opener in ("--", "#") or opener.startswith("/*"):
            continue  # a comment both readings agree on: its own rules (redact_comment)
        if opener.startswith("$"):
            if _BODY_BEFORE_RE.search(code_text[:start]):
                inner_start = start + len(opener)
                closed = end - start >= 2 * len(opener) and text.endswith(opener, 0, end)
                inner_end = end - len(opener) if closed else end
                inner = _both_readings_mask(text[inner_start:inner_end], engine, body=True)
                masked.update(inner_start + p for p in inner)
            else:
                masked.update(range(start, end))
            continue
        if _ALIAS_BEFORE_RE.search(code_text[:start]):
            continue
        if body and _STATEMENT_WORD_RE.search(text[start:end]):
            continue
        masked.update(range(start, end))
    return masked


_SECRET = "\x01"
_SOUP = [
    "'",
    "\\",
    "\\'",
    "''",
    '"',
    '\\"',
    " ",
    _SECRET,
    "x",
    " = ",
    ")",
    "(",
    ",",
    " AS ",
    "-- ",
    "#",
    "/*",
    "*/",
    "\n",
    "E'",
    "N'",
    "_utf8mb4'",
    "$$",
    "$f$",
    "q'[",
    "]'",
    "[",
    "]",
]
_LITERAL_CONTENT = st.lists(
    st.sampled_from(["a", " ", "'", '"', "\\", ";", ")", "x", _SECRET]), max_size=8
).map("".join)


@st.composite
def _literal(draw) -> str:
    """A string literal: escaped the MySQL way (``\\'``), doubled, or not at all."""
    content: str = draw(_LITERAL_CONTENT)
    quote: str = draw(st.sampled_from(["'", '"']))
    escape: str = draw(st.sampled_from(["backslash", "doubled", "raw"]))
    if escape == "backslash":
        content = content.replace("\\", "\\\\").replace(quote, "\\" + quote)
    elif escape == "doubled":
        content = content.replace(quote, quote * 2)
    prefix: str = draw(st.sampled_from(["", "", " AS ", "_utf8mb4", "N", "E"]))
    return prefix + quote + content + quote


_PIECE = st.one_of(
    _literal(),
    st.sampled_from([" = ", ") and k = 2", " and ", ", ", _SECRET, "(", " -- c\n", "\n"]),
)
# A text cut at some point (a length cap), or whole.
_STRUCTURED = st.tuples(st.lists(_PIECE, max_size=8).map("".join), st.floats(0, 2)).map(
    lambda pair: pair[0][: round(len(pair[0]) * pair[1])] if pair[1] < 1 else pair[0]
)
_BODY = st.one_of(
    st.lists(st.sampled_from(_SOUP), max_size=40).map("".join),
    _STRUCTURED,
)
_PREFIXES = st.sampled_from(
    [
        "SELECT a FROM t WHERE b = ",
        "select a from t where (t.n <> ",
        "CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $function$ BEGIN RETURN ",
    ]
)


def _with_secrets(text: str) -> tuple[str, list[tuple[str, int]]]:
    """``text`` with each secret mark replaced by its own word, and where each is."""
    out = []
    marks = []
    length = 0
    for n, piece in enumerate(text.split(_SECRET)):
        if n:
            word = "Zq" + "".join(chr(97 + (n // 26**k) % 26) for k in (2, 1, 0))
            marks.append((word, length + 1))
            piece = " " + word + " " + piece
        out.append(piece)
        length += len(piece)
    return "".join(out), marks


@pytest.mark.passes_on_main
@settings(max_examples=scaled(2000), deadline=None, suppress_health_check=list(HealthCheck))
@given(
    st.sampled_from(["mysql", "mariadb", "postgresql", "oracle", "sqlserver"]),
    st.integers(0, 3),
    _PREFIXES,
    _BODY,
)
# The ways a one-reading shortcut showed a value this rule masks (#2539 review): a text cut
# after an escaped quote, an unterminated E'' in a body, an alias holding escaped quotes.
@example("mysql", 0, "select a from t where b = ", "'\\'\x01 and c = 1")
@example(
    "postgresql",
    0,
    "CREATE FUNCTION f() RETURNS text LANGUAGE plpgsql AS $function$ BEGIN RETURN ",
    "E'\\'\x01 Q 4111; END $function$",
)
@example("mysql", 1, "select a ", "AS 'p\\'q\\'\x01\\'r\\'' from t")
@example("postgresql", 1, "select a ", "AS 'p\\'q\\'\x01\\'r\\'' from t")
@example("mysql", 0, "select a from t where (t.n <> ", "'it\\'s; x') and n = '\x01'")
def test_every_value_either_reading_hides_stays_masked(
    engine: str, reported: int, prefix: str, body: str
) -> None:
    """Whatever the reported setting, the mode, and whether the text is whole or cut,
    every value the both-readings rule masks is masked."""
    text, marks = _with_secrets(prefix + body)
    choices = _REPORTED[span_dialect(engine)]
    setting = choices[reported % len(choices)]
    must = _both_readings_mask(text, engine)
    # Code every reading leaves outside every string and comment is kept: the SQL after
    # a literal survives where both readings agree it is code (#2331).
    runs, _keyed = _hidden_runs(text, engine)
    code = [at for _word, at in marks if not any(s <= at < e for s, e in runs)]
    for redact in (redact_literals, redact_definition):
        out = redact(text, engine, setting)
        for word, at in marks:
            if at in must:
                assert word not in out, (redact.__name__, text, out, word)
            elif at in code:
                assert word in out, (redact.__name__, text, out, word)
