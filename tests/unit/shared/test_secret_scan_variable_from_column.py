# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A T-SQL ``@variable`` set from a credential-named column is no value (#2253).

``SELECT @UserId = u.UserId, @Password = u.Password FROM Users u`` loads a row into
variables: the text after ``=`` is a column, not a credential. The strict scan read
``@Password`` as the key and the column as its value. Now a ``@variable`` key whose
whole unquoted value is a column reference (a bare, ``[bracketed]`` or
``"double-quoted"`` identifier, optionally dotted) passes when the column itself is a
credential key name with no digit, made of words. A word or digit value, a random one,
a quoted string, a key that is no ``@variable``, a ``.env`` line and a connection
string still flag, in every scan built on the module, comment masking too. Every
value here is synthetic.
"""

from __future__ import annotations

import pytest

from ci.llm import private_scan
from src.agents.collector import routine_sources
from src.shared.secret_scan import credential_spans, secret_findings
from src.tools.sql.redact import scrub_comment

# Synthetic values, built at run time.
_WORDS = "Summer" + "2024"  # pragma: allowlist secret
_RANDOM = "Zq9Xv7" + "Wm3Kt5"  # pragma: allowlist secret
_RANDOM_LETTERS = "Xkqzvb" + "Trwplm"  # pragma: allowlist secret

_FROM_COLUMN = [
    # The DNN sample's form: no space around "=", inside a SELECT list.
    "SELECT @UserId = u.UserId, @Password=Password, @Email=Email FROM Users u",
    "SELECT @UserId = u.UserId, @Password = u.Password FROM Users u",
    "SELECT @Password = [Password] FROM Users",
    "SELECT @Password = [u].[Password] FROM Users u",
    "SELECT @Password = dbo.Users.Password FROM dbo.Users",
    'SELECT @Password = "Password" FROM Users',  # pragma: allowlist secret
    'SELECT @Password = u."Password" FROM Users u',
    "SELECT @Password = t1.Password FROM Users t1",
    "SELECT @pwd = PasswordHash, @Token = t.TokenValue FROM Users t",
    "SELECT @ApiKey = k.api_key FROM Keys k",
    "SELECT @Password = USER_PASSWORD FROM USERS",
    "\tSELECT @Password=Password",
    "SELECT @Password = Password\n",
    "SELECT @Password = Password)",
]


@pytest.mark.parametrize("text", _FROM_COLUMN)
def test_a_variable_set_from_a_credential_named_column_is_no_value(text: str) -> None:
    assert secret_findings(text) == []
    assert secret_findings(text, narrow_key_names=True) == []
    assert credential_spans(text) == []


_STILL_FLAGGED = [
    # A word or digit value, a random one: a password, not a column.
    "SELECT @Password=" + _WORDS,
    "SELECT @Password = " + _RANDOM,
    "SELECT @Password = " + _RANDOM_LETTERS,
    "SELECT @Password = Sunshine",
    "SELECT @Password = u." + _WORDS,
    "SELECT @Password = u.Password2",
    "SELECT @Password = [" + _RANDOM + "]",
    "SELECT @Password = " + _RANDOM + "Password",
    "SELECT @Password = Xkqzvb_Password",
    "SELECT @Password = " + _RANDOM + ".Password",
    "SELECT @Password = " + _RANDOM_LETTERS + ".Password",
    # A column that is no credential key name.
    "SELECT @Password = u.Value FROM Users u",
    # A string literal, a quote of any other kind, extra characters after the column.
    "SET @Password = 'Password'",  # pragma: allowlist secret
    "SET @Password = '" + _WORDS + "'",
    "SELECT @Password = `Password`",  # pragma: allowlist secret
    "SELECT @Password = `Password",
    "SELECT @Password = Password!x",
    "SELECT @Password = u.Password-9",
    # A key that is no @variable: a column, a system variable, a .env line, a
    # connection string.
    "UPDATE Users SET Password = u.Password",
    "SELECT @@Password = Password",
    "DB_PASSWORD=Password",
    "export DB_PASSWORD=Password",
    "@Password=Password",
    "Server=db;User Id=sa;Password=Password;",
    "Server=db;User Id=sa;@Password=Password;",
    # Outside SQL: key: value text.
    "@Password: Password",
]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("text", _STILL_FLAGGED)
def test_anything_but_a_variable_set_from_a_column_is_still_flagged(text: str) -> None:
    assert secret_findings(text) != []
    assert private_scan.secret_findings(text, narrow_key_names=True) != []
    assert credential_spans(text) != []


def test_a_second_pair_on_the_line_still_flags() -> None:
    text = "SELECT @Password = u.Password, @Token = " + _RANDOM + " FROM Users u"
    assert secret_findings(text) == ["token value at line 1"]


def test_the_collector_does_not_flag_a_routine_that_loads_a_row_into_variables() -> None:
    body = (
        "CREATE PROCEDURE dbo.get_user @UserName nvarchar(64) AS\n"
        "SELECT @UserId = u.UserId, @Password=Password FROM Users u\n"
        "WHERE u.UserName = @UserName"
    )
    assert routine_sources._secret_kinds(body) == ([], [])
    assert routine_sources._secret_kinds("SELECT @Password = " + _WORDS) == (
        ["password value"],
        [],
    )


def test_comment_masking_keeps_a_column_and_masks_a_value() -> None:
    assert scrub_comment("/* SELECT @Password = u.Password FROM Users u */") == (
        "/* SELECT @Password = u.Password FROM Users u */"
    )
    assert scrub_comment("-- SELECT @Password = " + _WORDS) == "-- SELECT @Password = ?"


# A value glued after the column, with no whitespace: the column is not the whole value,
# so the pair is a value and the text after it is never skipped (#2267 review).
_GLUED = [
    "SELECT @Password=Password,@Token=" + _RANDOM,
    "-- @Password=Password;Password=" + _RANDOM,
    "SELECT @Password=u.Password;ApiKey=" + _RANDOM,
    "SELECT @Password=[Password];pwd=" + _RANDOM,
    "SELECT @Password=Password," + _RANDOM,
    "SELECT @Password=Password)" + _RANDOM,
]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("text", _GLUED)
def test_a_value_glued_after_the_column_is_flagged_in_every_scan(text: str) -> None:
    assert secret_findings(text) != []
    assert routine_sources._secret_kinds(text)[0] != []
    assert private_scan.secret_findings(text, narrow_key_names=True) != []
    assert credential_spans(text) != []
    comment = text if text.startswith("--") else "/* " + text + " */"
    assert scrub_comment(comment) != comment
    assert _RANDOM not in scrub_comment(comment)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT @password = password",
        "SELECT @pwd = Secret",
        "SELECT @pwd = TopSecret",
        "SELECT @pwd = AdminPassword",
        "SELECT @ApiKey = AccessKey",
        "SELECT @PASSWORD=PASSWORD",
    ],
)
def test_a_word_value_that_is_a_credential_key_name_reads_as_a_column(text: str) -> None:
    # The stated limit (ADR-034 D4): such a value is a column's name, so no finding.
    assert secret_findings(text) == []


def test_a_column_reference_has_at_most_four_parts() -> None:
    assert secret_findings("SELECT @pwd = a.b.c.Password") == []
    assert secret_findings("SELECT @pwd = a.b.c.d.Password") != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize("qualifier", ["Zq9", "Xk9", "ab12", "Q7x"])
def test_a_short_random_qualifier_is_no_alias(qualifier: str) -> None:
    assert secret_findings("SELECT @pwd = " + qualifier + ".Password") != []


@pytest.mark.parametrize("qualifier", ["u", "usr", "t1", "T2"])
def test_a_short_alias_is_letters_or_one_letter_and_a_digit(qualifier: str) -> None:
    assert secret_findings("SELECT @pwd = " + qualifier + ".Password") == []
