"""Unit tests for utility/metadata statement detection (#327)."""

import pytest

from src.agents.referee.utility_statements import is_utility_statement


class TestIsUtilityStatement:
    def test_show_statement_is_utility(self):
        assert is_utility_statement("SHOW FULL FIELDS FROM `wp_options`") is True

    def test_set_session_is_utility(self):
        assert is_utility_statement("SET SESSION `SQL_BIG_SELECTS` = ?") is True

    def test_describe_is_utility(self):
        assert is_utility_statement("DESCRIBE wp_posts") is True
        assert is_utility_statement("DESC wp_posts") is True

    def test_explain_is_utility(self):
        assert is_utility_statement("EXPLAIN SELECT * FROM wp_posts") is True

    def test_create_extension_is_utility(self):
        assert is_utility_statement("CREATE EXTENSION IF NOT EXISTS pg_trgm") is True

    def test_create_table_is_utility(self):
        assert is_utility_statement("CREATE TABLE foo (id INT)") is True

    def test_alter_table_is_utility(self):
        assert is_utility_statement("ALTER TABLE wp_posts ADD COLUMN foo INT") is True

    def test_drop_table_is_utility(self):
        assert is_utility_statement("DROP TABLE wp_posts") is True

    def test_information_schema_lookup_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = $1"
            )
            is True
        )

    def test_pg_catalog_lookup_is_utility(self):
        assert is_utility_statement("SELECT * FROM pg_catalog.pg_tables") is True

    def test_leading_whitespace_is_tolerated(self):
        assert is_utility_statement("   SHOW TABLES") is True

    def test_case_insensitive(self):
        assert is_utility_statement("show tables") is True
        assert is_utility_statement("set session foo = 1") is True

    def test_ordinary_select_is_not_utility(self):
        assert is_utility_statement("SELECT * FROM wp_posts WHERE id = ?") is False

    def test_update_with_set_clause_is_not_utility(self):
        """UPDATE ... SET is workload traffic, not a session SET statement."""
        assert is_utility_statement("UPDATE wp_posts SET post_title = ? WHERE id = ?") is False

    def test_column_named_show_is_not_utility(self):
        assert is_utility_statement("SELECT show_name FROM wp_options WHERE id = ?") is False

    def test_none_text_is_not_utility(self):
        assert is_utility_statement(None) is False

    def test_empty_text_is_not_utility(self):
        assert is_utility_statement("") is False


class TestCatalogFunctionsAndCasts:
    """Tableless Postgres catalog/introspection calls are utility statements too."""

    def test_obj_description_with_no_real_table_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT obj_description($1::regclass::oid, $2)", tables_accessed=["unknown"]
            )
            is True
        )

    def test_pg_get_serial_sequence_with_no_real_table_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT pg_get_serial_sequence($1, $2)", tables_accessed=["unknown"]
            )
            is True
        )

    def test_setval_with_no_real_table_is_utility(self):
        assert (
            is_utility_statement("SELECT setval($1, $2, $3)", tables_accessed=["unknown"]) is True
        )

    def test_regtype_cast_with_no_real_table_is_utility(self):
        assert is_utility_statement("SELECT $1::regtype::oid", tables_accessed=["unknown"]) is True

    def test_regclass_cast_with_no_tables_accessed_at_all_is_utility(self):
        assert is_utility_statement("SELECT $1::regclass::oid") is True

    def test_catalog_function_with_a_real_table_is_not_utility(self):
        """A legitimate app query that happens to call a catalog function stays in scope."""
        assert (
            is_utility_statement(
                "SELECT setval('wp_posts_seq', (SELECT max(id) FROM wp_posts))",
                tables_accessed=["wp_posts"],
            )
            is False
        )

    def test_dual_pseudo_table_counts_as_no_real_table(self):
        assert is_utility_statement("SELECT $1::regtype::oid", tables_accessed=["DUAL"]) is True


