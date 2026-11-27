# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A credential value ends where the text around it says, or the file is withheld (#2260).

The key-value rule ended an unquoted value at a quote or backslash before ``,;:)]}``, a
backtick or whitespace, and a quoted one at the first undoubled quote of its own kind,
so the tail of a value holding one shipped. A quote now ends a value only when it closes
a string opened before the key (``conn("password=...")``) or the value's own quote; a
literal line-break escape ends one only inside a string. Where the scan cannot tell (a
quote run before whitespace with no string open), the redaction is incomplete and the
file is withheld. Every value here is synthetic and built at run time.
"""

from __future__ import annotations

import json

import pytest

from ci.llm import private_scan
from src.shared.secret_scan import credential_spans, secret_findings
from src.tools.sql.redact import scrub_comment

_HEAD = "Zq9"
_TAIL = "Xv7" + "Wm3Kt5"  # pragma: allowlist secret
_MARK = "[REDACTED:password-value]"


def _redact(text: str) -> private_scan.Redaction:
    return private_scan.redact(text, narrow_key_names=True)


# Unquoted values holding a quote or a backslash, with no string open around the key.
_UNQUOTED = [
    pytest.param(_HEAD + '",' + _TAIL, id="quote-comma"),
    pytest.param(_HEAD + '";' + _TAIL, id="quote-semicolon"),
    pytest.param(_HEAD + '":' + _TAIL, id="quote-colon"),
    pytest.param(_HEAD + '")' + _TAIL, id="quote-paren"),
    pytest.param(_HEAD + "'}" + _TAIL, id="apostrophe-brace"),
    pytest.param(_HEAD + '"`' + _TAIL, id="quote-backtick"),
    pytest.param(_HEAD + "\\n" + _TAIL, id="literal-backslash-n"),
    pytest.param(_HEAD + "\\r" + _TAIL, id="literal-backslash-r"),
    pytest.param(_HEAD + "\\u000a" + _TAIL, id="literal-backslash-u000a"),
    pytest.param(_HEAD + "\\" + '",' + _TAIL, id="escaped-quote-comma"),
]


@pytest.mark.parametrize("value", _UNQUOTED)
@pytest.mark.parametrize("line", ["password = {v}", "password: {v}", "DB_PASSWORD={v}"])
def test_an_unquoted_value_with_a_quote_before_a_delimiter_is_redacted_whole(
    line: str, value: str
) -> None:
    text = line.format(v=value)
    out = _redact(text)
    assert out.text == line.format(v=_MARK)
    assert out.complete
    (span,) = credential_spans(text)
    assert text[span[0] : span[1]] == value
    assert scrub_comment(text) == line.format(v="?")


# Quoted values holding a quote of their own kind (undoubled) or of the other kind.
_QUOTED = [
    pytest.param("'", _HEAD + "'" + _TAIL, id="same-kind-quote"),
    pytest.param('"', _HEAD + '"' + _TAIL, id="same-kind-double-quote"),
    pytest.param('"', _HEAD + "'," + _TAIL, id="other-kind-comma"),
    pytest.param("'", _HEAD + '",' + _TAIL, id="other-kind-comma-single"),
    pytest.param('"', _HEAD + "'}" + _TAIL, id="other-kind-brace"),
    pytest.param('"', _HEAD + "':" + _TAIL, id="other-kind-colon"),
    pytest.param('"', _HEAD + "']" + _TAIL, id="other-kind-bracket"),
    pytest.param('"', _HEAD + "'", id="other-kind-at-the-end", marks=pytest.mark.passes_on_main),
]


@pytest.mark.parametrize("quote,value", _QUOTED)
@pytest.mark.parametrize("line", ["password = {q}{v}{q}", "password={q}{v}{q}"])
def test_a_quoted_value_holding_a_quote_is_redacted_whole(
    line: str, quote: str, value: str
) -> None:
    text = line.format(q=quote, v=value)
    out = _redact(text)
    assert out.text == line.format(q=quote, v=_MARK)
    assert out.complete
    assert scrub_comment(text) == line.format(q=quote, v="?")


@pytest.mark.parametrize(
    "text",
    [
        "password = " + _HEAD + '" ' + _TAIL,
        "password = " + _HEAD + "' " + _TAIL,
        "password: " + _HEAD + "\\ " + _TAIL,
    ],
)
def test_a_value_whose_end_the_scan_cannot_tell_withholds_the_file(text: str) -> None:
    """A quote or backslash before whitespace, with no string open around the key,
    may end the value or be part of it: the redaction is incomplete, so the file is
    withheld, and comment masking masks to the end of the line."""
    out = _redact(text)
    assert not out.complete and not out.redactable
    assert _HEAD not in out.text
    assert secret_findings(text) == ["password value at line 1"]
    assert _TAIL not in scrub_comment(text) and _HEAD not in scrub_comment(text)


# Main already redacts these and keeps the text around them: guards that the union does too.
_ON_MAIN = pytest.mark.passes_on_main


@pytest.mark.passes_on_main  # main redacts these the same way; guards that the union keeps it
@pytest.mark.parametrize(
    "text,expected",
    [
        # A string around the key ends the value: the string and the JSON stay whole.
        pytest.param(
            '{"a": "password=' + _TAIL + '", "b": 1}',
            '{"a": "password=' + _MARK + '", "b": 1}',
            marks=_ON_MAIN,
        ),
        pytest.param(
            'conn("password=' + _TAIL + '")', 'conn("password=' + _MARK + '")', marks=_ON_MAIN
        ),
        pytest.param(
            "x = 'password=" + _TAIL + "' + y", "x = 'password=" + _MARK + "' + y", marks=_ON_MAIN
        ),
        # A quoted value followed by SQL.
        pytest.param(
            "WHERE password = '" + _TAIL + "' AND user = 'app'",
            "WHERE password = '" + _MARK + "' AND user = 'app'",
            marks=_ON_MAIN,
        ),
        pytest.param("password='" + _TAIL + "'", "password='" + _MARK + "'", marks=_ON_MAIN),
        # A line-break escape inside a string ends the value.
        pytest.param(
            json.dumps({"t": "DB_PASSWORD=" + _TAIL + "\nDB_USER=app"}),
            json.dumps({"t": "DB_PASSWORD=" + _MARK + "\nDB_USER=app"}),
            marks=_ON_MAIN,
        ),
    ],
)
def test_an_ordinary_string_or_clause_is_redacted_and_kept(text: str, expected: str) -> None:
    out = _redact(text)
    assert out.text == expected
    assert out.complete


def test_an_apostrophe_in_prose_opens_no_string() -> None:
    """``don't`` holds no quote that opens a string, so the quote in the value after
    it is part of the value."""
    text = "don't use password = " + _HEAD + "'," + _TAIL + " here"
    assert _redact(text).text == "don't use password = " + _MARK + " here"


@pytest.mark.passes_on_main  # main ends the value at the backtick too
def test_a_value_in_a_markdown_code_span_ends_at_the_closing_backtick() -> None:
    text = "Set `password = " + _HEAD + '"' + _TAIL + "` first"
    out = _redact(text)
    assert out.text == "Set `password = " + _MARK + "` first" and out.complete


def test_a_json_string_inside_a_json_string_keeps_both_whole() -> None:
    inner = json.dumps({"cmd": "mysql --password=" + _HEAD + '",' + _TAIL, "n": 1})
    text = json.dumps({"input": inner})
    out = _redact(text)
    assert out.complete
    assert json.loads(json.loads(out.text)["input"]) == {"cmd": "mysql --password=" + _MARK, "n": 1}


# -- #2595 review: a quote after a letter opens no string ---------------------------------


@pytest.mark.passes_on_main  # main never had this regression of this PR's first head
@pytest.mark.parametrize(
    "before,after",
    [
        pytest.param("-- the admins' ", after, id="possessive-" + after)
        for after in ("'.", "'+", "'=", "'|")
    ]
    + [
        pytest.param("the users' and ", "'.", id="plural-possessive"),
        pytest.param('-- 5" display; ', '".', id="inch-mark"),
    ],
)
def test_a_quote_after_a_letter_or_a_digit_opens_no_string_around_the_key(
    before: str, after: str
) -> None:
    """``admins'`` or ``5"`` before the key starts no string, so a quote in the value
    after it is part of the value, not the end of a string around the key."""
    text = before + "password=" + _HEAD + after + _TAIL
    for line in (text, json.dumps({"text": text})):
        out = _redact(line)
        assert _TAIL not in out.text, line
        assert _TAIL not in scrub_comment(line), line


@pytest.mark.passes_on_main  # main redacts this the same way
def test_an_apostrophe_inside_an_open_string_still_ends_nothing() -> None:
    text = "x = 'it''s the password=" + _HEAD + "' + y"
    assert _redact(text).text == "x = 'it''s the password=" + _MARK + "' + y"


def test_a_string_that_opens_at_the_start_of_the_line_closes_before_the_key() -> None:
    """``'x'`` at the start of a line opens and closes a string, so no string is open
    around the key and the quote in the value is part of it (#2595)."""
    text = "'x' , password = " + _HEAD + "':" + _TAIL
    assert _redact(text).text == "'x' , password = " + _MARK
    assert scrub_comment(text) == "'x' , password = ?"
