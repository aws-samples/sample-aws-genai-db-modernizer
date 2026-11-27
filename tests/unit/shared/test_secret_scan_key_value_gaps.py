# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Five gaps in the credential scan's key-value rule (#2263).

1. A long run of backslashes before a key cost time quadratic in the run.
2. ``"DynamoDBKey": {`` read as a pair whose value is ``{``, and redaction broke the JSON.
3. An unquoted value took a ``)`` that closes a bracket around it.
4. A YAML block scalar (``password: |``) flagged only the ``|``, not its lines.
5. A value whose opening quote never closes was no finding.

Every value here is synthetic and built at run time.
"""

from __future__ import annotations

import json

import pytest

from ci.llm import private_scan
from src.shared.secret_scan import credential_spans, secret_findings, secret_kinds
from src.tools.sql.redact import scrub_comment
from tests.unit.tools.sql._timing import assert_linear

_VALUE = "Zq9" + "Xv7" + "Wm3Kt5"  # pragma: allowlist secret
_MARK = "[REDACTED:password-value]"
_BS = "\\"


def _redact(text: str) -> private_scan.Redaction:
    return private_scan.redact(text, narrow_key_names=True)


# -- 1. Linear time on long backslash and quote runs -----------------------------------

# Each shape is a line of ``n`` repeated characters around a key: the scan must grow
# linearly with ``n``, never quadratically.
_RUN_SHAPES = {
    "backslashes-before-a-json-key": lambda n: '"' + _BS * n + 'password": 1',
    "backslashes-in-a-quoted-value": lambda n: 'password = "' + _BS * n + "x",
    "backslashes-after-an-unquoted-value": lambda n: "password=Zq9" + _BS * n + "x",
}
# Shapes main already scans in linear time, kept so a change to the value's end does
# not make them quadratic.
_LINEAR_ON_MAIN = {
    "escaped-quotes-in-a-quoted-value": lambda n: "password = '" + (_BS + "'") * n,
    "quotes-after-a-key": lambda n: "password: " + "\"'" * n,
    "many-keys": lambda n: ('"password": "' + _BS + '"x", ') * (n // 16),
}
_SHAPES = [pytest.param(make, id=name) for name, make in _RUN_SHAPES.items()] + [
    pytest.param(make, id=name, marks=pytest.mark.passes_on_main)
    for name, make in _LINEAR_ON_MAIN.items()
]


@pytest.mark.parametrize("make", _SHAPES)
def test_the_scan_is_linear_in_a_run_of_backslashes_or_quotes(make) -> None:  # type: ignore[no-untyped-def]
    def scan(text: str) -> None:
        secret_findings(text)
        _redact(text)

    assert_linear(make, scan, sizes=(2_000, 16_000))


# -- 2. A key whose value is an object or an array -------------------------------------


@pytest.mark.parametrize("key", ["DynamoDBKey", "api_key", "password", "secret"])
def test_the_values_of_an_object_under_a_key_name_are_masked_whatever_its_members(
    key: str,
) -> None:
    """The scan reads each value in the object, whatever its member names (#2595: no
    schema-shaped exception). Main's rules still mask the ``{`` after the key too, and
    the union keeps that, as main does."""
    text = json.dumps(
        {key: {"type": "object", "properties": {"name": {"type": "str" + "ing"}}}}, indent=2
    )
    result = _redact(text)
    assert result.findings
    assert '"object"' not in result.text and '"string"' not in result.text


def test_each_string_in_an_array_under_a_credential_key_is_a_value() -> None:
    """An array's scalars are values, one-line, pretty-printed and escaped (#2263)."""
    one_line = json.dumps({"password": [_VALUE, "abc" + "Def1"], "user": "app"})
    out = _redact(one_line)
    assert _VALUE not in out.text and "abcDef1" not in out.text and '"user": "app"' in out.text
    pretty = json.dumps({"password": [_VALUE, {"id": 1}], "user": "app"}, indent=2)
    out = _redact(pretty)
    assert _VALUE not in out.text and '"user": "app"' in out.text
    assert secret_kinds(pretty) == {"password value"}
    nested = json.dumps({"input": one_line})
    assert _VALUE not in _redact(nested).text


# -- 3. A closing bracket after an unquoted value --------------------------------------


@pytest.mark.passes_on_main  # main masks these whole too; guards that the union keeps it
@pytest.mark.parametrize(
    "text",
    [
        "f(password=" + _VALUE + ")",
        "f(x, password=" + _VALUE + "), g(y)",
        "[password=" + _VALUE + "]",
        "{password=" + _VALUE + "}",
        "f(password=" + _VALUE + "(1))",
    ],
)
def test_a_value_before_a_closing_bracket_is_masked(text: str) -> None:
    """The value is masked; main's rules also mask the bracket after it, and the union
    keeps that (#2595), so the issue's ``f(password=...)`` keeps no ``)``."""
    assert _VALUE not in _redact(text).text and _VALUE not in scrub_comment(text)
    assert _redact(text).text.startswith(text.split("password=")[0] + "password=")


@pytest.mark.passes_on_main  # main already masks the whole run, bracket included
@pytest.mark.parametrize("value", [_VALUE + ")", _VALUE + "]", _VALUE + "}"])
def test_a_closing_bracket_with_nothing_open_is_part_of_the_value(value: str) -> None:
    text = "password=" + value
    assert _redact(text).text == "password=" + _MARK
    (span,) = credential_spans(text)
    assert text[span[0] : span[1]] == value


# -- 4. YAML block scalars --------------------------------------------------------------


@pytest.mark.parametrize("indicator", ["|", ">", "|-", ">+", "|2", "|+2", ">-1"])
def test_a_yaml_block_scalar_s_lines_are_the_value(indicator: str) -> None:
    """The block's more indented lines are its value (#2263). Main's rules mask the
    indicator itself, and the union keeps that."""
    text = (
        "db:\n"
        f"  password: {indicator}\n"
        f"    {_VALUE}\n"
        "\n"
        f"    second{_VALUE[::-1]}\n"
        "  user: app\n"
    )
    out = _redact(text)
    assert (
        out.text
        == "db:\n  password: " + _MARK + "\n    " + _MARK + "\n\n    " + _MARK + "\n  user: app\n"
    )
    assert out.complete
    assert ("password value", 3) in out.findings and ("password value", 5) in out.findings
    masked = scrub_comment(text)
    assert _VALUE not in masked and _VALUE[::-1] not in masked
    assert "  user: app" in masked


def test_a_block_scalar_with_a_comment_and_a_placeholder() -> None:
    text = "password: |  # the app's\n  <password>\n  " + _VALUE + "\nuser: app\n"
    out = _redact(text)
    assert out.text.endswith("\n  <password>\n  " + _MARK + "\nuser: app\n")


def test_a_block_scalar_inside_a_json_string_withholds_the_file() -> None:
    """In one JSON string the block's lines are line-break escapes the scan does not
    split, so the redaction cannot tell where the value ends: it is incomplete and
    the file is withheld."""
    text = json.dumps({"content": "password: |\n  " + _VALUE + "\nuser: app\n"})
    assert secret_findings(text) == ["password value at line 1"]
    assert not _redact(text).complete
    # Comment masking has no file to withhold: it masks to the end of the line.
    assert _VALUE not in scrub_comment(text)


# -- 5. A value whose opening quote never closes -----------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "password = '{v}",
        'password = "{v}',
        "password: '{v}",
        "DB_PASSWORD='{v}",
        '"password": "{v}',
    ],
)
def test_a_value_whose_opening_quote_never_closes_is_a_finding(line: str) -> None:
    text = line.format(v=_VALUE)
    assert secret_findings(text) == ["password value at line 1"]
    assert secret_kinds(text) == {"password value"}
    assert _redact(text).text == line.format(v=_MARK)
    assert scrub_comment(text) == line.format(v="?")
    # At the end of a line inside a text, too.
    assert _redact(text + "\nuser: app\n").text == line.format(v=_MARK) + "\nuser: app\n"


