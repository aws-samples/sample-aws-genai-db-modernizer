"""The sqlglot facade for table references (#557).

The facade is the one place that calls sqlglot. These tests pin what it reports
per source dialect, how it treats CTE names (the #682/#686 rules), subqueries
and quoted identifiers, and that SQL it can't parse falls back to the regex
extractor without raising.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.tools.sql.facade import MAX_SQL_CHARS, TableRef, sqlglot_dialect, table_references
from tests.unit.tools.sql._timing import assert_linear


def _names(sql: str, engine: str) -> list[str]:
    return [t.written for t in table_references(sql, engine).tables]


class TestDialect:
    @pytest.mark.parametrize(
        "engine,dialect",
        [
            ("mysql", "mysql"),
            ("mariadb", "mysql"),
            ("postgresql", "postgres"),
            ("postgres", "postgres"),
            ("sqlserver", "tsql"),
            ("oracle", "oracle"),
            ("db2", ""),
            ("SQLServer", "tsql"),
            ("something-else", ""),
        ],
    )
    def test_source_engine_maps_to_a_sqlglot_dialect(self, engine, dialect):
        assert sqlglot_dialect(engine) == dialect

    def test_result_records_the_dialect_used(self):
        assert table_references("SELECT 1 FROM t", "sqlserver").dialect == "tsql"


class TestPerDialect:
    def test_mysql_digest_with_backticks_and_placeholders(self):
        sql = (
            "SELECT `p` . `ID` FROM `wp_posts` `p` INNER JOIN `wp_postmeta` `m` "
            "ON `p` . `ID` = `m` . `post_id` WHERE `post_type` = ? "
            "AND `p` . `post_status` IN (...) LIMIT ?, ..."
        )
        refs = table_references(sql, "mysql")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == ["wp_posts", "wp_postmeta"]

    def test_mysql_upsert_update_is_not_a_table(self):
        sql = (
            "INSERT INTO `wp_options` ( `option_name` , `option_value` ) VALUES (...) "
            "ON DUPLICATE KEY UPDATE `option_name` = VALUES ( `option_name` )"
        )
        assert _names(sql, "mysql") == ["wp_options"]

    def test_mysql_from_dual_is_not_a_table(self):
        assert _names("SELECT ?, ... FROM DUAL", "mysql") == []

    def test_postgresql_on_conflict_do_update_is_not_a_table(self):
        sql = (
            "INSERT INTO user_stats (user_id, likes) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET likes = excluded.likes"
        )
        assert _names(sql, "postgresql") == ["user_stats"]

    def test_postgresql_schema_qualified_and_quoted(self):
        sql = 'SELECT * FROM public."Users" u JOIN "posts" p ON p.user_id = u.id'
        refs = table_references(sql, "postgresql")
        assert refs.tables == (TableRef(name="Users", schema="public"), TableRef(name="posts"))

    def test_sqlserver_brackets_three_part_name_and_hint(self):
        sql = (
            "SELECT o.[OrderId] FROM [trailgear].[Sales].[Orders] o WITH (NOLOCK) "
            "LEFT JOIN [dbo].[Customers] c ON c.Id = o.CustomerId WHERE o.Id = @Id"
        )
        refs = table_references(sql, "sqlserver")
        assert refs.tables == (
            TableRef(name="Orders", schema="Sales", catalog="trailgear"),
            TableRef(name="Customers", schema="dbo"),
        )

    def test_sqlserver_merge_target_and_source(self):
        sql = (
            "MERGE INTO [Sales].[Stock] AS t USING [Staging].[Stock] AS s ON t.Id = s.Id "
            "WHEN MATCHED THEN UPDATE SET t.Qty = s.Qty "
            "WHEN NOT MATCHED THEN INSERT (Id, Qty) VALUES (s.Id, s.Qty);"
        )
        assert _names(sql, "sqlserver") == ["Sales.Stock", "Staging.Stock"]

    @pytest.mark.parametrize(
        "sql,expected",
        [
            (
                "DELETE TOP (5000) FROM [UserSession] WHERE [ExpiresAt] < SYSUTCDATETIME()",
                ["UserSession"],
            ),
            ("UPDATE TOP (10) PERCENT [dbo].[Queue] SET Taken = 1", ["dbo.Queue"]),
        ],
    )
    def test_sqlserver_dml_top_is_not_a_table(self, sql, expected):
        refs = table_references(sql, "sqlserver")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == expected

    def test_postgresql_placeholders_flush_against_operators(self):
        sql = 'DELETE FROM "group_users" WHERE $1=$2 AND "group_users"."user_id" IN ($3,$4)'
        refs = table_references(sql, "postgresql")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == ["group_users"]

    def test_postgresql_delete_using_and_comma_joins(self):
        sql = "DELETE FROM bookmarks b USING topics t, posts p WHERE b.id = p.id AND t.id = p.tid"
        assert _names(sql, "postgresql") == ["bookmarks", "topics", "posts"]

    def test_is_distinct_from_and_extract_from_are_not_tables(self):
        sql = (
            "SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP - p.created_at) FROM posts p "
            "JOIN topics t ON t.id = p.topic_id WHERE p.x IS DISTINCT FROM t.id"
        )
        assert _names(sql, "postgresql") == ["posts", "topics"]

    def test_words_in_comments_are_not_tables(self):
        sql = "WITH u AS ( -- users allowed to join chat\n SELECT id FROM users) SELECT * FROM u"
        assert _names(sql, "postgresql") == ["users"]

    def test_oracle_quoted_names_and_dual(self):
        sql = (
            'SELECT e."EMP_ID" FROM "HR"."EMPLOYEES" e WHERE e.dept_id = :1 '
            "AND EXISTS (SELECT 1 FROM DUAL)"
        )
        refs = table_references(sql, "oracle")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == ["HR.EMPLOYEES"]

    def test_oracle_positional_and_named_binds(self):
        sql = "UPDATE orders SET status = :1, note = '12:30' WHERE id = :2 AND region = :region"
        refs = table_references(sql, "oracle")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == ["orders"]

    def test_update_and_delete_targets(self):
        assert _names("UPDATE orders SET status = $1 WHERE id = $2", "postgresql") == ["orders"]
        assert _names("DELETE FROM `wp_options` WHERE `option_name` = ?", "mysql") == ["wp_options"]


class TestCteNames:
    """Mirrors the #682 regex cases in tests/unit/tools/database/test_cte_names.py."""

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("WITH a AS (SELECT 1 FROM r) SELECT * FROM a", ["r"]),
            (
                "WITH RECURSIVE t(n) AS (SELECT 1 FROM r UNION ALL SELECT n FROM t) "
                "SELECT * FROM t",
                ["r"],
            ),
            ('WITH "Q" AS (SELECT 1 FROM r) SELECT * FROM "Q"', ["r"]),
            (
                "WITH a AS (SELECT 1 x FROM r), b AS (SELECT * FROM a) SELECT * FROM b JOIN s",
                ["r", "s"],
            ),
            ("SELECT * FROM (WITH s AS (SELECT 1 FROM r) SELECT * FROM s) x", ["r"]),
            # A non-recursive CTE reading the real table of the same name.
            (
                "WITH orders AS (SELECT * FROM orders WHERE active = 1) SELECT * FROM orders",
                ["orders"],
            ),
            # A subquery's WITH must not hide a real table outside it.
            (
                "SELECT * FROM users u WHERE u.id IN "
                "(WITH users AS (SELECT 1 AS id) SELECT id FROM users)",
                ["users"],
            ),
            # A later CTE is not visible inside an earlier one.
            ("WITH a AS (SELECT * FROM b), b AS (SELECT 1 FROM r) SELECT * FROM a", ["b", "r"]),
            # A qualified name is never a CTE.
            ("WITH a AS (SELECT 1 FROM r) SELECT * FROM public.a", ["r", "public.a"]),
            # CTE names compare case-insensitively.
            ("WITH Recent AS (SELECT 1 FROM r) SELECT * FROM RECENT", ["r"]),
        ],
    )
    def test_cte_names_are_not_tables(self, sql, expected):
        refs = table_references(sql, "postgresql")
        assert refs.parsed_by == "sqlglot"
        assert sorted(t.written for t in refs.tables) == sorted(expected)

    def test_cte_feeding_an_insert(self):
        sql = "WITH src AS (SELECT id FROM staging) INSERT INTO target SELECT id FROM src"
        assert sorted(_names(sql, "postgresql")) == ["staging", "target"]

    def test_tsql_recursive_cte_without_the_keyword(self):
        sql = (
            "WITH Tree AS (SELECT Id FROM Nodes UNION ALL "
            "SELECT n.Id FROM Nodes n JOIN Tree t ON n.ParentId = t.Id) SELECT * FROM Tree"
        )
        assert _names(sql, "sqlserver") == ["Nodes"]