class TestSystemCatalogTablesWithoutPrefix:
    """A query naming only system catalog tables is utility even with no pg_catalog. prefix,
    catalog function call or cast (#327 finding 2)."""

    def test_pg_type_join_pg_proc_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT t.oid, t.typname FROM pg_type as t JOIN pg_proc as ti ON ti.oid = t.typinput",
                tables_accessed=["pg_type", "pg_proc"],
            )
            is True
        )

    def test_pg_matviews_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE matviewname = $1)",
                tables_accessed=["pg_matviews"],
            )
            is True
        )

    def test_pg_class_is_utility(self):
        assert (
            is_utility_statement(
                "SELECT attr.attname FROM pg_class seq, pg_attribute attr, pg_depend dep",
                tables_accessed=["pg_class", "pg_attribute", "pg_depend"],
            )
            is True
        )

    def test_information_schema_table_name_is_utility(self):
        assert (
            is_utility_statement("SELECT 1", tables_accessed=["information_schema.tables"]) is True
        )

    def test_mixed_real_and_catalog_table_is_not_utility(self):
        """A real application table alongside a catalog one keeps the query in scope."""
        assert is_utility_statement("SELECT 1", tables_accessed=["pg_class", "wp_posts"]) is False

    def test_empty_tables_accessed_does_not_trigger_this_rule(self):
        assert is_utility_statement("SELECT 1 + 1", tables_accessed=[]) is False


class TestTransactionControlAndTruncate:
    """BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE/TRUNCATE are utility statements (#327 finding 2)."""

    def test_begin_is_utility(self):
        assert is_utility_statement("BEGIN") is True

    def test_commit_is_utility(self):
        assert is_utility_statement("COMMIT") is True

    def test_rollback_is_utility(self):
        assert is_utility_statement("ROLLBACK") is True

    def test_savepoint_is_utility(self):
        assert is_utility_statement("SAVEPOINT my_savepoint") is True

    def test_release_is_utility(self):
        assert is_utility_statement("RELEASE my_savepoint") is True

    def test_truncate_is_utility(self):
        assert is_utility_statement("TRUNCATE TABLE wp_sessions") is True


class TestLeadingComments:
    """A leading comment before the utility verb does not hide it (#327 finding 2)."""

    def test_block_comment_before_show_is_utility(self):
        assert is_utility_statement("/* schema dump */ SHOW TABLES") is True

    def test_line_comment_before_set_is_utility(self):
        assert is_utility_statement("-- session setup\nSET SESSION foo = 1") is True

    def test_multiple_leading_comments_are_stripped(self):
        assert is_utility_statement("/* a */\n-- b\n/* c */\nBEGIN") is True

    def test_leading_comment_before_ordinary_select_is_still_not_utility(self):
        assert is_utility_statement("/* app query */ SELECT * FROM wp_posts WHERE id = ?") is False


class TestSequenceFetch:
    """A tableless sequence fetch is sequence housekeeping, not an application
    read (#654): SQL Server's ``NEXT VALUE FOR``, Oracle's ``seq.NEXTVAL`` from
    ``DUAL``, alongside PostgreSQL's ``nextval(...)``."""

    def test_sql_server_next_value_for_is_utility(self):
        sql = "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq] AS [NextOrderNumber]"
        assert is_utility_statement(sql, tables_accessed=["unknown"]) is True

    def test_lowercase_next_value_for_is_utility(self):
        assert is_utility_statement("select next value for order_seq", ["unknown"]) is True

    def test_oracle_nextval_from_dual_is_utility(self):
        sql = "SELECT order_seq.NEXTVAL FROM DUAL"
        assert is_utility_statement(sql, tables_accessed=["DUAL"]) is True

    def test_oracle_currval_from_dual_is_utility(self):
        sql = "SELECT app.order_seq.CURRVAL FROM DUAL"
        assert is_utility_statement(sql, tables_accessed=["DUAL"]) is True

    def test_postgres_nextval_is_still_utility(self):
        assert is_utility_statement("SELECT nextval($1)", tables_accessed=["unknown"]) is True

    def test_sequence_used_in_a_real_insert_is_not_utility(self):
        sql = (
            "INSERT INTO [SalesOrder] ([OrderNumber], [CustomerID]) "
            "VALUES (NEXT VALUE FOR [dbo].[OrderNumberSeq], @CustomerID)"
        )
        assert is_utility_statement(sql, tables_accessed=["trailgear.SalesOrder"]) is False

    def test_column_named_next_value_is_not_utility(self):
        sql = "SELECT [NextValue] FROM [Counter] WHERE [Name] = @Name"
        assert is_utility_statement(sql, tables_accessed=["trailgear.Counter"]) is False


