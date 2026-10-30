# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The facade's dialect-aware tokens (#471): what the relational-move gap check reads."""

from __future__ import annotations

import pytest

from src.tools.sql.facade import sql_tokens


def test_words_and_calls_are_upper_cased_in_text_order() -> None:
    t = sql_tokens("select top (5) name from t where getdate() > x", "sqlserver")
    assert t.tokens[:5] == ("SELECT", "TOP", "(", "5", ")")
    assert {"TOP", "GETDATE"} <= t.calls
    assert "NAME" not in t.calls


@pytest.mark.parametrize(
    ("sql", "engine"),
    [
        ("SELECT 'NOLOCK and GETDATE()' FROM t", "sqlserver"),
        ("SELECT x FROM t -- NOLOCK GETDATE()", "sqlserver"),
        ("SELECT x /* NOLOCK GETDATE() */ FROM t", "sqlserver"),
        ("SELECT [NOLOCK], [GETDATE] FROM t", "sqlserver"),
        ('SELECT "NOLOCK" FROM t', "postgresql"),
        ("SELECT $$ NOLOCK GETDATE() $$", "postgresql"),
        ("SELECT x FROM t # NOLOCK GETDATE()", "mysql"),
        ("SELECT 'it\\'s NOLOCK GETDATE()' FROM t", "mysql"),
        ("SELECT q'[NOLOCK GETDATE()]' FROM dual", "oracle"),
    ],
)
def test_literals_quoted_names_and_comments_never_count(sql: str, engine: str) -> None:
    t = sql_tokens(sql, engine)
    assert "NOLOCK" not in t.tokens
    assert "GETDATE" not in t.calls


def test_hash_is_a_comment_only_in_the_mysql_family() -> None:
    # In PostgreSQL "#" is an operator, so what follows it is still SQL.
    assert "SECRET" not in sql_tokens("SELECT 1 # secret", "mysql").tokens
    assert "SECRET" in sql_tokens("SELECT 1 # secret", "postgresql").tokens


def test_backslash_escapes_only_in_the_mysql_family() -> None:
    # PostgreSQL's standard strings give a backslash no meaning: the literal ends at
    # the second quote, so NOLOCK is outside it.
    sql = "SELECT 'a\\' NOLOCK '"
    assert "NOLOCK" not in sql_tokens(sql, "mysql").tokens
    assert "NOLOCK" in sql_tokens(sql, "postgresql").tokens


def test_a_quoted_function_name_is_still_a_call() -> None:
    # A MySQL digest writes a native function name in backticks.
    assert "FOUND_ROWS" in sql_tokens("SELECT `FOUND_ROWS` ( )", "mysql").calls
    assert "TIME_BUCKET" in sql_tokens('SELECT "time_bucket"(x) FROM t', "postgresql").calls
    # A quoted column is not a call, nor a quoted name inside a literal.
    assert sql_tokens("SELECT `found_rows` FROM t", "mysql").calls == frozenset()
    assert sql_tokens("SELECT 'say `found_rows`(' FROM t", "mysql").calls == frozenset()


def test_dotted_calls_keep_their_qualifier() -> None:
    t = sql_tokens("BEGIN DBMS_OUTPUT.PUT_LINE('x'); END;", "oracle")
    assert {"PUT_LINE", "DBMS_OUTPUT.PUT_LINE"} <= t.calls


def test_sigils_stay_on_system_variables_and_temp_tables() -> None:
    t = sql_tokens("SELECT @@IDENTITY, @id FROM #tmp", "sqlserver")
    assert {"@@IDENTITY", "@ID", "#TMP"} <= set(t.tokens)


def test_punctuation_tokens_form_operators() -> None:
    t = sql_tokens("WHERE a.id = b.id(+) AND s.NEXTVAL > 1", "oracle")
    joined = " ".join(t.tokens)
    assert "( + )" in joined
    assert ". NEXTVAL" in joined


@pytest.mark.parametrize("sql", ["", None, "   ", "'unterminated", "/* open"])
def test_never_raises(sql: str | None) -> None:
    assert sql_tokens(sql, "mysql").tokens is not None  # type: ignore[arg-type]


def test_unqualified_calls_leave_out_qualified_ones() -> None:
    t = sql_tokens('SELECT a.f(1), g(2), "h"(3) FROM t', "postgresql")
    assert {"A.F", "F", "G", "H"} <= t.calls
    assert t.unqualified_calls == {"G", "H"}
