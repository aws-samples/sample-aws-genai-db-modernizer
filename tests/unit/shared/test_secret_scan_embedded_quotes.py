# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A credential value holding a quote or a backslash is redacted whole (#2247).

The key-value rule ended an unquoted value at its first ``"``, ``'`` or ``\\``, and a
quoted one at the first quote of either kind, so ``password = Zq9"Xv7Wm3Kt5`` was
redacted to ``password = [REDACTED:password-value]"Xv7Wm3Kt5`` and the tail shipped.
Now the span runs to the end of the value: an unquoted one up to whitespace (a quote or
backslash run inside it is part of it unless it closes an enclosing string), a quoted
one up to its own closing quote at its own escape level. What counts as a finding does
not change. Every value here is synthetic, built at run time.
"""

from __future__ import annotations

import json

import pytest

from ci.llm import private_scan
from src.shared.secret_scan import credential_spans, secret_findings, secret_kinds
from src.tools.sql.redact import redact_definition, scrub_comment

_HEAD = "Zq9"
_TAIL = "Xv7" + "Wm3Kt5"  # pragma: allowlist secret
_MARK = "[REDACTED:password-value]"

# One value per embedded character, and one with all of them.
_EMBEDDED = [
    pytest.param(_HEAD + '"' + _TAIL, id="double-quote"),
    pytest.param(_HEAD + "'" + _TAIL, id="single-quote"),
    pytest.param(_HEAD + "\\" + _TAIL, id="backslash"),
    pytest.param(_HEAD + '"' + _TAIL[:2] + "'" + _TAIL[2:4] + "\\" + _TAIL[4:], id="mixed"),
]


def _redacted(text: str) -> str:
    return private_scan.redact(text, narrow_key_names=True).text


@pytest.mark.parametrize("value", _EMBEDDED)
@pytest.mark.parametrize("line", ["password = {v}", "password: {v}", "db_password={v}"])
def test_an_unquoted_value_with_an_embedded_quote_or_backslash_is_redacted_whole(
    line: str, value: str
) -> None:
    text = line.format(v=value)
    assert _redacted(text) == line.format(v=_MARK)
    (span,) = credential_spans(text)
    assert text[span[0] : span[1]] == value
    # Still one finding, as before.
    assert secret_findings(text) == ["password value at line 1"]


@pytest.mark.parametrize("value", _EMBEDDED)
def test_the_recorded_value_is_the_whole_value_and_nothing_is_cut(value: str) -> None:
    result = private_scan.redact("password = " + value + "\n", narrow_key_names=True)
    assert [item[-1] for item in result.pending] == [value]
    assert result.cut == set()


@pytest.mark.parametrize("value", _EMBEDDED)
def test_a_dotenv_line_is_redacted_whole(value: str) -> None:
    text = "APP_ENV=prod\nDB_PASSWORD={v}\nexport API_SECRET={v}\n"
    assert _redacted(text.format(v=value)) == text.format(v=_MARK)


def test_a_json_value_with_an_escaped_quote_or_backslash_is_redacted_whole() -> None:
    for value in (_HEAD + '"' + _TAIL, _HEAD + "\\" + _TAIL, _HEAD + "\\" + '"' + _TAIL):
        text = json.dumps({"password": value, "user": "app"})
        out = _redacted(text)
        assert json.loads(out) == {"password": _MARK, "user": "app"}
        # The same JSON inside a JSON string (a transcript line), one level deeper.
        nested = json.dumps({"input": text})
        assert json.loads(json.loads(_redacted(nested))["input"]) == {
            "password": _MARK,
            "user": "app",
        }


def test_a_quoted_value_holding_the_other_quote_or_a_doubled_one_is_redacted_whole() -> None:
    assert _redacted("password: '" + _HEAD + '"' + _TAIL + "'") == "password: '" + _MARK + "'"
    assert _redacted('password = "' + _HEAD + "'" + _TAIL + '"') == 'password = "' + _MARK + '"'
    # SQL doubles a quote inside a string literal.
    sql = "SET password = '" + _HEAD + "''" + _TAIL + "';"
    assert _redacted(sql) == "SET password = '" + _MARK + "';"


def test_a_json_value_ending_in_an_escaped_backslash_keeps_the_json_valid() -> None:
    text = json.dumps({"password": _HEAD + "\\", "user": "app"})
    assert json.loads(_redacted(text)) == {"password": _MARK, "user": "app"}


def test_a_value_in_backticks_with_an_embedded_quote_is_redacted_up_to_the_backtick() -> None:
    text = "`password`=`" + _HEAD + '"' + _TAIL + "`"
    assert _redacted(text) == "`password`=`" + _MARK + "`"


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "text,expected",
    [
        # A quote that closes an enclosing string ends the value, so the string, the
        # JSON and the call stay whole.
        ('conn("password=' + _TAIL + '")', 'conn("password=' + _MARK + '")'),
        (
            '{"a":"password=' + _TAIL + '","b":"x y"}',
            '{"a":"password=' + _MARK + '","b":"x y"}',
        ),
        (
            json.dumps({"cmd": "mysql --password=" + _TAIL}),
            json.dumps({"cmd": "mysql --password=" + _MARK}),
        ),
        ("x = 'password=" + _TAIL + "' + y", "x = 'password=" + _MARK + "' + y"),
    ],
)
def test_a_quote_that_closes_an_enclosing_string_still_ends_the_value(
    text: str, expected: str
) -> None:
    assert _redacted(text) == expected


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "text",
    [
        # Prose under a token name stays no finding in the CI scans (#2179).
        "Validate the token: isn't stored, only token_hash",
        # A placeholder stays allowed, whatever follows its quote.
        'password = <password>"s',
        # A bind parameter stays one.
        'WHERE password = ?"',
    ],
)
def test_what_counts_as_a_finding_does_not_change(text: str) -> None:
    assert secret_findings(text, narrow_key_names=True) == []
    assert _redacted(text) == text


# A quoted value whose own closing quote is missing inside the JSON string around it:
# the value ends where the rule ended it before, never past that string (#2259 review).
# Main kept these whole; the guard keeps the wider span from breaking them.
_UNCLOSED_IN_JSON = [
    pytest.param(
        '{"sql":"password = \'abc","note":"it\'s fine"}',
        '{"sql":"password = \'[REDACTED:password-value]","note":"it\'s fine"}',
        id="other-kind-member-kept",
    ),
    pytest.param(
        '{"sql":"password = \'abc","n":{"x":"it\'s"}}',
        '{"sql":"password = \'[REDACTED:password-value]","n":{"x":"it\'s"}}',
        id="other-kind-nested-object",
    ),
    pytest.param(
        '{"role":"assistant","content":"the password: \'Zq9 was cut","next":[{"t":"don\'t"}]}',
        '{"role":"assistant","content":"the password: \'[REDACTED:password-value]",'
        '"next":[{"t":"don\'t"}]}',
        id="transcript-line",
    ),
    pytest.param(
        '{"a":"password=\\"Zq9","b":"x\\"y"}',
        '{"a":"password=\\"[REDACTED:password-value]","b":"x\\"y"}',
        id="lower-escape-level",
    ),
    pytest.param(
        '{"a":"password=\\"Zq9\\\\","b":"x\\"y"}',
        '{"a":"password=\\"[REDACTED:password-value]\\\\","b":"x\\"y"}',
        id="lower-escape-level-after-a-backslash",
    ),
]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("text,expected", _UNCLOSED_IN_JSON)
def test_a_quoted_value_never_runs_past_the_json_string_around_it(text: str, expected: str) -> None:
    out = _redacted(text)
    assert out == expected
    # The JSON stays valid and keeps every member.
    assert json.loads(out).keys() == json.loads(text).keys()


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "escape",
    ["\\n", "\\r\\n", "\\u000a", "\\u000D", "\\\\n"],
    ids=["n", "crlf", "u000a", "u000D", "level-1-n"],
)
def test_a_json_line_break_escape_ends_an_unquoted_value(escape: str) -> None:
    """A ``.env`` file read into a transcript: the value ends at the escaped line
    break, so the settings after it are kept (#2259 review)."""
    text = '{"t":"DB_PASSWORD=Zq9' + escape + "DB_USER=app" + escape + 'DB_PORT=5432 done"}'
    out = _redacted(text)
    assert out == text.replace("Zq9", _MARK)
    assert json.loads(out)["t"].startswith("DB_PASSWORD=" + _MARK)


def test_a_routine_body_comment_is_masked_whole() -> None:
    value = _HEAD + '"' + _TAIL
    body = (
        "CREATE PROCEDURE sync() BEGIN -- password = "
        + value
        + "\n UPDATE orders SET status = 'x'; END"
    )
    out = redact_definition(body, "mysql")
    assert _TAIL not in out and _HEAD not in out
    assert "-- password = ?\n" in out
    # The collector's own scan of the carried body names the kind, as before.
    assert secret_kinds(body) == {"password value"}


@pytest.mark.parametrize("value", _EMBEDDED)
def test_a_comment_is_scrubbed_whole(value: str) -> None:
    for comment in ("-- password = " + value, "/* password: " + value + " */"):
        out = scrub_comment(comment)
        assert _TAIL[2:] not in out, out
        assert out == comment.replace(value, "?")


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("password = " + _HEAD + '",' + _TAIL, id="quote-comma"),
        pytest.param("password = " + _HEAD + '";' + _TAIL, id="quote-semicolon"),
        pytest.param("password = " + _HEAD + '")' + _TAIL, id="quote-paren"),
        pytest.param("password = " + _HEAD + "'}" + _TAIL, id="apostrophe-brace"),
        pytest.param("password = " + _HEAD + '"`' + _TAIL, id="quote-backtick"),
        pytest.param("password='" + _HEAD + "'" + _TAIL + "'", id="same-kind-quote"),
        # A literal line-break escape outside JSON reads as one.
        pytest.param("password = " + _HEAD + "\\n" + _TAIL, id="literal-backslash-n"),
        pytest.param("password = " + _HEAD + "\\r" + _TAIL, id="literal-backslash-r"),
        pytest.param("password = " + _HEAD + "\\u000a" + _TAIL, id="literal-backslash-u000a"),
        # The other quote kind before ``,:}]`` or the end reads as closing a string around it.
        pytest.param('password = "' + _HEAD + "'," + _TAIL + '"', id="other-kind-comma"),
        pytest.param("password = '" + _HEAD + '",' + _TAIL + "'", id="other-kind-comma-single"),
        pytest.param('password = "' + _HEAD + "'}" + _TAIL + '"', id="other-kind-brace"),
    ],
)
def test_the_known_limits_still_cut_the_value_at_the_quote(text: str) -> None:
    """The limits ADR-034 Decision 4 states, open as #2260: the value is still
    redacted only up to the quote, backslash or line-break escape, and the tail after
    it ships. When #2260 is fixed,
    this test changes with it."""
    assert _redacted(text) == text.replace(_HEAD, _MARK)
