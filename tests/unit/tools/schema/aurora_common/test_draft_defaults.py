"""Column defaults in the Aurora draft: MySQL backslash escaping, expression-default notes."""

from __future__ import annotations

import pytest

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.constraint_translator import (
    default_clause,
    is_expression_default,
)
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl, generate_pg_ddl
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

_HOSTILE = "x\\', evil INT, z TEXT -- "


def _table(*columns) -> AgentTable:
    return AgentTable.model_validate(
        {
            "table_id": "db.t",
            "table_name": "t",
            "row_count": 1,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "data_type": "bigint", "nullable": False},
                *columns,
            ],
        }
    )


def _col(name: str, data_type: str, default) -> dict:
    return {"column_name": name, "data_type": data_type, "nullable": True, "default_value": default}


def test_mysql_default_backslash_cannot_close_the_literal():
    ddl = generate_mysql_ddl(
        [_table(_col("note", "varchar(50)", _HOSTILE))], source_engine="mysql"
    ).full_ddl

    # \ doubled and ' doubled: one literal, no injected column
    assert "`note` VARCHAR(50) DEFAULT 'x\\\\'', evil INT, z TEXT -- '" in ddl
    line = next(ln for ln in ddl.splitlines() if "`note`" in ln)
    body = line.split("DEFAULT ", 1)[1].rstrip(",")
    # Under MySQL's default sql_mode, \\ is a backslash and '' a quote: the literal
    # spans the whole default and ends at the last character.
    assert body.startswith("'") and body.endswith("'")
    inner = body[1:-1].replace("\\\\", "").replace("''", "")
    assert "'" not in inner and "\\" not in inner


def test_pg_default_keeps_backslashes_literal():
    # standard_conforming_strings: a backslash is an ordinary character
    assert default_clause("a\\b") == " DEFAULT 'a\\b'"
    ddl = generate_pg_ddl(
        [_table(_col("note", "text", "a\\b"))], source_engine="postgresql"
    ).full_ddl
    assert "\"note\" TEXT DEFAULT 'a\\b'" in ddl


@pytest.mark.parametrize(
    "value,expected",
    [
        ("'{}'::integer[]", True),
        ("gen_random_uuid()", True),
        ("'open'::character varying", True),
        ("now()", False),  # rendered as a keyword
        ("CURRENT_TIMESTAMP", False),
        ("open", False),
        (0, False),
        (None, False),
    ],
)
def test_expression_defaults_are_detected(value, expected):
    assert is_expression_default(value) is expected


def test_expression_defaults_get_a_column_note_and_a_merge_warning():
    table = _table(
        _col("ids", "_int4", "'{}'::integer[]"),
        _col("status", "character varying", "open"),
    )
    result = generate_pg_ddl([table], source_engine="postgresql")
    assert [(n["column"], n["kind"]) for n in result.column_notes] == [("ids", "default")]
    assert "'{}'::integer[]" in result.column_notes[0]["reason"]

    base = AuroraDesignBase(
        engine="aurora_postgresql",
        job_id="j",
        source_database="db",
        source_engine="postgresql",
        tables=[table],
    )
    merged = merge_design_delta(base, {"delta_version": "1.0"})
    assert merged.errors == []
    assert any("SQL-expression DEFAULT" in w and "t.ids" in w for w in merged.warnings)


def test_timestamptz_to_mysql_adds_a_utc_app_layer_note():
    table = _table(
        {"column_name": "created_at", "data_type": "timestamp with time zone", "nullable": False}
    )
    base = AuroraDesignBase(
        engine="aurora_mysql",
        job_id="j",
        source_database="db",
        source_engine="postgresql",
        tables=[table],
    )

    output = merge_design_delta(base, {"delta_version": "1.0"}).output

    contract = AuroraMySQLModelOutputContract.model_validate(output)
    [note] = contract.app_layer_notes
    assert note.feature == "time_zone" and note.source_object == "t.created_at"
    assert "UTC" in note.recommendation
    assert "`created_at` DATETIME(6) NOT NULL" in contract.generated_ddl


def test_pg_to_mysql_json_with_default_is_a_residual_not_invalid_ddl():
    table = _table(_col("tools", "json", "'[]'::json"))
    result = generate_mysql_ddl([table], source_engine="postgresql")
    assert [r["column"] for r in result.residuals] == ["tools"]


def test_mysql_backslash_default_is_a_residual_for_sql_mode_review():
    result = generate_mysql_ddl(
        [_table(_col("note", "varchar(50)", "a\\b"))], source_engine="mysql"
    )
    [residual] = result.residuals
    assert residual["column"] == "note" and "NO_BACKSLASH_ESCAPES" in residual["reason"]
    assert "DEFAULT 'a\\\\b'" in result.full_ddl  # still escaped, type unchanged
    assert "SET SESSION" not in result.full_ddl


def test_pg_backslash_default_is_not_a_residual():
    result = generate_pg_ddl([_table(_col("note", "text", "a\\b"))], source_engine="postgresql")
    assert result.residuals == []
