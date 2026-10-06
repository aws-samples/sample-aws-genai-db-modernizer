"""Unit tests for utility/metadata statement detection (#327)."""

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
