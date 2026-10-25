"""A column's text type is read in the facts layer, in the source dialect (#1057).

The substring-match scan penalty tells a word search from a substring scan by the type
of the column a leading-wildcard ``LIKE`` tests (#942). That test lives next to
:func:`is_timestamp_type` as :func:`is_text_type`: it reads the collector's normalized
type first, then the native type in each source engine's own spellings.
"""

from __future__ import annotations

import pytest

from src.tools.sql.query_facts import is_text_type


def _column(data_type: str | None, normalized: str | None = None) -> dict:
    return {"column_name": "c", "data_type": data_type, "normalized_data_type": normalized}


@pytest.mark.parametrize(
    "engine,text_types,other_types",
    [
        (
            "mysql",
            ["varchar(255)", "char(36)", "tinytext", "longtext", "enum('a','b')", "set('x')"],
            ["int", "json", "blob", "varbinary(16)", "datetime"],
        ),
        (
            "mariadb",
            ["VARCHAR(10)", "mediumtext", "enum('open','closed')"],
            ["bigint", "binary(16)"],
        ),
        (
            "postgresql",
            ["text", "varchar", "character varying(40)", "bpchar", "citext", "name"],
            ["_text", "text[]", "_varchar", "tsvector", "jsonb", "bytea", "uuid", "int4"],
        ),
        (
            "sqlserver",
            ["nvarchar(max)", "NVARCHAR", "ntext", "varchar(50)", "nchar(2)", "sysname", "text"],
            ["int", "uniqueidentifier", "varbinary(max)", "xml", "datetime2"],
        ),
        (
            "oracle",
            ["VARCHAR2(100)", "NVARCHAR2(20)", "CLOB", "NCLOB", "CHAR(1)", "LONG"],
            ["NUMBER(10)", "BLOB", "RAW(16)", "DATE", "LONG RAW"],
        ),
    ],
)
def test_each_source_engine_reads_its_own_text_types(engine, text_types, other_types):
    for data_type in text_types:
        assert is_text_type(_column(data_type), engine), (engine, data_type)
    for data_type in other_types:
        assert not is_text_type(_column(data_type), engine), (engine, data_type)


def test_a_type_is_text_only_in_the_dialect_that_spells_it():
    """``name`` is PostgreSQL's identifier type; ``sysname`` SQL Server's; ``enum`` MySQL's."""
    assert is_text_type(_column("name"), "postgresql")
    assert not is_text_type(_column("name"), "mysql")
    assert is_text_type(_column("sysname"), "sqlserver")
    assert not is_text_type(_column("sysname"), "postgresql")
    assert is_text_type(_column("enum('a')"), "mysql")
    assert not is_text_type(_column("enum('a')"), "oracle")


def test_the_normalized_type_comes_first():
    assert is_text_type(_column("USER-DEFINED", "text"), "postgresql")
    assert is_text_type(_column("my_domain", "string"), "postgresql")
    assert not is_text_type(_column("varchar", "integer"), "mysql")
    assert is_text_type({"column_name": "c", "normalized_data_type": "string"}, "")


def test_a_column_with_no_type_is_not_known_to_be_text():
    """The caller decides what an undescribed column counts as (substring_match: text)."""
    assert not is_text_type(_column(None), "mysql")
    assert not is_text_type(_column(""), "postgresql")


def test_with_no_engine_known_every_dialect_spelling_counts():
    for data_type in ("varchar2(5)", "nvarchar(max)", "citext", "longtext"):
        assert is_text_type(_column(data_type), ""), data_type
    assert not is_text_type(_column("integer"), "")
