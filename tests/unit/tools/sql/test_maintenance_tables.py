"""Maintenance and DDL statements name the table they act on (#483).

``ALTER TABLE t``, ``DROP TABLE t``, ``TRUNCATE t``, ``ANALYZE t``, ``DESCRIBE t`` and
``COMMENT ON TABLE t`` parse in sqlglot but were refused as "not a statement", and
the regex fallback found no ``FROM`` in them, so the collector reported ``unknown``.
``ALTER INDEX i ON t``, ``UPDATE STATISTICS t``, ``OPTIMIZE TABLE t``, ``LOCK TABLES
t``, ``VACUUM t``, ``REINDEX TABLE t`` and ``CLUSTER t`` are generic commands to
sqlglot (``UPDATE STATISTICS t`` even read as a table named ``STATISTICS``); the
regex now reads them (:mod:`src.tools.sql.maintenance`). Each case runs with the
source dialect of the engine that writes it.
"""

from __future__ import annotations

import pytest

from src.tools.sql.facade import table_references
from src.tools.sql.maintenance import maintenance_tables
from src.tools.sql.regex_tables import regex_table_refs


def _names(sql: str, engine: str) -> list[str]:
    return [".".join([*ref.qualifiers, ref.name]) for ref in table_references(sql, engine).tables]


# (engine, sql, tables as written, parsed by)
CASES = [
    # sqlglot reads these; the facade used to refuse them as not a statement.
    ("mysql", "ALTER TABLE `t1` ADD COLUMN a int", ["t1"], "sqlglot"),
    ("postgresql", "ALTER TABLE ONLY public.t1 ADD COLUMN a int", ["public.t1"], "sqlglot"),
    ("sqlserver", "ALTER TABLE [dbo].[t1] ADD a int", ["dbo.t1"], "sqlglot"),
    ("oracle", "ALTER TABLE t1 ADD (a int)", ["t1"], "sqlglot"),
    ("mysql", "DROP TABLE IF EXISTS t1, t2", ["t1", "t2"], "sqlglot"),
    ("sqlserver", "DROP TABLE dbo.t1", ["dbo.t1"], "sqlglot"),
    ("postgresql", "DROP VIEW IF EXISTS v1", ["v1"], "sqlglot"),
    ("mysql", "DROP INDEX idx ON t1", ["t1"], "sqlglot"),
    ("sqlserver", "DROP INDEX IX_a ON dbo.Orders", ["dbo.Orders"], "sqlglot"),
    ("postgresql", "TRUNCATE t1, t2 RESTART IDENTITY", ["t1", "t2"], "sqlglot"),
    ("sqlserver", "TRUNCATE TABLE dbo.Orders", ["dbo.Orders"], "sqlglot"),
    ("oracle", "TRUNCATE TABLE t1", ["t1"], "sqlglot"),
    ("mysql", "ANALYZE TABLE `t1`", ["t1"], "sqlglot"),
    ("postgresql", "ANALYZE t1", ["t1"], "sqlglot"),
    ("oracle", "ANALYZE TABLE t1 COMPUTE STATISTICS", ["t1"], "sqlglot"),
    ("mysql", "DESCRIBE t1", ["t1"], "sqlglot"),
    ("postgresql", "COMMENT ON TABLE t1 IS 'x'", ["t1"], "sqlglot"),
    # Only the table is a reference: not the new name, the index, or a sequence.
    ("postgresql", "ALTER TABLE t1 RENAME TO t3", ["t1"], "sqlglot"),
    ("postgresql", "ALTER INDEX i RENAME TO j", [], "sqlglot"),
    ("postgresql", "DROP INDEX CONCURRENTLY idx", [], "sqlglot"),
    ("postgresql", "DROP SEQUENCE s", [], "sqlglot"),
    # The foreign key's target is not the table being altered.
    (
        "postgresql",
        "ALTER TABLE ONLY t1 ADD CONSTRAINT c FOREIGN KEY (a) REFERENCES t2 (id)",
        ["t1"],
        "sqlglot",
    ),
    # Generic commands sqlglot does not read: the regex does.
    ("sqlserver", "ALTER INDEX [IX_a] ON [StockMovement] REORGANIZE", ["StockMovement"], "regex"),
    (
        "sqlserver",
        "ALTER INDEX ALL ON [dbo].[Orders] REBUILD WITH (ONLINE = ON)",
        ["dbo.Orders"],
        "regex",
    ),
    (
        "sqlserver",
        "/* c */ ALTER INDEX x ON [db].[dbo].[Orders] REBUILD",
        ["db.dbo.Orders"],
        "regex",
    ),
    ("sqlserver", "UPDATE STATISTICS [dbo].[Orders] WITH FULLSCAN", ["dbo.Orders"], "regex"),
    ("mysql", "OPTIMIZE NO_WRITE_TO_BINLOG TABLE db.t1, `t2`", ["db.t1", "t2"], "regex"),
    ("mysql", "CHECK TABLE t1", ["t1"], "regex"),
    ("mysql", "LOCK TABLES t1 AS a READ LOCAL, t2 LOW_PRIORITY WRITE", ["t1", "t2"], "regex"),
    ("postgresql", "VACUUM ANALYZE t1", ["t1"], "regex"),
    ("postgresql", "VACUUM (FULL, ANALYZE) public.t1", ["public.t1"], "regex"),
    ("postgresql", 'VACUUM FULL "My T"', ["My T"], "regex"),
    ("postgresql", "REINDEX (VERBOSE) TABLE CONCURRENTLY t1", ["t1"], "regex"),
    ("postgresql", "CLUSTER VERBOSE t1 USING idx", ["t1"], "regex"),
    ("postgresql", "LOCK TABLE ONLY a, b IN ACCESS SHARE MODE", ["a", "b"], "regex"),
    ("oracle", "LOCK TABLE t1 IN EXCLUSIVE MODE", ["t1"], "regex"),
]


