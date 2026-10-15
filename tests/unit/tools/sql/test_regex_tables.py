"""The regex extractor reads leading comments with the shared comment rule (#814).

``is_merge_statement`` used its own comment regex; it now uses
:mod:`src.shared.sql_comments`, so a nested block comment before a MERGE is
read the way the source dialect reads it: PostgreSQL and SQL Server nest block
comments, MySQL and Oracle end one at its first ``*/``.
"""

import pytest

from src.tools.sql.regex_tables import is_merge_statement, regex_table_refs

MERGE = "MERGE INTO stock t USING movement s ON t.id = s.id WHEN MATCHED THEN UPDATE SET n = s.n"


class TestLeadingComments:
    @pytest.mark.parametrize(
        "prefix",
        ["", "/* job */ ", "-- job\n", "/* a */ -- b\n  /* c */"],
    )
    @pytest.mark.parametrize("engine", ["", "postgresql", "mysql", "sqlserver", "oracle"])
    def test_plain_leading_comments_before_merge(self, prefix, engine):
        assert is_merge_statement(prefix + MERGE, engine) is True

    @pytest.mark.parametrize("engine", ["postgresql", "postgres", "sqlserver", "tsql"])
    def test_nested_comment_before_merge_where_comments_nest(self, engine):
        text = "/* a /* b */ c */ " + MERGE
        assert is_merge_statement(text, engine) is True
        assert regex_table_refs(text, engine) == [([], "stock"), ([], "movement")]

    @pytest.mark.parametrize("engine", ["", "mysql", "oracle"])
    def test_first_close_ends_the_comment_where_comments_do_not_nest(self, engine):
        assert is_merge_statement("/* a /* b */ c */ " + MERGE, engine) is False
        assert is_merge_statement("/* a /* b */ " + MERGE, engine) is True

    def test_line_comment_without_its_newline_is_kept(self):
        assert is_merge_statement("-- " + MERGE) is False
