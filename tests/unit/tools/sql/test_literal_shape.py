"""``literal_shape``: the text of a statement with its literal values masked (#412).

Two statements that differ only in a literal value share a shape; two that differ in
structure (another column, operator, table, hint, positional ORDER BY) do not.
"""

from __future__ import annotations

import pytest

from src.tools.sql.normalize import literal_shape, literal_shape_digest


def _same(a: str, b: str, dialect: str = "oracle") -> None:
    assert literal_shape(a, dialect) == literal_shape(b, dialect)
    assert literal_shape_digest(a, dialect) == literal_shape_digest(b, dialect)


def _differ(a: str, b: str, dialect: str = "oracle") -> None:
    assert literal_shape(a, dialect) != literal_shape(b, dialect)
    assert literal_shape_digest(a, dialect) != literal_shape_digest(b, dialect)


class TestLiteralVariantsShareAShape:
    def test_numbers(self) -> None:
        _same("SELECT * FROM t WHERE id = 1", "SELECT * FROM t WHERE id = 2")
        _same("SELECT * FROM t WHERE p > 1.5", "SELECT * FROM t WHERE p > 250")
        _same("SELECT * FROM t WHERE p > 1e3", "SELECT * FROM t WHERE p > .5")

    def test_strings_and_doubled_quotes(self) -> None:
        _same("SELECT * FROM t WHERE s = 'A'", "SELECT * FROM t WHERE s = 'it''s'")

    def test_national_strings(self) -> None:
        _same("SELECT * FROM t WHERE s = N'a'", "SELECT * FROM t WHERE s = 'b'")

    @pytest.mark.parametrize("literal", ["q'[a'b]'", "q'(x)'", "Q'{y}'", "q'!it's!'", "nq'<z>'"])
    def test_q_quoted_strings(self, literal: str) -> None:
        _same("SELECT * FROM t WHERE s = 'plain'", f"SELECT * FROM t WHERE s = {literal}")

    def test_dates_and_timestamps(self) -> None:
        _same(
            "SELECT * FROM t WHERE d > DATE '2024-01-01'",
            "SELECT * FROM t WHERE d > DATE '2025-06-30'",
        )
        _same(
            "SELECT * FROM t WHERE d > TO_DATE('2024-01-01', 'YYYY-MM-DD')",
            "SELECT * FROM t WHERE d > TO_DATE('2025-06-30', 'YYYY-MM-DD')",
        )
        _same(
            "SELECT * FROM t WHERE d > TIMESTAMP '2024-01-01 10:00:00'",
            "SELECT * FROM t WHERE d > TIMESTAMP '2025-06-30 11:30:00'",
        )

    def test_in_lists_of_any_length(self) -> None:
        _same(
            "SELECT * FROM t WHERE id IN (1, 2, 3)",
            "SELECT * FROM t WHERE id IN (4)",
        )
        _same(
            "SELECT * FROM t WHERE s NOT IN ('a','b')",
            "SELECT * FROM t WHERE s NOT IN ('c','d','e','f')",
        )

    def test_whitespace_and_keyword_case(self) -> None:
        _same(
            "select *  from t\n where id = 1",
            "SELECT * FROM t WHERE id = 2",
        )

    def test_plain_comments_are_not_structure(self) -> None:
        _same("SELECT /* req 7 */ * FROM t WHERE id = 1", "SELECT * FROM t WHERE id = 2")

    def test_between_and_limits(self) -> None:
        _same(
            "SELECT * FROM t WHERE n BETWEEN 1 AND 10 FETCH FIRST 5 ROWS ONLY",
            "SELECT * FROM t WHERE n BETWEEN 20 AND 30 FETCH FIRST 50 ROWS ONLY",
        )

    def test_trailing_semicolon(self) -> None:
        _same("SELECT 1 FROM dual", "SELECT 2 FROM dual;")


class TestStructuralVariantsStayApart:
    def test_other_column_operator_or_table(self) -> None:
        _differ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE b = 1")
        _differ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE a > 1")
        _differ("SELECT * FROM t WHERE a = 1", "SELECT * FROM u WHERE a = 1")
        _differ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE a = 1 AND b = 2")

    def test_literal_count_outside_in_lists(self) -> None:
        _differ("INSERT INTO t VALUES (1, 2)", "INSERT INTO t VALUES (1, 2, 3)")
        _differ("SELECT 1, 2 FROM dual", "SELECT 1, 2, 3 FROM dual")

    def test_positional_order_and_group_by(self) -> None:
        _differ("SELECT a, b FROM t ORDER BY 1", "SELECT a, b FROM t ORDER BY 2")
        _differ("SELECT a, b FROM t ORDER BY 1, 2", "SELECT a, b FROM t ORDER BY 1")
        _differ("SELECT a, count(*) FROM t GROUP BY 1", "SELECT a, count(*) FROM t GROUP BY 2")

    def test_optimizer_hints(self) -> None:
        _differ(
            "SELECT /*+ INDEX(t i1) */ * FROM t WHERE a = 1",
            "SELECT /*+ FULL(t) */ * FROM t WHERE a = 1",
        )
        _differ("SELECT /*+ FULL(t) */ * FROM t WHERE a = 1", "SELECT * FROM t WHERE a = 1")

    def test_bind_variables_are_not_literals(self) -> None:
        _differ("SELECT * FROM t WHERE a = :1", "SELECT * FROM t WHERE a = 1")
        _differ("SELECT * FROM t WHERE a = :1", "SELECT * FROM t WHERE a = :2")

    def test_digits_inside_names_are_kept(self) -> None:
        _differ("SELECT c1 FROM t1 WHERE a = 1", "SELECT c2 FROM t1 WHERE a = 1")
        _differ("SELECT * FROM t1 WHERE a = 1", "SELECT * FROM t2 WHERE a = 1")
        _differ('SELECT "C1" FROM t', 'SELECT "C2" FROM t')

    def test_quoted_identifier_case_matters(self) -> None:
        _differ('SELECT "Col" FROM t', 'SELECT "COL" FROM t')

    def test_interval_unit(self) -> None:
        _differ("SELECT INTERVAL '5' DAY FROM dual", "SELECT INTERVAL '5' HOUR FROM dual")

    def test_string_content_is_never_read_as_sql(self) -> None:
        _same("SELECT * FROM t WHERE s = 'a -- b'", "SELECT * FROM t WHERE s = 'c'")
        _same(
            "SELECT * FROM t WHERE s = 'x /* y' AND n = 1",
            "SELECT * FROM t WHERE s = 'z' AND n = 2",
        )


class TestOtherDialectsKeepTheirOwnQuoting:
    def test_mysql_backslash_escape(self) -> None:
        _same("SELECT * FROM t WHERE s = 'it\\'s'", "SELECT * FROM t WHERE s = 'x'", "mysql")

    def test_postgres_dollar_quote(self) -> None:
        _same("SELECT * FROM t WHERE s = $$a$$", "SELECT * FROM t WHERE s = $$b$$", "postgresql")

    def test_q_quote_is_oracle_only(self) -> None:
        # Outside Oracle, ``q'[a]'`` is an identifier ``q`` then a string.
        assert literal_shape("SELECT q'[a]' FROM t", "postgresql") != literal_shape(
            "SELECT 'x' FROM t", "postgresql"
        )
