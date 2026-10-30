"""Property tests for literal redaction (#1625, ``src.tools.sql.redact``).

Redaction is idempotent for every input: redacting redacted text changes nothing, so
a collector output read many times, or written and read back, keeps one text. A tag
read from redacted text is already redacted, so the contract and the read of a
stored output leave it as it is.
"""

from __future__ import annotations

import re

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from src.tools.sql.query_tags import statement_tags
from src.tools.sql.redact import redact_comment, redact_definition, redact_literals

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