class TestSubqueries:
    def test_tables_in_subqueries_and_derived_tables(self):
        sql = (
            "SELECT * FROM (SELECT user_id FROM posts GROUP BY user_id) p "
            "WHERE p.user_id IN (SELECT id FROM users WHERE active) "
            "AND EXISTS (SELECT 1 FROM bans b WHERE b.user_id = p.user_id)"
        )
        assert _names(sql, "postgresql") == ["posts", "users", "bans"]

    def test_duplicates_collapse_in_first_seen_order(self):
        sql = "SELECT * FROM a JOIN b ON 1 = 1 WHERE a.x IN (SELECT x FROM a)"
        assert _names(sql, "postgresql") == ["a", "b"]

    def test_table_functions_are_not_tables(self):
        assert _names("SELECT * FROM generate_series(1, 3) g", "postgresql") == []


class TestFallback:
    @pytest.mark.parametrize(
        "sql,engine,reason",
        [
            ("SELEC * FORM nowhere ((", "mysql", "parse_error"),
            ("SELECT * FROM t WHERE (a = 1", "postgresql", "parse_error"),
            ("\x00\x01 garbage", "oracle", "not_a_statement"),
        ],
    )
    def test_unparseable_sql_falls_back_without_raising(self, sql, engine, reason):
        refs = table_references(sql, engine)
        assert refs.parsed_by == "regex"
        assert refs.fallback_reason == reason

    def test_fallback_uses_the_regex_extractor(self):
        refs = table_references("SELECT * FROM orders WHERE (a = 1", "postgresql")
        assert [t.written for t in refs.tables] == ["orders"]

    def test_generic_command_falls_back(self):
        refs = table_references("REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv", "postgresql")
        assert refs.parsed_by == "regex"
        assert refs.fallback_reason == "command"
        assert [t.written for t in refs.tables] == ["public.mv"]

    def test_empty_text_falls_back(self):
        refs = table_references("   ", "mysql")
        assert refs.parsed_by == "regex"
        assert refs.fallback_reason == "empty"
        assert refs.tables == ()

    def test_truncated_digest_is_flagged(self):
        refs = table_references("SELECT `a` FROM `wp_posts` WHERE `ID` IN (", "mysql")
        assert refs.truncated is True
        assert refs.parsed_by == "regex"
        assert [t.written for t in refs.tables] == ["wp_posts"]

    def test_unexpected_parser_exception_falls_back(self, monkeypatch):
        import src.tools.sql.facade as facade

        def boom(*_a, **_k):
            raise RecursionError("too deep")

        monkeypatch.setattr(facade.sqlglot, "parse", boom)
        refs = table_references("SELECT * FROM t", "mysql")
        assert refs.parsed_by == "regex"
        assert refs.fallback_reason == "parse_error"
        assert [t.written for t in refs.tables] == ["t"]


