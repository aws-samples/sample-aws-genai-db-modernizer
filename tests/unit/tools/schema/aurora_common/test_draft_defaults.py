"""Column defaults in the Aurora draft: MySQL backslash escaping, translated and dropped defaults."""

from __future__ import annotations

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.constraint_translator import translate_default
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
    # E'' with the backslash doubled reads as one backslash whatever
    # standard_conforming_strings is set to
    assert (
        translate_default(
            "a\\b", source_engine="mysql", target="aurora_postgresql", aurora_type="TEXT"
        ).clause
        == " DEFAULT E'a\\\\b'"
    )
    ddl = generate_pg_ddl(
        [_table(_col("note", "text", "'a\\b'::text"))], source_engine="postgresql"
    ).full_ddl
    assert "\"note\" TEXT DEFAULT E'a\\\\b'::text" in ddl


def test_pg_expression_defaults_become_real_defaults():
    table = _table(
        _col("ids", "_int4", "'{}'::integer[]"),
        _col("meta", "jsonb", "'{}'::jsonb"),
        _col("status", "character varying", "'open'::character varying"),
        _col("created_at", "timestamp", "now()"),
        _col("flag", "bool", "false"),
        _col("n", "int4", "0"),
    )
    result = generate_pg_ddl([table], source_engine="postgresql")
    ddl = result.full_ddl
    assert "\"ids\" INTEGER[] DEFAULT '{}'::integer[]" in ddl
    assert "\"meta\" JSONB DEFAULT '{}'::jsonb" in ddl
    assert "\"status\" VARCHAR DEFAULT 'open'" in ddl
    assert '"created_at" TIMESTAMP DEFAULT CURRENT_TIMESTAMP' in ddl
    assert '"flag" BOOLEAN DEFAULT FALSE' in ddl
    assert '"n" INTEGER DEFAULT 0' in ddl
    assert result.column_notes == []


def test_unrecognized_default_is_dropped_with_a_note_and_a_merge_warning():
    hostile = "lower('X'::text)\n); DROP TABLE t; --"
    table = _table(
        _col("slug", "text", "lower('X'::text)"),
        _col("evil", "text", hostile),
        _col("status", "character varying", "'open'::character varying"),
    )
    result = generate_pg_ddl([table], source_engine="postgresql")
    assert [(n["column"], n["kind"]) for n in result.column_notes] == [
        ("slug", "default"),
        ("evil", "default"),
    ]
    assert '"slug" TEXT,' in result.full_ddl and "lower(" not in result.full_ddl
    assert "DROP" not in result.full_ddl
    assert "drops it" in result.column_notes[0]["reason"]
    # the source text is echoed through excerpt(): repr'd, so no raw newline
    assert repr(hostile[:80])[:20] in result.column_notes[1]["reason"]
    assert "\n" not in result.column_notes[1]["reason"]

    base = AuroraDesignBase(
        engine="aurora_postgresql",
        job_id="j",
        source_database="db",
        source_engine="postgresql",
        tables=[table],
    )
    merged = merge_design_delta(base, {"delta_version": "1.0"})
    assert merged.errors == []
    assert any("source DEFAULT" in w and "t.slug" in w for w in merged.warnings)
    assert "lower(" not in merged.output["generated_ddl"]


def test_sequence_default_without_auto_increment_becomes_identity():
    table = AgentTable.model_validate(
        {
            "table_id": "db.orders",
            "table_name": "orders",
            "row_count": 1,
            "primary_key": ["id"],
            "columns": [
                {
                    "column_name": "id",
                    "data_type": "bigint",
                    "nullable": False,
                    "default_value": "(NEXT VALUE FOR [dbo].[order_seq])",
                    "normalized_data_type": "integer",
                },
                {
                    "column_name": "ref",
                    "data_type": "bigint",
                    "nullable": True,
                    "default_value": "(NEXT VALUE FOR [dbo].[ref_seq])",
                    "normalized_data_type": "integer",
                },
            ],
        }
    )
    pg = generate_pg_ddl([table], source_engine="sqlserver")
    assert '"id" BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL' in pg.full_ddl
    # identity implies NOT NULL, so a nullable column keeps no default instead
    ref = next(ln for ln in pg.full_ddl.splitlines() if '"ref"' in ln)
    assert "IDENTITY" not in ref and "DEFAULT" not in ref
    assert "NEXT VALUE" not in pg.full_ddl
    notes = {n["column"]: n["reason"] for n in pg.column_notes}
    assert "IDENTITY" in notes["id"] and "drops it" in notes["ref"]

    # MySQL allows one AUTO_INCREMENT column, and it must be the key
    my = generate_mysql_ddl([table], source_engine="sqlserver")
    assert "`id` BIGINT AUTO_INCREMENT NOT NULL" in my.full_ddl
    ref = next(ln for ln in my.full_ddl.splitlines() if "`ref`" in ln)
    assert "AUTO_INCREMENT" not in ref and "DEFAULT" not in ref
    assert "drops it" in next(n for n in my.column_notes if n["column"] == "ref")["reason"]


def test_auto_increment_columns_keep_no_default_and_no_note():
    table = _table(
        {
            "column_name": "seq_id",
            "data_type": "bigint",
            "nullable": False,
            "default_value": "nextval('a'); drop",
            "is_auto_increment": True,
        }
    )
    result = generate_pg_ddl([table], source_engine="postgresql")
    assert "drop" not in result.full_ddl and "nextval" not in result.full_ddl
    assert result.column_notes == []


def test_mysql_target_drops_literal_defaults_on_json_and_text():
    table = _table(_col("tools", "json", "'[]'::json"), _col("q", "text", "'SELECT 1'::text"))
    result = generate_mysql_ddl([table], source_engine="postgresql")
    assert "DEFAULT" not in result.full_ddl
    assert {n["column"] for n in result.column_notes} == {"tools", "q"}


def test_mysql_target_matches_the_column_precision_for_current_timestamp():
    table = _table(_col("created_at", "timestamp", "now()"))
    ddl = generate_mysql_ddl([table], source_engine="postgresql").full_ddl
    assert "`created_at` DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)" in ddl


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
    result = generate_pg_ddl(
        [_table(_col("note", "text", "'a\\b'::text"))], source_engine="postgresql"
    )
    assert result.residuals == []
    assert "DEFAULT E'a\\\\b'::text" in result.full_ddl
