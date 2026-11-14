# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A credential's shape written in a SQL comment is masked by the redaction (#2234).

String literals were always redacted; the plain text of a comment was kept, so an
unquoted token in a routine's ``--``, ``/* */`` or MySQL ``#`` comment reached the
collector output. Every comment's text now goes through the repository's own credential
rules (``src.shared.secret_scan.credential_spans``), in the source dialect's comment
forms, and what they flag becomes ``?``. The comment markers stay, so the SQL still reads
the same: a comment never changes a query's type or literal shape. Only made-up shapes
are used here, never a real key.
"""

from __future__ import annotations

import json

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.normalize import literal_shape
from src.tools.sql.query_type import statement_query_type
from src.tools.sql.redact import (
    redact_collector_output,
    redact_comment,
    redact_definition,
    redact_literals,
    redact_raw_collection,
    scrub_comment,
)
from tests.unit.agents.routine_collections import pg_collection

# An AWS access key id's shape (made up, built in two parts so no scanner reads it here).
_KEY = "AKIA" + "QWERTYUIOPASDFGH"  # pragma: allowlist secret
# A key-name pair's shape in plain words: the strict rules flag the value.
_PAIR = "password: " + "not" + "-a-real-one"  # pragma: allowlist secret
_PAIR_VALUE = "not-a-real-one"  # pragma: allowlist secret

# One comment form per case: (engine, the comment holding the key, its marker).
_FORMS = [
    pytest.param("mysql", f"-- deploy key {_KEY}\n", "-- deploy key ?", id="mysql-line"),
    pytest.param("mysql", f"# deploy key {_KEY}\n", "# deploy key ?", id="mysql-hash"),
    pytest.param("mysql", f"/* deploy key {_KEY} */", "/* deploy key ? */", id="mysql-block"),
    pytest.param("postgresql", f"-- deploy key {_KEY}\n", "-- deploy key ?", id="pg-line"),
    pytest.param(
        "postgresql",
        f"/* outer /* deploy key {_KEY} */ still a comment */",
        "/* outer /* deploy key ? */ still a comment */",
        id="pg-nested",
    ),
    pytest.param("oracle", f"-- deploy key {_KEY}\n", "-- deploy key ?", id="oracle-line"),
    pytest.param("oracle", f"/* deploy key {_KEY} */", "/* deploy key ? */", id="oracle-block"),
    pytest.param("sqlserver", f"-- deploy key {_KEY}\n", "-- deploy key ?", id="mssql-line"),
    pytest.param("sqlserver", f"/* deploy key {_KEY} */", "/* deploy key ? */", id="mssql-block"),
]

_BODY = {
    "mysql": "CREATE PROCEDURE sync() BEGIN {c} UPDATE orders SET status = 'x'; END",
    "postgresql": (
        "CREATE FUNCTION sync() RETURNS void LANGUAGE plpgsql AS $f$\nBEGIN\n  {c}\n"
        "  UPDATE orders SET status = 'x';\nEND;\n$f$"
    ),
    "oracle": "CREATE PROCEDURE sync AS BEGIN {c} UPDATE orders SET status = 'x'; END;",
    "sqlserver": "CREATE PROCEDURE dbo.sync AS BEGIN {c} UPDATE orders SET status = 'x' END",
}
_QUERY = "SELECT id FROM orders {c} WHERE status = 'x'"


@pytest.mark.parametrize("engine,comment,masked", _FORMS)
def test_a_key_in_a_routine_body_comment_is_masked(engine, comment, masked):
    out = redact_definition(_BODY[engine].format(c=comment), engine)
    assert _KEY not in out
    assert masked in out
    # Everything outside the comment is redacted as before.
    assert out.replace(masked, "<c>") == redact_definition(
        _BODY[engine].format(c=comment.replace(_KEY, "?")), engine
    ).replace(masked, "<c>")


@pytest.mark.parametrize("engine,comment,masked", _FORMS)
def test_a_key_in_a_query_comment_is_masked_and_the_query_reads_the_same(engine, comment, masked):
    text = _QUERY.format(c=comment)
    clean = _QUERY.format(c="")
    out = redact_literals(text, engine)
    assert _KEY not in out
    assert masked in out
    # A comment never changes what the query is, or its literal shape (the grouping key).
    assert statement_query_type(out, engine) == statement_query_type(clean, engine) == "SELECT"
    assert literal_shape(out, engine) == literal_shape(redact_literals(clean, engine), engine)
    assert redact_literals(out, engine) == out


@pytest.mark.parametrize("engine", ["mysql", "postgresql", "oracle", "sqlserver"])
def test_a_key_name_pair_in_a_comment_is_masked(engine):
    out = redact_definition(_BODY[engine].format(c=f"/* {_PAIR} */"), engine)
    assert _PAIR_VALUE not in out
    assert "/* password: ? */" in out


@pytest.mark.passes_on_main  # the comment-free case must not change
@pytest.mark.parametrize("engine", ["mysql", "postgresql", "oracle", "sqlserver"])
def test_text_without_a_credential_in_its_comments_is_unchanged(engine):
    bare = _BODY[engine].format(c="")
    assert redact_definition(bare, engine) == bare.replace("'x'", "'?'")
    body = _BODY[engine].format(c="/* sets the status; see ticket notes */")
    assert "/* sets the status; see ticket notes */" in redact_definition(body, engine)
    assert redact_comment(" refresh the cache nightly ", engine) == " refresh the cache nightly "
    plain = "SELECT id FROM orders /* app='shop',route='orders' */ WHERE id = ?"
    assert redact_literals(plain, engine) == plain


def test_a_nested_comment_keeps_every_marker_when_a_value_runs_into_one():
    # The pair's unquoted value would run up to the inner closer: the closer stays.
    text = "SELECT 1 /* a /* password=abc*/ b */ FROM t"  # pragma: allowlist secret
    out = redact_literals(text, "postgresql")
    assert "abc" not in out
    assert out == "SELECT 1 /* a /* password=?*/ b */ FROM t"
    assert statement_query_type(out, "postgresql") == "SELECT"


def test_comment_scrub_masks_each_shape_and_is_idempotent():
    for text in (f" key {_KEY} ", f" {_PAIR} ", " Bearer " + "abcdefghij" * 3 + " "):
        once = scrub_comment(text)
        assert once != text and scrub_comment(once) == once
    assert scrub_comment(" nothing here ") == " nothing here "


def test_credential_spans_names_the_spans_never_more():
    from src.shared.secret_scan import credential_spans

    text = f"ok {_KEY} ok"
    assert [text[a:b] for a, b in credential_spans(text)] == [_KEY]
    assert credential_spans("a plain comment") == []


def _pg_with_commented_body() -> dict:
    raw = pg_collection()
    body = raw["procedures"][2]["definition"]
    raw["procedures"][2]["definition"] = body.replace(
        "BEGIN\n", f"BEGIN\n  -- deploy key {_KEY}\n  /* outer /* {_PAIR} */ x */\n"
    )
    raw["queries"][0]["query_text"] += f" /* deploy key {_KEY} */"
    return raw


def test_no_comment_credential_reaches_the_collector_output():
    raw = _pg_with_commented_body()
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1", database_name="shop", s3_key="c.json"
    )
    for text in (
        json.dumps(output),
        json.dumps(redact_collector_output(output)),
        json.dumps(redact_raw_collection(raw)),
    ):
        assert _KEY not in text
        assert _PAIR_VALUE not in text


# A connection string's password (#2234 review): the private scan reads it as Basic Auth.
_PW = "pw" + "-not-real-9"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "engine,dsn,kept",
    [
        ("mysql", f"mysql://svc_app:{_PW}@db-primary:3306/app", "mysql://svc_app:?@db-primary"),
        ("postgresql", f"postgres://svc_app:{_PW}@db.internal/app", "postgres://svc_app:?@db"),
        ("postgresql", f"postgresql://svc_app:{_PW}@db.internal/app", "postgresql://svc_app:?@"),
        ("sqlserver", f"mssql://sa:{_PW}@sql01:1433/shop", "mssql://sa:?@sql01:1433/shop"),
        ("sqlserver", f"sqlserver://sa:{_PW}@sql01:1433", "sqlserver://sa:?@sql01:1433"),
        ("oracle", f"oracle://app:{_PW}@ora01:1521/ORCL", "oracle://app:?@ora01:1521/ORCL"),
        ("mysql", f"jdbc:mysql://svc_app:{_PW}@db-primary:3306/app", "jdbc:mysql://svc_app:?@"),
        # An empty user (Redis), and a password holding an unencoded "@": masked up to
        # the last "@" before the host, as common parsers split it.
        ("postgresql", f"redis://:{_PW}@cache:6379/0", "redis://:?@cache:6379/0"),
        ("mysql", f"mysql://svc_app:p@{_PW}@db-primary:3306/app", "mysql://svc_app:?@db-primary"),
    ],
)
def test_a_connection_string_password_in_a_comment_is_masked(engine, dsn, kept):
    from src.shared.secret_scan import detect_secrets_available, secret_kinds

    for text in (f"SELECT 1 FROM t -- dsn {dsn}\nWHERE 1 = 1", f"SELECT 1 /* dsn {dsn} */"):
        out = redact_literals(text, engine)
        assert _PW not in out
        assert kept in out  # the scheme, the user and the host stay
        if detect_secrets_available():
            assert "Basic Auth Credentials" not in secret_kinds(out)


def test_known_token_formats_in_a_mysql_executable_comment_are_masked():
    for text in (
        f"SELECT 1 /*!99999 {_KEY} */ FROM t",
        f"SELECT 1 /*!50000 {_KEY} */ FROM t",
        f"SELECT 1 /*! {_KEY} */ FROM t",
        # Stuck to the version number (MySQL reads its first 5 or 6 digits).
        f"SELECT 1 /*!50000{_KEY} */ FROM t",
        f"SELECT 1 /*!800001{_KEY} */ FROM t",
    ):
        out = redact_literals(text, "mysql")
        assert _KEY not in out
        assert out.startswith("SELECT 1 /*!") and out.endswith(" */ FROM t")
    # The rest of an executable comment is code, as before.
    code = "SELECT 1 /*!50001 STRAIGHT_JOIN */ FROM t"
    assert redact_literals(code, "mysql") == "SELECT 1 /*!? STRAIGHT_JOIN */ FROM t"
    for hint in (
        "SELECT /*!STRAIGHT_JOIN*/ a FROM t",
        "SELECT a FROM t /*!40000 USE INDEX (idx_a) */",
        "INSERT /*!40000 IGNORE */ INTO t VALUES (?)",
        "SELECT /*! SQL_NO_CACHE */ a FROM t",
    ):
        assert redact_literals(hint, "mysql") == hint.replace("40000", "?")


_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxIn0" + ".c2lnbmF0dXJl"
_GHP = "ghp_" + "A1b2C3d4" * 4 + "Zz9Y"


@pytest.mark.parametrize("token", [_JWT, _GHP], ids=["jwt", "ghp"])
def test_known_token_formats_in_a_comment_are_masked(token):
    assert scrub_comment(f" ship it {token} today ") == " ship it ? today "


def test_a_credential_table_in_a_comment_is_masked():
    table = "\n| app | password |\n|---|---|\n| shop | hunter-two-x |\n"
    out = redact_definition(f"CREATE VIEW v AS SELECT 1 /*{table}*/", "postgresql")
    assert "hunter-two-x" not in out and "| shop | ? |" in out


def test_a_credential_below_the_first_line_of_a_comment_is_masked_in_place():
    text = f"/* line one\n   line two\n   key {_KEY} end\n*/"
    assert scrub_comment(text) == "/* line one\n   line two\n   key ? end\n*/"


def test_the_scrub_repeats_until_nothing_changes():
    # The first public address is masked only once the key next to it is gone.
    text = "8.8.4.4" + _KEY + "?8.8.4.4Bearer "
    once = scrub_comment(text)
    assert _KEY not in once and "8.8.4.4" + "?" not in once
    assert scrub_comment(once) == once


def test_the_mask_is_the_same_with_or_without_detect_secrets(monkeypatch):
    from types import SimpleNamespace

    from src.shared import secret_scan

    plain = " a plain comment about the deploy "
    before = (scrub_comment(plain), secret_scan.credential_spans(plain))
    # A plugin that flags every line: the comment mask never runs it.
    hit = SimpleNamespace(type="Fake Plugin", secret_value="plain")
    monkeypatch.setattr(secret_scan, "_scan_line", lambda line: [hit])
    monkeypatch.setattr(secret_scan, "_denylist", None)
    # The strict scan runs the plugins: it finds the fake hit.
    assert ("Fake Plugin", (3, 8)) in secret_scan._line_hits(plain, emails=False)
    assert (scrub_comment(plain), secret_scan.credential_spans(plain)) == before == (plain, [])


def test_an_empty_span_is_never_a_span(monkeypatch):
    from src.shared import secret_scan

    monkeypatch.setattr(secret_scan, "_line_hits", lambda *_a, **_k: [("x", (3, 3))])
    assert secret_scan.credential_spans("abcdef") == []


@pytest.mark.parametrize(
    "engine,text,binds",
    [
        (
            "sqlserver",
            "SELECT id FROM users u WHERE id = 1 -- by key WHERE u.ApiKey = @ApiKey AND u.Pwd = @Pwd",
            ("= @ApiKey", "= @Pwd"),
        ),
        ("oracle", "SELECT id FROM users u WHERE id = 1 -- by token = :tok", ("token = :tok",)),
        ("mysql", "SELECT id FROM users u WHERE id = 1 -- by password = ?", ("password = ?",)),
    ],
)
def test_the_rest_of_a_one_line_statement_keeps_its_bind_parameters(engine, text, binds):
    out = redact_literals(text, engine)
    for bind in binds:
        assert bind in out
    # A value there is still masked.
    leaky = text.split(" -- ")[0] + f" -- by {_PAIR}"
    assert _PAIR_VALUE not in redact_literals(leaky, engine)