@pytest.mark.passes_on_main  # main finds no value in an empty quote either
def test_an_empty_quote_is_still_no_finding() -> None:
    for text in ("password = ''", 'password = ""', "password = '", '"password": ""'):
        assert secret_findings(text) == [], text


@pytest.mark.passes_on_main  # main finds no value here either: guards that the unclosed-quote rule adds none
@pytest.mark.parametrize(
    "text",
    [
        "x = 'password=' + pw",
        'conn("password=" + pw)',
        "CONCAT('password=', @p)",
    ],
)
def test_a_quote_that_closes_a_string_around_the_key_opens_no_value(text: str) -> None:
    """A quote right after ``key=`` that ends a string opened before the key opens no
    value, so the unclosed-quote rule above flags nothing here. Where main's rules do
    find one (``{"a":"password=","b":1}``, an ``echo 'password=' ...`` line), the union
    keeps it (``tests/property/test_secret_scan_differential.py``)."""
    assert secret_findings(text) == []
    assert _redact(text).text == text


# -- #2595 review: values inside an object, YAML keys and flow collections -------------

_A, _B = "Ab1" + "Cd2", "Ef3" + "Gh4"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "value",
    [
        {"current": _A + _B},
        [{"v": _A + _B}],
        {"history": [{"old": _A + _B}]},
    ],
    ids=["object", "array-of-objects", "nested"],
)
@pytest.mark.parametrize("indent", [None, 2], ids=["one-line", "pretty"])
def test_the_values_inside_an_object_under_a_credential_key_are_masked(value, indent) -> None:  # type: ignore[no-untyped-def]
    text = json.dumps({"password": value, "user": "app"}, indent=indent)
    assert secret_kinds(text) == {"password value"}
    out = _redact(text)
    assert _A not in out.text and _B not in out.text
    assert _A not in scrub_comment(text)