class TestSequenceFetchThroughTheParser:
    """#654 review: the offline parser keeps the statement's own case, so the
    pseudo table can come back as ``dual`` -- test the parser's real output."""

    @staticmethod
    def _parsed(sql: str) -> tuple[str, list[str]]:
        from src.tools.database.offline_parser import _transform_queries

        q = _transform_queries([{"query_text": sql}], "shop", {"orders"})[0]
        return q["query_text"], q["tables_accessed"]

    @pytest.mark.parametrize(
        "sql",
        [
            "select order_seq.nextval from dual",
            "SELECT order_seq.NEXTVAL FROM DUAL",
            "SELECT app.order_seq.CURRVAL FROM Dual",
            "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq] AS [NextOrderNumber]",
        ],
    )
    def test_tableless_sequence_fetch_is_utility(self, sql):
        text, tables = self._parsed(sql)
        assert is_utility_statement(text, tables) is True


class TestSequenceLookalikes:
    """#654 review: text that only looks like a sequence reference."""

    @pytest.mark.parametrize(
        "sql",
        [
            # a scalar UDF named NextVal, schema-qualified
            "SELECT dbo.NextVal(@k)",
            "SELECT [dbo].[NextVal](@k) AS [Next]",
            # the words inside a string literal
            "SELECT 'next value for' AS label",
            "SELECT 'order_seq.nextval' AS label FROM DUAL",
        ],
    )
    def test_tableless_lookalike_is_not_utility(self, sql):
        assert is_utility_statement(sql, ["unknown"]) is False

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT c.nextval FROM Counter c WHERE c.ID = @ID",
            "SELECT [Body] FROM [Article] WHERE [Body] = 'next value for'",
            "SELECT dbo.NextVal(@k) FROM [Counter] WHERE [ID] = @ID",
            "SELECT [NextValue] FROM [Counter] WHERE [Name] = @Name",
        ],
    )
    def test_lookalike_is_not_a_sequence_fetch(self, sql):
        from src.agents.referee.utility_statements import is_sequence_fetch

        assert is_sequence_fetch(sql) is False

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq]",
            "select next value for order_seq",
            "SELECT order_seq.NEXTVAL FROM DUAL",
            "SELECT app.order_seq.CURRVAL FROM dual",
            "INSERT INTO orders (id) VALUES (order_seq.NEXTVAL)",
            "SELECT nextval('orders_id_seq')",
            "SELECT lastval()",
        ],
    )
    def test_real_sequence_reference_is_a_sequence_fetch(self, sql):
        from src.agents.referee.utility_statements import is_sequence_fetch

        assert is_sequence_fetch(sql) is True

    def test_tableless_lastval_is_utility(self):
        assert is_utility_statement("SELECT lastval()", ["unknown"]) is True


class TestSequencePatternIsShared:
    """aurora_choice's ``sequences`` workload feature and the utility rule read
    one detector, so they can never disagree on what a sequence reference is."""

    def test_aurora_choice_uses_the_shared_detector(self):
        from src.agents.referee import aurora_choice, utility_statements

        assert aurora_choice.has_sequence_reference is utility_statements.has_sequence_reference