class TestBridgesNeverChangeTables:
    """Each dialect bridge rewrites only syntax that names no table (#557 review round 2)."""

    def test_tsql_top_with_a_subquery_keeps_both_tables(self):
        refs = table_references("DELETE TOP (SELECT n FROM dbo.Config) FROM dbo.Queue", "sqlserver")
        assert sorted(t.written for t in refs.tables) == ["dbo.Config", "dbo.Queue"]

    @pytest.mark.parametrize("limit", ["5000", "@BatchSize", "?", " 10 "])
    def test_tsql_top_with_a_number_or_variable_is_dropped(self, limit):
        refs = table_references(
            f"DELETE TOP ({limit}) FROM [dbo].[Queue] WHERE Done = 1", "sqlserver"
        )
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == ["dbo.Queue"]

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ('SELECT * FROM "price$1usd" WHERE a = $1', ["price$1usd"]),
            ("SELECT * FROM a$1b JOIN c ON c.id = a$1b.id WHERE x=$1", ["a$1b", "c"]),
            ('SELECT * FROM "$1abc" WHERE a=$1', ["$1abc"]),
            ('SELECT * FROM "t $2x" WHERE a IN ($1,$2)', ["t $2x"]),
            ("SELECT '$1x' AS s FROM t WHERE a=$1", ["t"]),
            ("SELECT 1 FROM t -- $1x\nWHERE a=$1", ["t"]),
        ],
    )
    def test_postgresql_placeholder_spacing_never_splits_identifiers(self, sql, expected):
        refs = table_references(sql, "postgresql")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == expected

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT ':1 FROM secret' FROM orders WHERE id = :1", ["orders"]),
            ("SELECT a FROM orders -- :2 FROM ghost\nWHERE id = :1", ["orders"]),
            ('SELECT a FROM "T:1" WHERE id = :1', ["T:1"]),
            ("SELECT a FROM orders WHERE t = '10:1' AND id = :1", ["orders"]),
        ],
    )
    def test_oracle_bind_rename_skips_literals_comments_and_quoted_names(self, sql, expected):
        refs = table_references(sql, "oracle")
        assert refs.parsed_by == "sqlglot"
        assert [t.written for t in refs.tables] == expected


