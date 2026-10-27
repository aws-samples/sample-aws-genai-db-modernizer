"""Aurora MySQL indexes on TEXT and BLOB columns carry a key prefix (#1623).

MySQL rejects ``CREATE INDEX ix ON posts (body)`` on a TEXT column (error 1170).
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import AgentIndex, AgentTable
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    generate_mysql_ddl,
    generate_pg_ddl,
    render_index,
)
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from src.tools.schema.aurora_common.sql_safety import SqlFragmentError


def _table(indexes, body_type="text") -> AgentTable:
    return AgentTable.model_validate(
        {
            "table_id": "blog.posts",
            "table_name": "posts",
            "row_count": 1,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "data_type": "bigint", "nullable": False},
                {"column_name": "title", "data_type": "varchar(100)", "nullable": True},
                {"column_name": "body", "data_type": body_type, "nullable": True},
            ],
            "indexes": indexes,
        }
    )


def _ddl(indexes, body_type="text"):
    return generate_mysql_ddl([_table(indexes, body_type)], source_engine="mysql")


def test_a_source_prefix_length_is_rendered():
    result = _ddl([{"index_name": "ix", "columns": ["body"], "is_unique": False,
                    "prefix_lengths": [100]}])  # fmt: skip
    assert "CREATE INDEX `ix` ON `posts` (`body`(100));" in result.full_ddl


def test_prefix_applies_to_its_own_column_in_a_composite_index():
    result = _ddl([{"index_name": "ix", "columns": ["title", "body"], "is_unique": False,
                    "prefix_lengths": [None, 50]}])  # fmt: skip
    assert "(`title`, `body`(50));" in result.full_ddl
    assert result.index_notes == []


@pytest.mark.parametrize("body_type", ["text", "blob", "longtext", "mediumblob"])
def test_a_lob_column_without_a_known_prefix_gets_the_default_and_a_note(body_type):
    result = _ddl([{"index_name": "ix", "columns": ["body"], "is_unique": False}], body_type)
    assert "(`body`(191));" in result.full_ddl
    (note,) = result.index_notes
    assert note["index"] == "ix" and "191" in note["reason"] and "body" in note["reason"]


def test_a_unique_index_without_a_known_prefix_is_not_made_unique_on_a_prefix():
    result = _ddl([{"index_name": "ux", "columns": ["body"], "is_unique": True}])
    assert "CREATE INDEX `ux`" in result.full_ddl
    assert "UNIQUE" not in result.full_ddl.split("CREATE", 2)[-1]
    assert "non-unique" in result.index_notes[0]["reason"]


def test_a_unique_index_with_a_source_prefix_stays_unique():
    result = _ddl([{"index_name": "ux", "columns": ["body"], "is_unique": True,
                    "prefix_lengths": [64]}])  # fmt: skip
    assert "CREATE UNIQUE INDEX `ux` ON `posts` (`body`(64));" in result.full_ddl
    assert result.index_notes == []


@pytest.mark.passes_on_main
def test_a_varchar_index_gets_no_prefix_and_no_note():
    result = _ddl([{"index_name": "ix", "columns": ["title"], "is_unique": False}])
    assert "ON `posts` (`title`);" in result.full_ddl
    assert result.index_notes == []


def test_an_input_written_before_the_field_still_loads():
    index = AgentIndex.model_validate({"index_name": "ix", "columns": ["body"], "is_unique": False})
    assert index.prefix_lengths is None


@pytest.mark.passes_on_main
def test_the_prefix_never_reaches_aurora_postgresql():
    table = _table([{"index_name": "ix", "columns": ["body"], "is_unique": False,
                     "prefix_lengths": [100]}])  # fmt: skip
    ddl = generate_pg_ddl([table], source_engine="mysql").full_ddl
    assert 'ON "posts" ("body");' in ddl


def test_render_index_refuses_a_prefix_that_is_not_a_positive_integer():
    for bad in (0, -1, "10); DROP TABLE x; --"):
        with pytest.raises(SqlFragmentError):
            render_index(MYSQL, "posts", "ix", ["body"], prefix_lengths=[bad])  # type: ignore[list-item]


def test_the_projection_carries_prefix_lengths_from_the_collector():
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.schema_design_input import project_schema_design_input
    from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
    from tests.unit.agents.test_group_input import _analysis

    collector = get_ecommerce_collector_output()
    collector["database_schema"]["tables"][0]["indexes"] = [
        {"index_name": "ix", "columns": ["name"], "is_unique": False, "prefix_lengths": [100]}
    ]
    contract = CollectorOutputContract.model_validate(collector)
    analysis = AnalysisOutputContract.model_validate(_analysis(collector))
    agent, _, _ = project_schema_design_input(contract, analysis)
    assert agent.tables[0].indexes[0].prefix_lengths == [100]  # type: ignore[index]


# ---------------------------------------------------------------------------
# The 3072-byte key limit (4 bytes per character on utf8mb4)
# ---------------------------------------------------------------------------


def _wide_table(columns: dict[str, str], indexes: list[dict]) -> AgentTable:
    return AgentTable.model_validate(
        {
            "table_id": "blog.wide",
            "table_name": "wide",
            "row_count": 1,
            "primary_key": ["id"],
            "columns": [{"column_name": "id", "data_type": "bigint", "nullable": False}]
            + [{"column_name": n, "data_type": t, "nullable": True} for n, t in columns.items()],
            "indexes": indexes,
        }
    )


def _key_bytes(statement: str, widths: dict[str, int]) -> int:
    """Key bytes of a rendered CREATE INDEX: prefixed parts by prefix, the rest by width."""
    import re

    total = 0
    for name, prefix in re.findall(r"`(\w+)`(?:\((\d+)\))?", statement.split(" ON ", 1)[1])[1:]:
        total += (int(prefix) if prefix else widths[name]) * 4
    return total


def test_five_text_columns_share_the_key_limit():
    cols = {f"c{i}": "text" for i in range(5)}
    index = {"index_name": "ix", "columns": list(cols), "is_unique": False}
    result = generate_mysql_ddl([_wide_table(cols, [index])], source_engine="mysql")
    (stmt,) = result.tables[0].index_sql
    assert _key_bytes(stmt, {}) <= 3072
    assert "`c0`(153)" in stmt and "(191)" not in stmt
    assert any("within 3072 bytes" in n["reason"] for n in result.index_notes)


def test_text_columns_share_what_two_varchar_255_parts_leave():
    cols = {"a": "text", "b": "text", "v1": "varchar(255)", "v2": "varchar(255)"}
    index = {"index_name": "ix", "columns": list(cols), "is_unique": False}
    result = generate_mysql_ddl([_wide_table(cols, [index])], source_engine="mysql")
    (stmt,) = result.tables[0].index_sql
    assert _key_bytes(stmt, {"v1": 255, "v2": 255}) <= 3072
    assert "`a`(129)" in stmt and "`b`(129)" in stmt


def test_a_source_prefix_too_long_for_utf8mb4_is_cut_and_a_unique_index_is_downgraded():
    cols = {"body": "text"}
    index = {"index_name": "ux", "columns": ["body"], "is_unique": True, "prefix_lengths": [1000]}
    result = generate_mysql_ddl([_wide_table(cols, [index])], source_engine="mysql")
    (stmt,) = result.tables[0].index_sql
    assert stmt == "CREATE INDEX `ux` ON `wide` (`body`(768));"
    assert "non-unique" in result.index_notes[0]["reason"]


def test_a_key_that_cannot_fit_is_commented_out_with_a_note():
    cols = {f"v{i}": "varchar(255)" for i in range(3)} | {"body": "text"}
    index = {"index_name": "ix", "columns": list(cols), "is_unique": False}
    result = generate_mysql_ddl([_wide_table(cols, [index])], source_engine="mysql")
    (stmt,) = result.tables[0].index_sql
    assert stmt.startswith("-- Index wide.ix not created")
    assert "CREATE INDEX" not in result.full_ddl.replace(stmt, "")
    assert result.index_notes[0]["kind"] == "key_prefix"


@pytest.mark.passes_on_main
def test_a_prefix_not_shorter_than_the_column_is_dropped():
    cols = {"slug": "varchar(50)"}
    index = {"index_name": "ix", "columns": ["slug"], "is_unique": False, "prefix_lengths": [100]}
    result = generate_mysql_ddl([_wide_table(cols, [index])], source_engine="mysql")
    assert result.tables[0].index_sql == ["CREATE INDEX `ix` ON `wide` (`slug`);"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("kind", ["fulltext", "spatial"])
def test_fulltext_and_spatial_indexes_get_no_prefix(kind):
    index = {"index_name": "ft", "columns": ["body"], "is_unique": False, "index_type": kind}
    result = generate_mysql_ddl([_wide_table({"body": "text"}, [index])], source_engine="mysql")
    assert "(191)" not in result.full_ddl and "`body`(" not in result.full_ddl
    assert [n for n in result.index_notes if n.get("kind") == "key_prefix"] == []


def test_a_primary_key_keeps_its_source_prefix():
    table = AgentTable.model_validate(
        {
            "table_id": "blog.pages",
            "table_name": "pages",
            "row_count": 1,
            "primary_key": ["url"],
            "columns": [{"column_name": "url", "data_type": "text", "nullable": False}],
            "indexes": [
                {"index_name": "PRIMARY", "columns": ["url"], "is_unique": True,
                 "is_primary": True, "prefix_lengths": [255]}  # fmt: skip
            ],
        }
    )
    ddl = generate_mysql_ddl([table], source_engine="mysql").full_ddl
    assert "PRIMARY KEY (`url`(255))" in ddl


# ---------------------------------------------------------------------------
# Design deltas and the deliverables
# ---------------------------------------------------------------------------


def _base(indexes, columns=None):
    cols = columns or {"slug": "text", "body": "text"}
    return AuroraDesignBase(
        engine="aurora_mysql", job_id="j", source_database="blog", source_engine="mysql",
        tables=[_wide_table(cols, indexes)],
    )  # fmt: skip


def _merge(base, **table):
    delta = {"delta_version": "1.0", "tables": [{"table_name": "wide", **table}]}
    result = merge_design_delta(base, delta)
    assert result.errors == []
    return result.output


def test_a_delta_index_on_a_lob_column_gets_a_prefix_and_a_note():
    output = _merge(_base([]), add_indexes=[{"index_name": "d", "columns": ["body"]}])
    assert "CREATE INDEX `d` ON `wide` (`body`(191));" in output["table_definitions"][0]["indexes"]
    (note,) = [n for n in output["app_layer_notes"] if n["feature"] == "index_key_prefix"]
    assert note["source_object"] == "wide.d" and "191" in note["recommendation"]


def test_a_modified_index_keeps_its_source_prefix():
    base = _base([{"index_name": "ix", "columns": ["slug"], "is_unique": False,
                   "prefix_lengths": [50]}])  # fmt: skip
    output = _merge(
        base, modify_indexes=[{"index_name": "ix", "columns": ["slug", "body"], "unique": False}]
    )
    assert output["table_definitions"][0]["indexes"] == [
        "CREATE INDEX `ix` ON `wide` (`slug`(50), `body`(191));"
    ]


def test_modifying_a_unique_prefix_index_keeps_it_unique_and_does_not_fail():
    base = _base([{"index_name": "ux", "columns": ["slug"], "is_unique": True,
                   "prefix_lengths": [100]}])  # fmt: skip
    output = _merge(
        base, modify_indexes=[{"index_name": "ux", "columns": ["slug"], "unique": True}]
    )
    assert output["table_definitions"][0]["indexes"] == [
        "CREATE UNIQUE INDEX `ux` ON `wide` (`slug`(100));"
    ]
    assert [n for n in output["app_layer_notes"] if n["feature"] == "index_key_prefix"] == []


def test_a_unique_index_losing_uniqueness_is_in_the_schema_output_notes():
    base = _base([{"index_name": "ux", "columns": ["slug"], "is_unique": True}])
    output = _merge(base)
    (note,) = [n for n in output["app_layer_notes"] if n["feature"] == "index_key_prefix"]
    assert note["source_object"] == "wide.ux"
    assert "non-unique" in note["recommendation"]


@pytest.mark.passes_on_main
def test_a_removed_index_takes_its_note_with_it():
    base = _base([{"index_name": "ix", "columns": ["slug"], "is_unique": False}])
    output = _merge(base, remove_indexes=["ix"])
    assert [n for n in output["app_layer_notes"] if n["feature"] == "index_key_prefix"] == []


def test_the_design_view_shows_the_index_as_rendered():
    from src.tools.schema.aurora_common.ddl_generator import rendered_key

    base = _base([{"index_name": "ux", "columns": ["slug"], "is_unique": True}])
    columns = base.generate().tables[0].columns
    unique, prefixes = rendered_key(base.tables[0].indexes[0], base.dialect, columns)
    assert (unique, prefixes) == (False, [191])


# ---------------------------------------------------------------------------
# Review round 2: the index count, primary key notes, key limit notes
# ---------------------------------------------------------------------------


def _unfit_base():
    cols = {f"v{i}": "varchar(255)" for i in range(3)} | {"body": "text"}
    indexes = [
        {"index_name": "ok", "columns": ["v0"], "is_unique": False},
        {"index_name": "wide", "columns": list(cols), "is_unique": False},
    ]
    return AuroraDesignBase(
        engine="aurora_mysql", job_id="j", source_database="blog", source_engine="mysql",
        tables=[_wide_table(cols, indexes)],
    )  # fmt: skip


def test_a_commented_out_index_is_not_counted_in_the_schema_summary():
    from src.agents.referee.synthesis_handler import _build_aurora_schema_summary

    output = merge_design_delta(_unfit_base(), {"delta_version": "1.0"}).output
    assert output["table_definitions"][0]["indexes"] == ["CREATE INDEX `ok` ON `wide` (`v0`);"]
    assert "-- Index wide.wide not created: its key parts" in output["generated_ddl"]
    summary = _build_aurora_schema_summary("aurora_mysql", output)
    assert summary["index_count"] == 1 and summary["tables"][0]["index_count"] == 1


def test_the_comment_does_not_repeat_itself():
    result = generate_mysql_ddl(_unfit_base().tables, source_engine="mysql")
    assert "not created: Index not created" not in result.full_ddl
    assert "not created: its key parts" in result.full_ddl
    assert result.index_notes[0]["reason"].startswith("Index not created: its key parts")


def _pk_table(type_: str, prefix: int | None, extra_cols: dict[str, str] | None = None):
    index = {"index_name": "PRIMARY", "columns": ["url"], "is_unique": True, "is_primary": True}
    if prefix is not None:
        index["prefix_lengths"] = [prefix]
    cols = {"url": type_} | (extra_cols or {})
    return AgentTable.model_validate(
        {
            "table_id": "blog.pages", "table_name": "pages", "row_count": 1,
            "primary_key": ["url"], "indexes": [index],
            "columns": [{"column_name": n, "data_type": t, "nullable": False}
                        for n, t in cols.items()],
        }
    )  # fmt: skip


def test_a_shortened_primary_key_prefix_is_noted_as_stricter_than_the_source():
    result = generate_mysql_ddl([_pk_table("text", 1000)], source_engine="mysql")
    assert "PRIMARY KEY (`url`(768))" in result.full_ddl
    (note,) = result.index_notes
    assert note["index"] == "PRIMARY" and note["kind"] == "key_prefix"
    assert "stricter" in note["reason"] and "duplicate" in note["reason"]


def test_a_text_primary_key_with_no_recorded_prefix_gets_a_note_and_no_default():
    result = generate_mysql_ddl([_pk_table("text", None)], source_engine="mysql")
    assert "PRIMARY KEY (`url`)" in result.full_ddl and "(191)" not in result.full_ddl
    (note,) = result.index_notes
    assert "needs one" in note["reason"] and "url" in note["reason"]


@pytest.mark.passes_on_main
def test_a_primary_key_with_a_recorded_prefix_that_fits_has_no_note():
    result = generate_mysql_ddl([_pk_table("text", 255)], source_engine="mysql")
    assert result.index_notes == []


def test_a_single_varchar_key_over_the_limit_is_noted():
    index = {"index_name": "ix", "columns": ["big"], "is_unique": False}
    result = generate_mysql_ddl(
        [_wide_table({"big": "varchar(1000)"}, [index])], source_engine="mysql"
    )
    (note,) = result.index_notes
    assert "4000 bytes" in note["reason"] and "1071" in note["reason"]
