"""Tests for column DEFAULT resolution across source-engine shapes.

The point of this module is that ``default_value`` arrives in a different shape
from each source engine, and the previous seven-entry allowlist turned everything
it did not recognise into a string literal. Two failure modes came from that, and
they are tested separately because they need different remedies:

* a type mismatch the target rejects at CREATE TABLE, e.g. ``DEFAULT '((0))'``
  on an integer column — loud, and blocked at provisioning;
* a clause the target accepts, e.g. ``DEFAULT 'gen_random_uuid()'``, after which
  every row carries that literal text and a load test measures a schema nobody
  would deploy.

The second is the one worth the machinery. An unresolved expression therefore
emits no DEFAULT and raises a residual, because a missing default is visible in
both the DDL and the residual list while a wrong literal is invisible.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentTable, NormalizedDataType
from src.tools.schema.aurora_common.constraint_translator import (
    mysql_escape_literal,
    pg_escape_literal,
    resolve_default,
)
from src.tools.schema.aurora_common.ddl_generator import MYSQL, POSTGRES, generate_pg_ddl


def _pg(value: object):
    return resolve_default(value, escape_literal=pg_escape_literal)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Scalars, which were already correct and must stay so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (42, " DEFAULT 42"),
        (3.5, " DEFAULT 3.5"),
        (True, " DEFAULT TRUE"),
        (False, " DEFAULT FALSE"),
        ("active", " DEFAULT 'active'"),
        ("O'Brien", " DEFAULT 'O''Brien'"),
    ],
)
def test_scalars_and_plain_literals(value: object, expected: str) -> None:
    result = _pg(value)

    assert result.clause == expected
    assert result.needs_judgment is False


@pytest.mark.parametrize(
    "raw",
    ["CURRENT_TIMESTAMP", "current_timestamp", "NOW()", "now()", "now( )", "NULL", "TRUE"],
)
def test_portable_keywords_pass_through_whitespace_and_case_insensitively(raw: str) -> None:
    """``now( )`` previously failed the allowlist on one space and was quoted."""
    result = _pg(raw)

    assert result.clause.startswith(" DEFAULT ")
    assert "'" not in result.clause
    assert result.needs_judgment is False


# ---------------------------------------------------------------------------
# Per-engine source shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # SQL Server wraps definitions in parentheses, one or two deep.
        ("((0))", " DEFAULT 0"),
        ("(( -3.5 ))", " DEFAULT -3.5"),
        ("('pending')", " DEFAULT 'pending'"),
        ("(getdate())", " DEFAULT CURRENT_TIMESTAMP"),
        ("(getutcdate())", " DEFAULT CURRENT_TIMESTAMP"),
        # Oracle reports string defaults already quoted, often with trailing space.
        ("'N' ", " DEFAULT 'N'"),
        ("SYSDATE", " DEFAULT CURRENT_TIMESTAMP"),
        ("SYSTIMESTAMP", " DEFAULT CURRENT_TIMESTAMP"),
        # PostgreSQL reports literals with a cast restating the column's type.
        ("'pending'::character varying", " DEFAULT 'pending'"),
        ("'x'::text", " DEFAULT 'x'"),
    ],
)
def test_source_engine_wrapping_is_unwrapped_not_quoted(raw: str, expected: str) -> None:
    result = _pg(raw)

    assert result.clause == expected
    assert result.needs_judgment is False


def test_numeric_text_is_not_quoted() -> None:
    """``DEFAULT '0'`` on an integer column is a type mismatch, not a default."""
    assert _pg("((0))").clause == " DEFAULT 0"
    assert "'" not in _pg("((0))").clause


def test_cast_literal_is_not_double_escaped() -> None:
    """The captured literal is still source-escaped and must be unescaped first."""
    result = _pg("'it''s'::text")

    assert result.clause == " DEFAULT 'it''s'"


# ---------------------------------------------------------------------------
# The judgment channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "nextval('orders_id_seq'::regclass)",
        "my_seq.NEXTVAL",
        "my_seq.nextval",
        "(a) + (b)",
        "some_unknown_function(1, 2)",
    ],
)
def test_unresolvable_expressions_emit_no_default_and_flag(raw: str) -> None:
    """Emitting nothing is the safe choice; emitting a literal is the silent one."""
    result = _pg(raw)

    assert result.clause == ""
    assert result.needs_judgment is True
    assert result.source_expression == raw.strip()
    assert raw.strip() in result.reason


def test_unresolved_default_reaches_the_residual_list() -> None:
    """The judgment signal has to survive into the artifact the designer reads."""
    table = AgentTable(
        table_id="t",
        table_name="orders",
        row_count=1,
        columns=[
            AgentColumn(
                column_name="ref",
                nullable=True,
                normalized_data_type=NormalizedDataType.string,
                max_length=50,
                default_value="nextval('s'::regclass)",
            )
        ],
    )

    result = generate_pg_ddl([table])

    assert "DEFAULT" not in result.full_ddl
    assert len(result.residuals) == 1
    assert result.residuals[0]["column"] == "ref"
    assert result.residuals[0]["source_default"] == "nextval('s'::regclass)"


def test_identity_column_never_carries_a_default() -> None:
    """Mutually exclusive, and the default must not become a residual either."""
    table = AgentTable(
        table_id="t",
        table_name="orders",
        row_count=1,
        columns=[
            AgentColumn(
                column_name="id",
                nullable=False,
                normalized_data_type=NormalizedDataType.integer,
                is_auto_increment=True,
                default_value="nextval('orders_id_seq'::regclass)",
            )
        ],
        primary_key=["id"],
    )

    result = generate_pg_ddl([table])

    # "GENERATED BY DEFAULT AS IDENTITY" contains the word, so assert on the
    # clause and on the source expression instead.
    assert "DEFAULT nextval" not in result.full_ddl
    assert "nextval" not in result.full_ddl
    assert "IDENTITY" in result.full_ddl
    assert result.residuals == []


# ---------------------------------------------------------------------------
# Engine-specific rendering
# ---------------------------------------------------------------------------


def test_backslash_is_escaped_for_mysql_but_not_postgres() -> None:
    """MySQL reads a backslash as an escape character; PostgreSQL does not.

    With standard_conforming_strings on — the Aurora default — a backslash is an
    ordinary character in PostgreSQL. Sharing the PostgreSQL escaper with MySQL
    turned a source default of ``\\n`` into a newline.
    """
    literal = "\\n"

    assert resolve_default(literal, escape_literal=pg_escape_literal).clause == " DEFAULT '\\n'"
    assert (
        resolve_default(literal, escape_literal=mysql_escape_literal).clause == " DEFAULT '\\\\n'"
    )


@pytest.mark.parametrize(
    ("raw", "pg_expected", "mysql_expected"),
    [
        ("newid()", " DEFAULT gen_random_uuid()", " DEFAULT UUID()"),
        ("NEWID()", " DEFAULT gen_random_uuid()", " DEFAULT UUID()"),
    ],
)
def test_uuid_generator_translates_per_target_engine(
    raw: str, pg_expected: str, mysql_expected: str
) -> None:
    """The same source expression has different names on the two targets."""
    assert POSTGRES.resolve_default(raw).clause == pg_expected
    assert MYSQL.resolve_default(raw).clause == mysql_expected


def test_dialect_binds_its_own_escaper() -> None:
    assert POSTGRES.resolve_default("a\\b").clause == " DEFAULT 'a\\b'"
    assert MYSQL.resolve_default("a\\b").clause == " DEFAULT 'a\\\\b'"


def test_empty_string_default_is_preserved_as_a_literal() -> None:
    """An empty string is a real default and is not the same as having none."""
    result = _pg("")

    assert result.clause == " DEFAULT ''"
    assert result.needs_judgment is False