class TestSizeCap:
    @staticmethod
    def _insert(n: int) -> str:
        return "INSERT INTO orders (note) VALUES ('" + "a" * n + "')"

    @pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "oracle"])
    def test_a_one_megabyte_literal_falls_back_to_the_regex(self, engine):
        refs = table_references(self._insert(1_000_000), engine)
        assert refs.parsed_by == "regex"
        assert refs.fallback_reason == "too_long"
        assert [t.written for t in refs.tables] == ["orders"]

    @pytest.mark.slow
    def test_long_text_is_handled_in_linear_time(self):
        assert_linear(self._insert, lambda text: table_references(text, "postgresql"))

    def test_the_cap_is_inclusive(self):
        frame = "SELECT '' FROM orders"
        at_cap = frame.replace("''", "'" + "x" * (MAX_SQL_CHARS - len(frame)) + "'")
        assert len(at_cap) == MAX_SQL_CHARS
        assert table_references(at_cap, "postgresql").parsed_by == "sqlglot"
        over = at_cap.replace("'x", "'xx", 1)
        assert table_references(over, "postgresql").fallback_reason == "too_long"


_REPO = Path(__file__).resolve().parents[4]
_FACADE = _REPO / "src" / "tools" / "sql" / "facade.py"


def _imports_sqlglot(source: str) -> bool:
    """Whether Python ``source`` imports sqlglot: ``import``/``from`` statements
    (``import os, sqlglot`` included) or ``importlib.import_module``/``__import__``
    with a ``"sqlglot..."`` string."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "sqlglot" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "sqlglot":
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and str(arg.value).split(".")[0] == "sqlglot":
                    return True
    return False


@pytest.mark.parametrize(
    "source,expected",
    [
        ("import sqlglot", True),
        ("import os, sqlglot", True),
        ("import sqlglot.expressions as e", True),
        ("from sqlglot import exp", True),
        ("from sqlglot.optimizer import scope", True),
        ("import importlib\nimportlib.import_module('sqlglot')", True),
        ("__import__('sqlglot.dialects')", True),
        ("from importlib import import_module\nimport_module('sqlglot')", True),
        ("from . import sqlglot_notes", False),
        ("x = 'import sqlglot'", False),
        ("import sqlglotish", False),
    ],
)
def test_import_guard_detects_every_import_form(source, expected):
    assert _imports_sqlglot(source) is expected


def test_only_the_facade_imports_sqlglot():
    """No other module walks sqlglot's tree (#557): one facade, one fact model."""
    offenders = []
    for root in ("src", "scripts"):
        for path in sorted((_REPO / root).rglob("*.py")):
            if path == _FACADE:
                continue
            if _imports_sqlglot(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []
    assert _imports_sqlglot(_FACADE.read_text(encoding="utf-8"))