def test_a_json_literal_in_a_routine_body_is_flagged() -> None:
    body = (
        "CREATE FUNCTION f() RETURNS json AS $$ SELECT '"
        + json.dumps({"password": {"current": _A + _B}})
        + "'::json $$ LANGUAGE sql"
    )
    assert secret_kinds(body, source_engine="postgresql") == {"password value"}


@pytest.mark.parametrize(
    "member",
    [
        {"type": "{v}"},
        {"type": "string", "title": "{v}"},
        {"type": "string", "pattern": "{v}"},
        {"$ref": "{v}"},
        {"enum": ["{v}"]},
        {"items": ["{v}"]},
        {"properties": {"current": "{v}"}},
        {"allOf": [{"current": "{v}"}]},
    ],
    ids=["type", "title", "pattern", "ref", "enum", "items", "properties", "all-of"],
)
@pytest.mark.parametrize("indent", [None, 2], ids=["one-line", "pretty"])
def test_schema_keyword_names_never_hide_a_value(member, indent) -> None:  # type: ignore[no-untyped-def]
    """No member name exempts a value: a schema-shaped object is scanned like any
    other (#2595 review round 2)."""
    text = json.dumps({"password": member}, indent=indent).replace("{v}", _A + _B)
    assert secret_kinds(text) == {"password value"}
    out = _redact(text)
    assert out.complete and _A not in out.text
    escaped = json.dumps({"input": text})
    assert _A not in _redact(escaped).text
    body = (
        "CREATE FUNCTION f() RETURNS json AS $$ SELECT '" + text.replace("\n", " ") + "'::json $$"
    )
    assert secret_kinds(body, source_engine="postgresql") == {"password value"}


@pytest.mark.parametrize(
    "text",
    [
        '"password": |\n  {v}\nuser: app\n',
        "'password': |\n  {v}\nuser: app\n",
        "password: !!str |\n  {v}\nuser: app\n",
        "password: !secret >-\n  {v}\nuser: app\n",
    ],
    ids=["double-quoted-key", "single-quoted-key", "str-tag", "custom-tag"],
)
def test_a_block_scalar_under_a_quoted_key_or_a_tag_is_masked(text: str) -> None:
    text = text.format(v=_A + _B)
    out = _redact(text)
    assert out.complete and _A not in out.text and "user: app" in out.text
    assert _A not in scrub_comment(text)


@pytest.mark.parametrize(
    "text",
    ["password: [{a}, {b}]", "password: {{current: {b}}}", "secret: [[{a}], {{x: {b}}}] # note"],
    ids=["sequence", "mapping", "nested"],
)
def test_a_yaml_flow_collection_is_masked_whole(text: str) -> None:
    text = text.format(a=_A, b=_B)
    out = _redact(text)
    assert out.complete and _A not in out.text and _B not in out.text
    assert _A not in scrub_comment(text) and _B not in scrub_comment(text)


def test_a_flow_collection_that_does_not_close_on_its_line_withholds_the_file() -> None:
    assert not _redact("password: [" + _A + ",\n  " + _B + "]\n").complete


