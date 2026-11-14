# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SQL bind parameter after ``=`` is no credential value (#2248).

The strict scan (``src/shared/secret_scan.py``) allowed only ``?``, ``%s``, ``$N`` and a
lower-case ``:name`` / ``@name`` ending in the key's own word, so SQL Server's
``u.ApiKey = @ApiKey`` and Oracle's ``token = :key`` were flagged. Now a known
positional shape (``:N`` / ``@N`` of at most three digits, ``@P1``, ``:B1``,
``:SYS_B_0``) passes, and a named bind (``@name``, ``:name``, ``%(name)s``) passes in
any case when its name is itself a credential key name, or bare ``key`` / ``pw``. A
human-word password, a digit run, a ``key: value`` line, a ``.env`` line and a
connection string still flag, in every scan built on the module, comment masking too.
Every value here is synthetic.
"""

from __future__ import annotations

import pytest

from ci.llm import private_scan
from src.agents.collector import routine_sources
from src.shared.secret_scan import credential_spans, secret_findings
from src.tools.sql.redact import scrub_comment

# Synthetic, random-looking 24-character values, built at run time.
_RANDOM = "Xk9mQ2vLr8Tz" + "W4pN7bY3cJ5h"  # pragma: allowlist secret
_RANDOM_LETTERS = "XkqmQzvLrbTz" + "WfpNhbYdcJsh"  # pragma: allowlist secret
_WORDS = "Summer" + "2024"  # pragma: allowlist secret

_BINDS = [
    "SELECT id FROM users u WHERE u.ApiKey = @ApiKey",
    "SELECT id FROM users u WHERE u.ApiKey = @api_key",
    "SELECT id FROM users WHERE token = :key",
    "SELECT id FROM users WHERE token = :TOKEN_VALUE",
    "WHERE password = @password",
    "SET @pwd = @Password",
    "UPDATE users SET password = :NEW_PASSWORD",
    "UPDATE users SET Password = @Pwd",
    "UPDATE users SET password = :pw",
    "UPDATE users SET secret = @ClientSecret, token = @Token)",
    # The DNN sample's three binds.
    "UPDATE Users SET UpdatePassword = @UpdatePassword WHERE UserId = @UserId",
    "UPDATE Users SET PasswordResetToken=@passwordResetToken WHERE UserId=@UserId",
    "SELECT * FROM Portals WHERE AccessKey = @AccessKey",
]
_POSITIONAL = [
    "SELECT id FROM users WHERE token = :1",
    "SELECT id FROM users WHERE token = :123",
    "UPDATE users SET password = @0",
    "UPDATE users SET password = @999",
    "UPDATE users SET password = @P1",
    "UPDATE users SET password = @p12",
    "UPDATE users SET password = :B1",
    "UPDATE users SET password = :SYS_B_0",
]
_PYFORMAT = [
    "cursor.execute('UPDATE users SET x = 1 WHERE api_key = %(api_key)s')",
    "WHERE password = %(pw)s",
    "WHERE password = %(Password)s",
]


@pytest.mark.parametrize("line", _BINDS + _POSITIONAL + _PYFORMAT)
def test_a_bind_parameter_in_any_case_and_name_is_no_value(line: str) -> None:
    assert secret_findings(line) == []
    assert secret_findings(line, narrow_key_names=True) == []


_BYPASSES = [
    # Human-word passwords and leet-speak, as a named bind's shape.
    "password = @" + _WORDS,
    "password = :Password1",
    "password = @Admin123",
    "password = @Welcome1",
    "password = :WELCOME_2024",
    "password = @Correct_Horse_Battery_Staple",
    "password = @dmin123",
    "password = @dministrator2024",
    # A name that is no credential key name, as main intended.
    "password = @session_id",
    # Mixed-case and random values.
    "api_key = @aBcDeFg",
    "api_key = @AbCdEfGh",
    "api_key = @" + _RANDOM,
    "api_key = :" + _RANDOM_LETTERS,
    # Outside SQL: key: value text, .env lines and connection strings.
    "password: @password",
    "password: @" + _WORDS,
    "DB_PASSWORD=@password",
    "DB_PASSWORD=@" + _WORDS,
    "export DB_PASSWORD=@password",
    "Server=db;User Id=sa;Password=@" + _WORDS + ";",
    "Server=db;User Id=sa;Password=@password;",
]


@pytest.mark.parametrize("line", _BYPASSES)
def test_a_value_that_only_looks_like_a_bind_is_still_flagged(line: str) -> None:
    assert secret_findings(line) != []
    assert private_scan.secret_findings(line, narrow_key_names=True) != []
    assert credential_spans(line) != []


# The DNN shape: a SET list written one column per indented line, no space around "=".
_INDENTED_SET = (
    "UPDATE {objectQualifier}Users\n"
    "\t\tSET\n"
    "\t\t\tPasswordResetToken=@passwordResetToken,\n"
    "\t\t\tPasswordResetExpiration=@passwordResetExpiration,\n"
    "\t\t\tUpdatePassword=@UpdatePassword\n"
    "\t\tWHERE UserId=@UserId"
)


def test_an_indented_multi_line_set_list_binds_its_parameters() -> None:
    assert secret_findings(_INDENTED_SET) == []
    assert routine_sources._secret_kinds(_INDENTED_SET) == ([], [])
    assert private_scan.secret_findings(_INDENTED_SET, narrow_key_names=True) == []
    assert scrub_comment("/*\n" + _INDENTED_SET + "\n*/") == "/*\n" + _INDENTED_SET + "\n*/"


@pytest.mark.parametrize(
    "line",
    [
        "\tDB_PASSWORD=@" + _WORDS,  # an indented value that is no credential name
        "  Server=db;Password=@password;",
    ],
)
def test_an_indented_assignment_still_needs_a_bind_shape(line: str) -> None:
    assert secret_findings(line) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "line",
    [
        "password = @" + _WORDS + "Password",
        "password = @My2024Secret",
        "password = @hunter2_pwd",
        "password = :Password2",
        "password = %(api_key2)s",
    ],
)
def test_a_named_bind_has_no_digit_in_its_name(line: str) -> None:
    assert secret_findings(line) != []
    assert credential_spans(line) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "line",
    [
        "password = :1234",
        "password = @20241225",
        "token = :982347598234759823475",
        "password = @P1234",
        "password = :B1234",
        "password = :SYS_B_1234",
    ],
)
def test_a_positional_bind_has_at_most_three_digits(line: str) -> None:
    assert secret_findings(line) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "line",
    [
        "password = %(" + _RANDOM + ")s",
        "password = %(session_id)s",
        "password = %(" + _WORDS + ")s",
    ],
)
def test_a_pyformat_name_must_be_a_credential_key_name(line: str) -> None:
    assert secret_findings(line) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "line",
    [
        "UPDATE users SET api_key = '@ApiKey'",  # a string literal holds a value
        'UPDATE users SET api_key = "@ApiKey"',
        "UPDATE users SET api_key = @@ApiKey",  # a system variable, not a bind
        "UPDATE users SET api_key = @ApiKey!x",  # extra characters: not the bind token
        "UPDATE users SET api_key = :api_key-9",
        "UPDATE users SET api_key = %(api_key)sx",
        "UPDATE users SET api_key = hunter2",
    ],
)
def test_a_value_that_is_not_the_whole_bind_token_is_still_flagged(line: str) -> None:
    assert secret_findings(line) == ["api key value at line 1"]


def test_the_ci_private_scan_does_not_flag_a_bind_parameter() -> None:
    text = "SELECT id FROM users u WHERE u.ApiKey = @ApiKey AND token = :key"
    assert private_scan.secret_findings(text, narrow_key_names=True) == []


def test_the_collector_does_not_flag_a_routine_that_binds_a_key() -> None:
    body = (
        "CREATE PROCEDURE dbo.find_user @ApiKey nvarchar(64) AS "
        "SELECT id FROM users u WHERE u.ApiKey = @ApiKey"
    )
    assert routine_sources._secret_kinds(body) == ([], [])
    assert routine_sources._secret_kinds("SET api_key = @" + _WORDS) == (["api key value"], [])


def test_comment_masking_keeps_a_bind_and_masks_a_value_that_only_looks_like_one() -> None:
    assert scrub_comment("/* WHERE u.ApiKey = @ApiKey AND token = :key */") == (
        "/* WHERE u.ApiKey = @ApiKey AND token = :key */"
    )
    assert scrub_comment("/* conn: Server=db;User Id=sa;Password=@" + _WORDS + "; */") == (
        "/* conn: Server=db;User Id=sa;Password=? */"
    )
    assert scrub_comment("-- password = :123456") == "-- password = ?"
    assert scrub_comment("-- password = @" + _WORDS) == "-- password = ?"


def test_a_credential_named_value_with_a_bind_prefix_reads_as_a_bind() -> None:
    # The stated limit (ADR-034 D4): a credential key name after "@" is a bind's name.
    assert secret_findings("password = @Supersecret") == []
