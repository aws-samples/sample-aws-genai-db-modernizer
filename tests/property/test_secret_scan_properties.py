# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A generated secret in each supported syntax is always masked (#2260, #2263).

Each value is a random head and tail with a run of quotes, backslashes, brackets and
other punctuation between them, written in one syntax the credential scan reads: an
unquoted ``key=value`` or ``key: value``, a ``.env`` line, a SQL string with doubled
quotes, a double-quoted string, JSON at one or two escape levels, a JSON array, a YAML
block scalar, a Markdown table and a value whose quote never closes. The redaction of
the CI private scan is complete and keeps neither the head nor the tail, and comment
masking (``scrub_comment``) keeps neither. The values are synthetic.
"""

from __future__ import annotations

import json
import string
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ci.llm import private_scan
from src.tools.sql.redact import scrub_comment
from tests.hypothesis_examples import scaled

# Upper case and digits, after a ``Q``: never part of the text around the value or of
# a redaction marker.
_PART = st.text(alphabet=string.ascii_uppercase + string.digits, min_size=6, max_size=10).map(
    lambda s: "Q" + s
)
_PUNCTUATION = "\"'\\`,;:)]}([{.!@#$%^&*=+-/<>?|~"
_MIDDLE = st.text(alphabet=_PUNCTUATION, min_size=1, max_size=6)
_SPACED_MIDDLE = st.text(alphabet=_PUNCTUATION + " ", min_size=1, max_size=8)


def _without(chars: str) -> Callable[[str], str]:
    return lambda middle: "".join(c for c in middle if c not in chars) or "."


def _sql(value: str) -> str:
    return "SET password = '" + value.replace("'", "''") + "';"


def _double_quoted(value: str) -> str:
    return 'password = "' + json.dumps(value)[1:-1] + '"'


def _column(value: str) -> str:
    return "| user | password |\n| --- | --- |\n| app | " + value + " |\n"


# Each syntax: how it writes a value, and which middles it can hold.
_SYNTAXES: dict[str, tuple[Callable[[str], str], st.SearchStrategy[str]]] = {
    "unquoted-equals": (lambda v: "password = " + v, _MIDDLE),
    "unquoted-colon": (lambda v: "password: " + v, _MIDDLE),
    "dotenv": (lambda v: "APP_ENV=prod\nDB_PASSWORD=" + v + "\nexport X=1\n", _MIDDLE),
    "sql-string": (_sql, _SPACED_MIDDLE),
    "double-quoted": (_double_quoted, _SPACED_MIDDLE),
    "json": (lambda v: json.dumps({"password": v, "user": "app"}), _SPACED_MIDDLE),
    "json-in-json": (
        lambda v: json.dumps({"input": json.dumps({"api_key": v, "n": 1})}),
        _SPACED_MIDDLE,
    ),
    "json-array": (lambda v: json.dumps({"secret": [v, "x"]}, indent=2), _SPACED_MIDDLE),
    "yaml-block": (lambda v: "db:\n  password: |\n    " + v + "\n  user: app\n", _SPACED_MIDDLE),
    "markdown-row": (lambda v: "| password | " + v + " |", _SPACED_MIDDLE.map(_without("|"))),
    "markdown-column": (_column, _SPACED_MIDDLE.map(_without("|"))),
    "unclosed-quote": (lambda v: "password = '" + v, _SPACED_MIDDLE.map(_without("'"))),
}


# Text before the key: a possessive apostrophe, an inch mark, an apostrophe inside a word,
# a string the line opens and closes before the key (#2595).
_BEFORE = st.sampled_from(
    ["", "-- the admins' ", '-- 5" display; ', "don't set ", "'x' , ", "-- Plan B's ", 'Plan B" ; ']
)


@pytest.mark.parametrize("syntax", list(_SYNTAXES), ids=list(_SYNTAXES))
def test_a_generated_secret_in_every_syntax_is_masked(syntax: str) -> None:
    write, middles = _SYNTAXES[syntax]

    @settings(max_examples=scaled(300), deadline=None, suppress_health_check=list(HealthCheck))
    @given(_PART, middles, _PART, _BEFORE)
    def check(head: str, middle: str, tail: str, before: str) -> None:
        text = write(head + middle + tail)
        if syntax.startswith("unquoted") or syntax in ("sql-string", "unclosed-quote"):
            text = before + text
        out = private_scan.redact(text, narrow_key_names=True)
        assert out.complete, text
        assert head not in out.text and tail not in out.text, (text, out.text)
        masked = scrub_comment(text)
        assert head not in masked and tail not in masked, (text, masked)

    check()


@pytest.mark.parametrize("engine", ["sqlserver", "oracle", "mysql", "postgresql", None])
def test_a_generated_secret_in_a_sql_string_is_masked_in_every_dialect(engine: str | None) -> None:
    """A SQL string with doubled quotes, backslashes and spaces in it, plain, prefixed
    and nested in dynamic SQL, read in each dialect (#2262)."""

    @settings(max_examples=scaled(300), deadline=None, suppress_health_check=list(HealthCheck))
    @given(
        _PART,
        _SPACED_MIDDLE,
        _PART,
        st.sampled_from(["plain", "prefixed", "nested", "nested-n", "sp-executesql", "set-sql"]),
    )
    def check(head: str, middle: str, tail: str, form: str) -> None:
        inner = (head + middle + tail).replace("'", "''")
        text = {
            "plain": "SET password = '" + inner + "';",
            "prefixed": "SET password = N'" + inner + "';",
            "nested": "EXEC('ALTER LOGIN sa WITH PASSWORD=''" + inner.replace("'", "''") + "''')",
            # Dynamic SQL in a prefixed string (#2595).
            "nested-n": "EXEC(N'ALTER LOGIN sa WITH PASSWORD=N''"
            + inner.replace("'", "''")
            + "''')",
            "sp-executesql": "EXEC sp_executesql N'ALTER LOGIN sa WITH PASSWORD=''"
            + inner.replace("'", "''")
            + "'''",
            "set-sql": "SET @sql = N'CREATE LOGIN app WITH PASSWORD=''"
            + inner.replace("'", "''")
            + "'', CHECK_POLICY=OFF'",
        }[form]
        masked = scrub_comment(text, source_engine=engine)
        assert head not in masked and tail not in masked, (text, masked)

    check()