@pytest.mark.parametrize("engine,sql,expected,parsed_by", CASES)
def test_statement_names_its_table(engine, sql, expected, parsed_by):
    refs = table_references(sql, engine)
    assert _names(sql, engine) == expected
    assert refs.parsed_by == parsed_by


# Statements that name no table, however they are read.
NO_TABLE = [
    ("postgresql", "VACUUM"),
    ("postgresql", "VACUUM FULL"),
    ("postgresql", "VACUUM (VERBOSE)"),
    ("postgresql", "CLUSTER"),
    ("postgresql", "CLUSTER VERBOSE"),
    ("postgresql", "REINDEX INDEX i"),
    ("postgresql", "REINDEX DATABASE d"),
    ("mysql", "LOCK INSTANCE FOR BACKUP"),
    ("mysql", "UNLOCK TABLES"),
    ("sqlserver", "ALTER INDEX x ON"),
    ("sqlserver", "SELECT 'ALTER INDEX x ON t REBUILD'"),
    ("postgresql", "SELECT 'VACUUM t1'"),
    ("postgresql", "SELECT cluster FROM s"),
]


@pytest.mark.parametrize("engine,sql", NO_TABLE)
def test_statement_without_a_table_names_none(engine, sql):
    assert maintenance_tables(sql, engine) is None
    assert [r for r in _names(sql, engine) if r in ("t1", "t", "STATISTICS")] == []


def test_update_statistics_is_no_longer_a_table_named_statistics():
    assert regex_table_refs("UPDATE STATISTICS dbo.Orders", "tsql") == [(["dbo"], "Orders")]


def test_only_the_first_statement_is_read():
    assert maintenance_tables("VACUUM t1; SELECT 1 FROM z", "postgresql") == [([], "t1")]


def test_a_dml_update_is_not_update_statistics():
    assert maintenance_tables("UPDATE orders SET a = 1", "sqlserver") is None
    assert _names("UPDATE orders SET a = 1", "sqlserver") == ["orders"]


def test_a_tsvector_column_type_in_ddl_is_full_text_search():
    # ALTER TABLE parses in sqlglot now, so the type reference the regex used to find
    # is read from the tree (sqlglot has no tsvector type; it is a user-defined one).
    from src.tools.sql.facade import sql_facts

    sql = "ALTER TABLE articles ADD COLUMN search_vec tsvector"
    assert sql_facts(sql, "postgresql").fulltext_search is True
    assert sql_facts("ALTER TABLE articles ADD COLUMN n int", "postgresql").fulltext_search is False


def test_a_keyword_in_a_comment_of_a_fallback_statement_is_not_a_fact():
    from src.tools.sql.facade import sql_facts

    facts = sql_facts("VACUUM t -- GROUP BY y, LIMIT 5", "postgresql")
    assert facts.parsed_by == "regex"
    assert facts.group_by is False
    assert facts.row_limit is False