# Shapes whose cost grew with the line or the file on the first round of #2595.
_REPEATED_SHAPES = {
    "array-keys-holding-their-own-name": lambda n: '"password": ["password", ' * (n // 24),
    "unclosed-array-keys": lambda n: '"password": [' * (n // 13),
}
# Main never walked an object, so this shape was linear there; kept so the walk stays so.
_REPEATED_ON_MAIN = {"unclosed-object-keys": lambda n: '"password": {"a": ' * (n // 18)}


@pytest.mark.passes_on_main  # main never had these regressions of this PR's first head
@pytest.mark.parametrize(
    "make",
    [pytest.param(make, id=name) for name, make in _REPEATED_SHAPES.items()]
    + [
        pytest.param(make, id=name, marks=pytest.mark.passes_on_main)
        for name, make in _REPEATED_ON_MAIN.items()
    ],
)
def test_the_scan_is_linear_in_repeated_container_and_block_keys(make) -> None:  # type: ignore[no-untyped-def]
    def scan(text: str) -> None:
        secret_findings(text)
        _redact(text)

    assert_linear(make, scan, sizes=(2_000, 16_000))


@pytest.mark.passes_on_main  # main never had these regressions of this PR's first head
def test_nested_block_scalars_cost_no_more_than_the_same_lines_unnested() -> None:
    """Each line more indented than the last, each a block scalar key: the blocks are
    read once, so the scan costs no more than twice the same lines in the reverse order,
    where each block ends at once (a ratio in one process, never a time)."""
    from tests.unit.tools.sql._timing import assert_cost_ratio

    lines = 1500
    nested = "".join(" " * i + "password: |\n" for i in range(lines))
    unnested = "".join(" " * (lines - 1 - i) + "password: |\n" for i in range(lines))
    assert_cost_ratio(lambda: secret_findings(nested), lambda: secret_findings(unnested), 2)


# -- #2595 review round 2: flow collections and containers fail closed -----------------


@pytest.mark.parametrize(
    "text",
    [
        "password=[]{v}",
        "password={{}}{v}",
        "password: []{v}",
        "password=[{a}]{b}",
        "password={{{a}}}{b}",
        "ALTER USER u WITH PASSWORD=[{a}]{b}",
        "password: [{a}]{b}",
        "password: [{a}], {b}",
    ],
)
def test_text_after_a_closing_bracket_is_never_kept(text: str) -> None:
    text = text.format(v=_A + _B, a=_A, b=_B)
    out = _redact(text)
    assert out.findings and _A not in out.text and _B not in out.text
    assert _A not in scrub_comment(text) and _B not in scrub_comment(text)


def test_a_bracket_in_a_mysql_string_value_is_flagged_by_the_collector() -> None:
    body = "CREATE PROCEDURE p() BEGIN SET @c = 'password={}" + _A + _B + "'; END"
    assert secret_kinds(body, source_engine="mysql") == {"password value"}


@pytest.mark.parametrize(
    "text",
    ['password: [{a}, "]", {b}]', 'password: {{a: "}}", b: {b}}}', "password: ['{a}]', {b}]"],
    ids=["quoted-bracket", "quoted-brace", "single-quoted-bracket"],
)
def test_a_flow_collection_skips_brackets_inside_quotes(text: str) -> None:
    text = text.format(a=_A, b=_B)
    out = _redact(text)
    assert out.complete and _B not in out.text
    assert _B not in scrub_comment(text)


@pytest.mark.parametrize(
    "text",
    ['{{"password": ["a", \\"{v}\\"]}}', '{{"password": [C:\\x, "{v}"]}}'],
    ids=["other-escape-level", "stray-backslash"],
)
def test_a_container_walk_that_stops_early_withholds_the_file(text: str) -> None:
    text = text.format(v=_A + _B)
    out = _redact(text)
    assert not out.complete
    assert _A not in scrub_comment(text)


_SCHEMA_AND_FLOW_SHAPES = {
    "nested-objects-on-one-line": lambda n: '{"password": {"type": "object", "properties": '
    * (n // 48),
    "repeated-flow-keys": lambda n: "password: [" * (n // 11),
}


@pytest.mark.passes_on_main  # guards behaviour main already has; changed here for a regression of this PR's first head
@pytest.mark.parametrize(
    "make", list(_SCHEMA_AND_FLOW_SHAPES.values()), ids=list(_SCHEMA_AND_FLOW_SHAPES)
)
def test_the_scan_is_linear_in_nested_objects_and_repeated_flow_keys(make) -> None:  # type: ignore[no-untyped-def]
    def scan(text: str) -> None:
        secret_findings(text)
        _redact(text)

    assert_linear(make, scan, sizes=(2_000, 16_000))


@pytest.mark.passes_on_main  # guards behaviour main already has; changed here for a regression of this PR's first head
def test_nested_objects_one_per_line_cost_no_more_than_unnested_ones() -> None:
    from tests.unit.tools.sql._timing import assert_cost_ratio

    lines = 400
    nested = '{"password": {"type": "object", "properties":\n' * lines
    flat = '{"password": {"type": "object", "properties": 1}}\n' * lines
    assert_cost_ratio(lambda: secret_findings(nested), lambda: secret_findings(flat), 3)
