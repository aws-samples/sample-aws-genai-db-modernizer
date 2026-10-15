"""Column DEFAULT translation for the Aurora draft (#156, item 4).

Source defaults are collector text. Only literals and a small per-target grammar
of expressions reach DDL, rebuilt from parsed parts; anything else is dropped
with a reason, never pasted in and never quoted into a fake literal.
"""

from __future__ import annotations

import pytest

from src.tools.schema.aurora_common.constraint_translator import translate_default
from tests.unit.tools.sql._timing import assert_fast

PG = "aurora_postgresql"
MY = "aurora_mysql"


def _pg(value, source="postgresql", aurora_type="TEXT", **kw):
    return translate_default(value, source_engine=source, target=PG, aurora_type=aurora_type, **kw)


def _my(value, source="mysql", aurora_type="VARCHAR(50)", **kw):
    return translate_default(value, source_engine=source, target=MY, aurora_type=aurora_type, **kw)


# --- Aurora PostgreSQL: every recognized form --------------------------------


@pytest.mark.parametrize(
    "value,aurora_type,clause",
    [
        ("now()", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("now( )", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("NOW()", "TIMESTAMP WITH TIME ZONE", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP(3)", "TIMESTAMP(3)", " DEFAULT CURRENT_TIMESTAMP(3)"),
        ("CURRENT_DATE", "DATE", " DEFAULT CURRENT_DATE"),
        ("gen_random_uuid()", "UUID", " DEFAULT gen_random_uuid()"),
        ("uuid_generate_v4()", "UUID", " DEFAULT gen_random_uuid()"),
        ("0", "INTEGER", " DEFAULT 0"),
        ("-12", "INTEGER", " DEFAULT -12"),
        ("(-1)", "INTEGER", " DEFAULT -1"),
        ("1.5", "NUMERIC(10,2)", " DEFAULT 1.5"),
        ("'-1'::integer", "INTEGER", " DEFAULT '-1'::integer"),
        ("'-31'::integer", "BIGINT", " DEFAULT '-31'"),  # cast differs from the column type
        ("'{}'::jsonb", "JSONB", " DEFAULT '{}'::jsonb"),
        ("'[]'::json", "JSON", " DEFAULT '[]'::json"),
        ("'{}'::integer[]", "INTEGER[]", " DEFAULT '{}'::integer[]"),
        ("'{}'::text[]", "TEXT[]", " DEFAULT '{}'::text[]"),
        ("'open'::character varying", "VARCHAR", " DEFAULT 'open'"),
        ("''::character varying", "VARCHAR(255)", " DEFAULT ''"),
        ("'SELECT 1'::text", "TEXT", " DEFAULT 'SELECT 1'::text"),
        ("'O''Brien'::text", "TEXT", " DEFAULT 'O''Brien'::text"),
        ("'draft'::post_status", "TEXT", " DEFAULT 'draft'"),  # not an allowlisted type
        ("'abc'", "TEXT", " DEFAULT 'abc'"),
        ("true", "BOOLEAN", " DEFAULT TRUE"),
        ("FALSE", "BOOLEAN", " DEFAULT FALSE"),
        ("NULL", "TEXT", " DEFAULT NULL"),
        ("NULL::character varying", "VARCHAR(20)", " DEFAULT NULL"),
    ],
)
def test_pg_recognized_forms(value, aurora_type, clause):
    result = _pg(value, aurora_type=aurora_type)
    assert result.clause == clause
    assert result.reason == ""


@pytest.mark.parametrize(
    "value",
    [
        "nextval('topics_id_seq'::regclass)",
        "nextval('public.topics_id_seq'::regclass)",
        "nextval( 'topics_id_seq'::regclass )",
    ],
)
def test_pg_sequence_becomes_identity_on_an_integer_column(value):
    result = _pg(value, aurora_type="BIGINT", identity_ok=True)
    assert result.identity is True
    assert result.clause == ""
    assert "identity" in result.reason.lower()


def test_pg_sequence_on_a_non_integer_column_is_dropped():
    result = _pg("nextval('s'::regclass)", aurora_type="TEXT", identity_ok=True)
    assert result.identity is False and result.clause == "" and result.reason


def test_pg_sequence_without_an_identity_slot_is_dropped():
    result = _pg("nextval('s'::regclass)", aurora_type="BIGINT", identity_ok=False)
    assert result.identity is False and result.clause == "" and result.reason


def test_python_typed_defaults():
    assert _pg(None).clause == ""
    assert _pg(42, aurora_type="INTEGER").clause == " DEFAULT 42"
    assert _pg(3.5, aurora_type="REAL").clause == " DEFAULT 3.5"
    assert _pg(True, aurora_type="BOOLEAN").clause == " DEFAULT TRUE"
    assert _pg(False, aurora_type="BOOLEAN").clause == " DEFAULT FALSE"
    assert _pg(True, aurora_type="SMALLINT").clause == " DEFAULT 1"
    assert _pg(float("nan"), aurora_type="REAL").clause == ""
    assert _pg(float("inf"), aurora_type="REAL").reason


def test_default_null_on_a_not_null_column_is_omitted():
    assert _pg("NULL", nullable=False).clause == ""
    assert _my(None, nullable=False).clause == ""
    assert _pg("NULL", nullable=False).reason == ""


# --- Aurora MySQL: every recognized form -------------------------------------


@pytest.mark.parametrize(
    "value,source,aurora_type,clause",
    [
        ("CURRENT_TIMESTAMP", "mysql", "DATETIME", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP", "mysql", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP(6)", "mysql", "DATETIME(6)", " DEFAULT CURRENT_TIMESTAMP(6)"),
        ("current_timestamp()", "mariadb", "DATETIME", " DEFAULT CURRENT_TIMESTAMP"),
        ("NOW()", "mysql", "DATETIME", " DEFAULT CURRENT_TIMESTAMP"),
        # MySQL requires the default's precision to match the column's
        ("now()", "postgresql", "DATETIME(6)", " DEFAULT CURRENT_TIMESTAMP(6)"),
        ("CURRENT_TIMESTAMP", "postgresql", "DATETIME(6)", " DEFAULT CURRENT_TIMESTAMP(6)"),
        ("uuid()", "mysql", "CHAR(36)", " DEFAULT (UUID())"),
        ("(uuid())", "mysql", "VARCHAR(64)", " DEFAULT (UUID())"),
        ("gen_random_uuid()", "postgresql", "CHAR(36)", " DEFAULT (UUID())"),
        ("0", "postgresql", "INT", " DEFAULT 0"),
        ("-1.25", "postgresql", "DECIMAL(5,2)", " DEFAULT -1.25"),
        ("((0))", "sqlserver", "INT", " DEFAULT 0"),
        ("NULL", "postgresql", "VARCHAR(10)", " DEFAULT NULL"),
        ("true", "postgresql", "BOOLEAN", " DEFAULT 1"),
        ("false", "postgresql", "BOOLEAN", " DEFAULT 0"),
        ("'open'::character varying", "postgresql", "VARCHAR(255)", " DEFAULT 'open'"),
        ("'-1'::integer", "postgresql", "INT", " DEFAULT '-1'"),
        ("open", "mysql", "VARCHAR(20)", " DEFAULT 'open'"),
        ("0", "mysql", "BIGINT", " DEFAULT '0'"),  # a MySQL default is its literal text
        ("", "mysql", "VARCHAR(20)", " DEFAULT ''"),
        ("b'1'", "mysql", "BIT(1)", " DEFAULT b'1'"),
        ("CURRENT_DATE", "postgresql", "DATE", " DEFAULT (CURDATE())"),
        ("curdate()", "mysql", "DATE", " DEFAULT (CURDATE())"),
        ("(curdate())", "mysql", "DATE", " DEFAULT (CURDATE())"),
    ],
)
def test_mysql_recognized_forms(value, source, aurora_type, clause):
    result = _my(value, source=source, aurora_type=aurora_type)
    assert result.clause == clause
    assert result.reason == ""


def test_mysql_sequence_becomes_auto_increment_only_with_an_identity_slot():
    ok = _my("nextval('s'::regclass)", source="postgresql", aurora_type="BIGINT", identity_ok=True)
    assert ok.identity is True and ok.clause == ""
    no = _my("nextval('s'::regclass)", source="postgresql", aurora_type="BIGINT")
    assert no.identity is False and no.clause == "" and no.reason


@pytest.mark.parametrize(
    "value,source,aurora_type",
    [
        ("now()", "postgresql", "VARCHAR(30)"),  # not a DATETIME/TIMESTAMP column
        ("CURRENT_DATE", "postgresql", "VARCHAR(10)"),
        ("CURRENT_TIME", "postgresql", "TIME"),
        ("gen_random_uuid()", "postgresql", "BINARY(16)"),  # 36 characters do not fit
        ("'{}'::jsonb", "postgresql", "JSON"),  # JSON/TEXT/BLOB take no literal default
        ("'SELECT 1'::text", "postgresql", "LONGTEXT"),
        ("0", "postgresql", "BLOB"),
    ],
)
def test_mysql_defaults_the_column_cannot_take_are_dropped(value, source, aurora_type):
    result = _my(value, source=source, aurora_type=aurora_type)
    assert result.clause == "" and result.reason


# --- Cross-source forms -------------------------------------------------------


@pytest.mark.parametrize(
    "value,aurora_type,clause",
    [
        ("((0))", "INTEGER", " DEFAULT 0"),
        ("((-1))", "INTEGER", " DEFAULT -1"),
        ("((1))", "BOOLEAN", " DEFAULT TRUE"),
        ("((0))", "BOOLEAN", " DEFAULT FALSE"),
        ("(getdate())", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("(sysdatetime())", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("(CURRENT_TIMESTAMP)", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("(newid())", "UUID", " DEFAULT gen_random_uuid()"),
        ("(newsequentialid())", "UUID", " DEFAULT gen_random_uuid()"),
        ("(N'abc')", "VARCHAR(10)", " DEFAULT 'abc'"),
        ("('it''s')", "TEXT", " DEFAULT 'it''s'"),
        ("('')", "TEXT", " DEFAULT ''"),
        ("(NULL)", "TEXT", " DEFAULT NULL"),
    ],
)
def test_sqlserver_defaults(value, aurora_type, clause):
    result = _pg(value, source="sqlserver", aurora_type=aurora_type)
    assert result.clause == clause and result.reason == ""


@pytest.mark.parametrize(
    "value", ["(NEXT VALUE FOR [dbo].[order_seq])", "(NEXT VALUE FOR order_seq)"]
)
def test_sqlserver_sequence_becomes_identity(value):
    result = _pg(value, source="sqlserver", aurora_type="BIGINT", identity_ok=True)
    assert result.identity is True and result.clause == ""


def test_sqlserver_bit_default_outside_0_1_is_dropped_on_boolean():
    result = _pg("((2))", source="sqlserver", aurora_type="BOOLEAN")
    assert result.clause == "" and result.reason


@pytest.mark.parametrize(
    "value,aurora_type,clause",
    [
        ("SYSDATE\n", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("systimestamp ", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("0 ", "NUMERIC(10,0)", " DEFAULT 0"),
        ("'N'", "CHAR(1)", " DEFAULT 'N'"),
        ("''", "VARCHAR(10)", " DEFAULT NULL"),  # Oracle's empty string is NULL
        ("NULL", "VARCHAR(10)", " DEFAULT NULL"),
    ],
)
def test_oracle_defaults(value, aurora_type, clause):
    result = _pg(value, source="oracle", aurora_type=aurora_type)
    assert result.clause == clause and result.reason == ""


@pytest.mark.parametrize(
    "value", ["ORDER_SEQ.NEXTVAL", '"APP"."ORDER_SEQ".nextval', "app.order_seq.nextval\n"]
)
def test_oracle_sequence_becomes_identity(value):
    result = _pg(value, source="oracle", aurora_type="BIGINT", identity_ok=True)
    assert result.identity is True and result.clause == ""


# --- String literals stay one safe literal -------------------------------------

_HOSTILE_LITERALS = [
    "O'Brien",
    "a\\b",
    "x\\', evil INT, z TEXT -- ",
    "'); DROP TABLE x; --",
    "\\'\\'",
    "line\nbreak",
    "''''",
]


def _pg_literal_end(sql: str, standard_conforming_strings: bool) -> int:
    """Index just past the string literal ``sql`` starts with, as PostgreSQL lexes it.

    ``E'...'`` always treats a backslash as an escape; a plain ``'...'`` does
    only when standard_conforming_strings is off. ``''`` is a quote in both.
    """
    escape_string = sql.startswith("E'")
    escapes = escape_string or not standard_conforming_strings
    i = 2 if escape_string else 1
    assert sql[i - 1] == "'"
    while i < len(sql):
        ch = sql[i]
        if escapes and ch == "\\":
            i += 2
        elif ch == "'" and sql[i + 1 : i + 2] == "'":
            i += 2
        elif ch == "'":
            return i + 1
        else:
            i += 1
    raise AssertionError(f"unterminated literal: {sql!r}")


def _single_pg_literal(clause: str, rest: str = "") -> bool:
    """The clause is one literal (then ``rest``) with the setting on and off."""
    body = clause.removeprefix(" DEFAULT ")
    return all(body[_pg_literal_end(body, scs) :] == rest for scs in (True, False))


# The reviewer's repro, a lone backslash, and a backslash before a doubled quote.
_PG_BACKSLASH_DEFAULTS = [
    r"'a\''; DROP TABLE t; --'::text",
    r"'a\'::text",
    r"'\\'''::text",
]


@pytest.mark.parametrize("value", _PG_BACKSLASH_DEFAULTS)
def test_pg_backslash_literal_is_one_literal_under_both_string_settings(value):
    result = _pg(value, aurora_type="TEXT")
    assert result.clause.startswith(" DEFAULT E'")
    assert _single_pg_literal(result.clause, rest="::text")


def test_reviewer_repro_renders_as_an_escape_string():
    result = _pg(r"'a\''; DROP TABLE t; --'::text", aurora_type="TEXT")
    assert result.clause == r" DEFAULT E'a\\''; DROP TABLE t; --'::text"


def _single_literal(body: str, backslashes: bool) -> bool:
    """``body`` is exactly one quoted literal under the target's string rules."""
    if not (body.startswith("'") and body.endswith("'") and len(body) >= 2):
        return False
    inner = body[1:-1]
    if backslashes:
        inner = inner.replace("\\\\", "")
    inner = inner.replace("''", "")
    return "'" not in inner and (not backslashes or "\\" not in inner)


@pytest.mark.parametrize("text", _HOSTILE_LITERALS)
def test_mysql_source_literals_stay_one_literal_on_both_targets(text):
    pg = _pg(text, source="mysql", aurora_type="TEXT")
    assert _single_pg_literal(pg.clause)
    my = _my(text, source="mysql", aurora_type="VARCHAR(50)")
    assert _single_literal(my.clause.removeprefix(" DEFAULT "), backslashes=True)


def test_literal_escaping_per_target():
    # PostgreSQL: a literal with a backslash is an E'' string, whatever
    # standard_conforming_strings says; one without stays a plain literal.
    assert _pg("a'b\\c", source="mysql").clause == " DEFAULT E'a''b\\\\c'"
    assert _pg("a'b", source="mysql").clause == " DEFAULT 'a''b'"
    assert _my("a'b\\c", source="mysql").clause == " DEFAULT 'a''b\\\\c'"
    # a quoted source literal is unescaped once, then escaped for the target
    assert _my("'a''b\\c'::text", source="postgresql").clause == " DEFAULT 'a''b\\\\c'"


def test_nul_byte_literal_is_dropped():
    assert _pg("a\x00b", source="mysql").clause == ""


# --- Injection attempts never reach DDL raw -----------------------------------

_INJECTIONS = [
    ("postgresql", "0); DROP TABLE x; --"),
    ("postgresql", "nextval('a'); drop"),
    ("postgresql", "nextval('a'); DROP TABLE x; --'::regclass)"),
    ("postgresql", "nextval('a''; drop table x; --'::regclass)"),
    ("postgresql", "nextval('\"weird name\"'::regclass)"),
    ("postgresql", "'a'::text::character varying"),
    ("postgresql", "('now'::text)::date"),
    ("postgresql", "'x'::text); DROP TABLE t; --"),
    ("postgresql", "'x'::text; DROP TABLE t"),
    ("postgresql", "now(); drop table x"),
    ("postgresql", "now() -- comment"),
    ("postgresql", "gen_random_uuid() || 'x'"),
    ("postgresql", "1; DROP TABLE x"),
    ("postgresql", "0x41"),
    ("postgresql", "'a' || pg_sleep(10)"),
    ("postgresql", "E'\\x27; DROP TABLE x; --'"),
    ("sqlserver", "((0)); DROP TABLE x; --("),
    ("sqlserver", "(0)) OR ((1)"),
    ("sqlserver", "(NEXT VALUE FOR [dbo].[s]; DROP TABLE x)"),
    ("sqlserver", "(NEXT VALUE FOR [a b])"),
    ("oracle", "SEQ.NEXTVAL; DROP TABLE x"),
    ("oracle", "SYSDATE + 1"),
    ("oracle", "'a' || 'b'"),
    ("mysql", "(rand() * 100)"),
    ("mysql", "uuid()); DROP TABLE x; --"),
]


@pytest.mark.parametrize("target", [PG, MY])
@pytest.mark.parametrize("source,value", _INJECTIONS)
def test_injection_attempts_are_dropped_with_a_reason(source, value, target):
    result = translate_default(
        value, source_engine=source, target=target, aurora_type="BIGINT", identity_ok=True
    )
    assert result.clause == ""
    assert result.identity is False
    assert result.reason


def test_unallowlisted_cast_is_never_emitted():
    result = _pg("'abc'::pg_sleep(10)", aurora_type="TEXT")
    assert result.clause == " DEFAULT 'abc'"


# --- Unrecognized expressions are dropped, not quoted --------------------------


@pytest.mark.parametrize(
    "source,value",
    [
        ("postgresql", "lower('X'::text)"),
        ("postgresql", "clock_timestamp()"),
        ("postgresql", "(EXTRACT(epoch FROM now()))::integer"),
        ("postgresql", "array[]::integer[]"),
        ("sqlserver", "(getutcdate())"),
        ("sqlserver", "(CONVERT([bit],(0)))"),
        ("sqlserver", "(user_name())"),
        ("oracle", "USER"),
        ("oracle", "SYS_CONTEXT('USERENV','SESSION_USER')"),
        ("mysql", "concat('a','b')"),
        ("mysql", "(curdate() + interval 1 day)"),
    ],
)
def test_unrecognized_expressions_are_dropped(source, value):
    for target, aurora_type in ((PG, "TEXT"), (MY, "VARCHAR(50)")):
        result = translate_default(
            value, source_engine=source, target=target, aurora_type=aurora_type
        )
        assert result.clause == "", (target, result)
        assert result.reason
        assert value not in result.reason  # the caller quotes the source text, not us


# --- Type mismatches are dropped, not emitted ----------------------------------


@pytest.mark.parametrize("aurora_type", ["TIMESTAMP", "DATE", "TIME", "UUID", "JSONB", "INTEGER[]"])
def test_pg_number_on_a_non_numeric_column_is_dropped(aurora_type):
    result = _pg("((0))", source="sqlserver", aurora_type=aurora_type)
    assert result.clause == "" and result.reason


@pytest.mark.parametrize("aurora_type", ["DATETIME", "DATETIME(3)", "DATE", "TIME", "JSON"])
def test_mysql_number_on_a_temporal_column_is_dropped(aurora_type):
    result = _my("((0))", source="sqlserver", aurora_type=aurora_type)
    assert result.clause == "" and result.reason


def test_sqlserver_zero_on_datetime_repro_both_targets():
    pg = _pg("((0))", source="sqlserver", aurora_type="TIMESTAMP")
    my = _my("((0))", source="sqlserver", aurora_type="DATETIME")
    assert pg.clause == my.clause == ""
    assert "number" in pg.reason and "number" in my.reason


@pytest.mark.parametrize("aurora_type", ["BIGINT", "BOOLEAN", "UUID"])
def test_pg_current_timestamp_on_a_non_temporal_column_is_dropped(aurora_type):
    result = _pg("now()", aurora_type=aurora_type)
    assert result.clause == "" and result.reason


@pytest.mark.parametrize(
    "value,aurora_type,clause",
    [
        ("now()", "VARCHAR(40)", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_DATE", "TIMESTAMP", " DEFAULT CURRENT_DATE"),
        ("0", "VARCHAR(5)", " DEFAULT 0"),
        ("0", "NUMERIC(5,2)", " DEFAULT 0"),
    ],
)
def test_pg_compatible_types_keep_the_default(value, aurora_type, clause):
    assert _pg(value, aurora_type=aurora_type).clause == clause


def test_reasons_use_the_right_article():
    reasons = [
        _pg("gen_random_uuid()", aurora_type="INTEGER").reason,
        _pg("((0))", source="sqlserver", aurora_type="INTERVAL").reason,
        _pg("now()", aurora_type="UUID").reason,
        _pg("nextval('s'::regclass)", aurora_type="TEXT").reason,
    ]
    for reason in reasons:
        assert reason
        assert " a INTEGER" not in reason and " a INTERVAL" not in reason
        assert " an UUID" not in reason and " an Aurora" not in reason


# --- DB2 and unknown sources ----------------------------------------------------


@pytest.mark.parametrize(
    "value,aurora_type,clause",
    [
        ("CURRENT TIMESTAMP", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT_TIMESTAMP", "TIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        ("CURRENT DATE", "DATE", " DEFAULT CURRENT_DATE"),
        ("'abc'", "VARCHAR(10)", " DEFAULT 'abc'"),
        ("'it''s'", "VARCHAR(10)", " DEFAULT 'it''s'"),
        ("0", "INTEGER", " DEFAULT 0"),
        ("NULL", "INTEGER", " DEFAULT NULL"),
    ],
)
def test_db2_defaults(value, aurora_type, clause):
    result = _pg(value, source="db2", aurora_type=aurora_type)
    assert result.clause == clause and result.reason == ""


@pytest.mark.parametrize("value", ["NEXT VALUE FOR app.order_seq", "NEXTVAL FOR order_seq"])
def test_db2_sequence_becomes_identity(value):
    result = _pg(value, source="db2", aurora_type="BIGINT", identity_ok=True)
    assert result.identity is True


@pytest.mark.parametrize("value", ["abc", "CURRENT USER", "GENERATE_UNIQUE()", "'a' || 'b'"])
def test_db2_unrecognized_is_dropped(value):
    result = _pg(value, source="db2", aurora_type="VARCHAR(20)")
    assert result.clause == "" and result.reason


@pytest.mark.parametrize(
    "value,clause",
    [
        ("'abc'", " DEFAULT 'abc'"),
        ("'it''s'", " DEFAULT 'it''s'"),
        ("0", " DEFAULT 0"),
        ("NULL", " DEFAULT NULL"),
    ],
)
def test_unknown_source_keeps_only_unambiguous_literals(value, clause):
    for source in ("informix", ""):
        assert _pg(value, source=source, aurora_type="VARCHAR(10)").clause == clause


@pytest.mark.parametrize(
    "value", ["abc", "CURRENT_TIMESTAMP", "now()", "NEXT VALUE FOR s", "'a' || 'b'", "'a'b'"]
)
def test_unknown_source_drops_everything_else(value):
    result = _pg(value, source="informix", aurora_type="TEXT", identity_ok=True)
    assert result.clause == "" and result.reason and result.identity is False


# --- Deep nesting stays linear --------------------------------------------------


@pytest.mark.parametrize("source", ["sqlserver", "oracle", "db2", "postgresql", "mysql", "x"])
@pytest.mark.parametrize("shape", ["balanced", "unclosed", "unopened"])
def test_deeply_nested_parentheses_finish_fast_and_are_dropped(source, shape):
    depth = 50_000
    value = {
        "balanced": "(" * depth + "0" + ")" * depth,
        "unclosed": "(" * depth + "0",
        "unopened": "0" + ")" * depth,
    }[shape]
    results = []
    assert_fast(
        lambda: results.append(
            translate_default(value, source_engine=source, target=PG, aurora_type="INTEGER")
        ),
        0.05,
    )
    result = results[-1]
    if source == "mysql" and shape == "unopened":
        # a MySQL default is literal text: "0)))..." is one quoted literal
        assert _single_pg_literal(result.clause)
    else:
        assert result.clause == "" and result.reason


def test_parentheses_within_the_depth_limit_still_strip():
    value = "(" * 32 + "0" + ")" * 32
    assert _pg(value, source="sqlserver", aurora_type="INTEGER").clause == " DEFAULT 0"
    deeper = "(" * 33 + "0" + ")" * 33
    result = _pg(deeper, source="sqlserver", aurora_type="INTEGER")
    assert result.clause == "" and "32" in result.reason


def test_non_ascii_digits_are_not_numeric_defaults():
    from src.tools.schema.aurora_common.constraint_translator import translate_default

    for target in ("aurora_postgresql", "aurora_mysql"):
        result = translate_default(
            "((١٢))", source_engine="sqlserver", target=target, aurora_type="INTEGER"
        )
        assert "١٢" not in (result.clause or "")
