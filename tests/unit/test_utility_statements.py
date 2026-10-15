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


class TestAdminCommandFamilies:
    """Maintenance, locking, privilege, bulk-copy and extra transaction/session
    commands are database administration, not application access (#737).

    Each is recognised by its leading verb only (comments stripped first), the
    same engine-neutral rule as SHOW/SET/CREATE, whatever the statement names
    as its table.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            # Maintenance and statistics
            "VACUUM",
            "VACUUM ANALYZE some_table",
            "VACUUM (VERBOSE, ANALYZE) public.orders",
            "ANALYZE some_table",
            "ANALYSE some_table",
            "ANALYZE TABLE wp_posts",
            "OPTIMIZE TABLE wp_options",
            "REINDEX TABLE orders",
            "REINDEX INDEX CONCURRENTLY orders_pkey",
            "CLUSTER orders USING orders_pkey",
            "CHECKPOINT",
            "CHECK TABLE wp_posts",
            "REPAIR TABLE wp_posts",
            "FLUSH TABLES",
            "DBCC CHECKDB",
            "DBCC SHOW_STATISTICS ('dbo.Orders', IX_Orders)",
            "UPDATE STATISTICS dbo.Orders",
            # Locking
            "LOCK TABLE orders IN ACCESS EXCLUSIVE MODE",
            "LOCK TABLES wp_posts WRITE",
            "UNLOCK TABLES",
            # Privileges
            "GRANT SELECT ON orders TO reporting",
            "REVOKE ALL ON orders FROM public",
            # Transaction control
            "START TRANSACTION",
            "START TRANSACTION READ ONLY",
            "END",
            "END TRANSACTION",
            "ABORT",
            # Session state
            "RESET ALL",
            "DISCARD ALL",
            "DEALLOCATE PREPARE stmt1",
            "USE wordpress",
            # Object comments
            "COMMENT ON TABLE orders IS 'customer orders'",
        ],
    )
    def test_admin_command_is_utility(self, sql):
        assert is_utility_statement(sql) is True

    @pytest.mark.parametrize(
        "sql",
        ["vacuum analyze some_table", "grant select on orders to app", "start transaction"],
    )
    def test_admin_command_is_case_insensitive(self, sql):
        assert is_utility_statement(sql) is True

    def test_admin_command_resolving_to_a_real_table_is_still_utility(self):
        """The issue's repro: a VACUUM ANALYZE on a resolved table is not application access."""
        assert is_utility_statement("VACUUM ANALYZE some_table", ["public.some_table"]) is True
        assert is_utility_statement("GRANT SELECT ON orders TO app", ["public.orders"]) is True

    def test_leading_comment_before_admin_command_is_utility(self):
        assert is_utility_statement("/* nightly */ VACUUM ANALYZE orders") is True
        assert is_utility_statement("-- maintenance\nREINDEX TABLE orders") is True

    @pytest.mark.parametrize(
        "sql",
        [
            # The admin word appears, but not as the leading verb.
            "SELECT * FROM reports WHERE status = 'analyze'",
            "SELECT analyze_id, vacuum_count FROM job_stats WHERE id = ?",
            'SELECT "grant", amount FROM awards WHERE id = $1',
            "SELECT `grant` FROM awards WHERE id = ?",
            "UPDATE awards SET grant = ? WHERE id = ?",
            "INSERT INTO permissions (grant, revoke) VALUES (?, ?)",
            "DELETE FROM locks WHERE lock_id = ?",
            "SELECT * FROM copy_jobs WHERE id = ?",
            "SELECT * FROM t WHERE id = ? FOR UPDATE",
            "SELECT * FROM orders WHERE status = ? LOCK IN SHARE MODE",
            # A leading word that only starts with an admin verb.
            "ANALYZED_ROWS",
            "VACUUMING",
            "GRANTS",
            "ENDPOINT",
            "USER_LOOKUP",
            # START/UPDATE only count in their admin phrases.
            "START REPLICA_LIKE",
            "UPDATE statistics SET hits = hits + 1 WHERE id = ?",
            "UPDATE statistics_daily SET hits = ? WHERE id = ?",
            # COPY is application bulk I/O, never utility (#737 review).
            "COPY orders FROM STDIN WITH (FORMAT csv)",
            "COPY (SELECT status, count(*) FROM orders GROUP BY status) TO STDOUT WITH CSV",
            # A comment that mentions an admin verb, then application access.
            "/* run VACUUM later */ SELECT * FROM orders WHERE id = ?",
        ],
    )
    def test_admin_word_outside_the_leading_verb_is_not_utility(self, sql):
        assert is_utility_statement(sql, ["public.orders"]) is False


def _collected(
    sql: str,
    source_engine: str = "postgresql",
    known: frozenset[str] = frozenset({"badges", "posts"}),
) -> tuple[str, list[str]]:
    """One query through the offline collector, as the pipeline reads it."""
    from src.tools.database.offline_parser import _transform_queries

    q = _transform_queries(
        [{"query_text": sql}],
        "discourse",
        set(known),
        {t: {"public"} for t in known},
        source_engine=source_engine,
    )[0]
    return q["query_text"], q["tables_accessed"]


class TestSequenceReads:
    """A read of a PostgreSQL sequence's own state, or a sequence function called
    on its own, is sequence housekeeping, not an application read or write (#783).

    A sequence is a one-row relation (``last_value``, ``log_cnt``,
    ``is_called``), and it is not a schema table: the offline collector reports
    a sequence-state read of a relation the schema does not hold as ``unknown``,
    and the narrow detector rule then applies only when there is no real source
    table. So a real table that happens to be called ``invoice_seq`` stays an
    application table, and so does any read on a non-PostgreSQL source.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT last_value\nFROM public.badges_id_seq",
            "SELECT last_value FROM public.web_hook_event_types_id_seq",
            "SELECT last_value FROM badges_id_seq;",
            'SELECT "last_value" FROM "public"."badges_id_seq"',
            "SELECT last_value, is_called FROM public.badges_id_seq",
            "select last_value, log_cnt, is_called from badges_id_seq",
            "/* seed check */ SELECT last_value FROM public.badges_id_seq",
            # The reviewer's harmless misses (#797 review).
            "SELECT last_value FROM public.badges_id_seq -- seed check",
            "SELECT last_value FROM public.badges_id_seq /* seed */",
            "SELECT last_value FROM public.badges_id_seq LIMIT 1",
            "SELECT s.last_value FROM public.badges_id_seq s",
            "SELECT s.last_value FROM public.badges_id_seq AS s",
            "SELECT last_value AS v FROM public.badges_id_seq",
            "SELECT last_value FROM discourse.public.badges_id_seq",
            # Standalone sequence calls (#654), regressions.
            "SELECT nextval('posts_id_seq')",
            "SELECT nextval('posts_id_seq'::regclass) AS id",
            "SELECT currval('posts_id_seq')",
            "SELECT setval($1, $2, $3)",
            "SELECT setval('posts_id_seq', $1, true)",
            "SELECT lastval()",
        ],
    )
    def test_sequence_read_is_utility(self, sql):
        text, tables = _collected(sql)
        assert is_utility_statement(text, tables) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT last_value FROM public.badges_id_seq",
            "SELECT last_value FROM badges_id_seq LIMIT 1 -- note",
        ],
    )
    def test_the_collector_reports_a_sequence_as_no_table(self, sql):
        """The collector output change: the sequence is not a schema table."""
        _text, tables = _collected(sql)
        assert tables == ["unknown"]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT last_value FROM invoice_seq",
            "SELECT last_value FROM public.invoice_seq",
            "SELECT last_value FROM app.order_seq",
            "SELECT last_value, is_called FROM order_seq LIMIT 1",
            # PostgreSQL folds an unquoted name to lower case (#797 review).
            "SELECT last_value FROM INVOICE_SEQ",
            "SELECT last_value FROM Public.Invoice_Seq",
            "SELECT last_value FROM APP.ORDER_SEQ",
        ],
    )
    def test_a_schema_table_named_like_a_sequence_is_application_access(self, sql):
        """#797 review: a real table called ``invoice_seq`` is a table, not a sequence."""
        from src.tools.database.offline_parser import _transform_queries

        q = _transform_queries(
            [{"query_text": sql}],
            "app",
            {"invoice_seq", "order_seq"},
            {"invoice_seq": {"public"}, "order_seq": {"app"}},
            source_engine="postgresql",
        )[0]
        assert q["tables_accessed"] != ["unknown"]
        assert is_utility_statement(q["query_text"], q["tables_accessed"]) is False

    def test_a_quoted_name_matches_the_schema_case_sensitively(self):
        """A quoted identifier keeps its case, so ``"INVOICE_SEQ"`` is not the
        schema's ``invoice_seq``: it is a relation the schema does not hold."""
        text, tables = _collected(
            'SELECT last_value FROM "INVOICE_SEQ"', known=frozenset({"invoice_seq"})
        )
        assert tables == ["unknown"]
        text, tables = _collected(
            'SELECT last_value FROM "Invoice_Seq"', known=frozenset({"Invoice_Seq"})
        )
        assert tables == ["discourse.Invoice_Seq"]
        assert is_utility_statement(text, tables) is False

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE invoice_seq SET last_value = last_value + 1 WHERE id = $1",
            "INSERT INTO invoice_seq (id, last_value) VALUES ($1, $2)",
        ],
    )
    def test_writes_to_a_schema_table_named_like_a_sequence_stay_application_access(self, sql):
        text, tables = _collected(sql, known=frozenset({"invoice_seq"}))
        assert tables == ["discourse.invoice_seq"]
        assert is_utility_statement(text, tables) is False

    @pytest.mark.parametrize("source_engine", ["mysql", "sqlserver", "oracle"])
    def test_a_non_postgresql_source_has_no_sequence_relations(self, source_engine):
        """MySQL has no sequences; SQL Server and Oracle read them with NEXT VALUE
        FOR / seq.NEXTVAL. ``SELECT last_value FROM x_seq`` there is a table read."""
        text, tables = _collected(
            "SELECT last_value FROM invoice_seq", source_engine=source_engine, known=frozenset()
        )
        assert tables == ["invoice_seq"]
        assert is_utility_statement(text, tables) is False

    def test_without_the_collector_a_named_relation_is_not_assumed_a_sequence(self):
        """The detector alone cannot tell a sequence from a table it never saw."""
        assert is_utility_statement("SELECT last_value FROM invoice_seq", ["invoice_seq"]) is False
        assert (
            is_utility_statement("SELECT last_value FROM app.order_seq", ["app.order_seq"]) is False
        )

    @pytest.mark.parametrize(
        "sql",
        [
            # A sequence value used in an application write is the write.
            "INSERT INTO posts (id, title) VALUES (nextval('posts_id_seq'), $1)",
            "INSERT INTO posts (id, title) VALUES (posts_seq.NEXTVAL, :1)",
            # A real table with a column called last_value.
            "SELECT last_value FROM badges WHERE id = $1",
            "SELECT last_value FROM badges",
            # A sequence-named relation read with anything but its state columns.
            "SELECT id, name FROM badges_id_seq",
            "SELECT max(last_value) FROM badges_id_seq",
            # A filtered or joined read is not a plain sequence-state read.
            "SELECT last_value FROM public.badges_id_seq WHERE is_called",
            "SELECT s.last_value FROM badges_id_seq s JOIN badges b ON b.id = s.last_value",
            "SELECT last_value FROM badges_id_seq, badges",
            "SELECT last_value FROM badges_id_seq LIMIT 5",
        ],
    )
    def test_application_access_is_not_utility(self, sql):
        text, tables = _collected(sql)
        assert is_utility_statement(text, tables) is False


class TestCommentStrippingIsLinear:
    """#808: the trailing-comment regex backtracked exponentially on a run of
    ``--`` or ``*//*``; every entry point that strips comments now finishes a
    100k-character pathological input well under a second."""

    @pytest.mark.parametrize(
        "text",
        [
            "--" * 50_000 + "\nx",
            "SELECT last_value FROM badges_id_seq " + "--" * 50_000 + "\nx",
            "/*" + "*//*" * 25_000 + "x",
            "SELECT last_value FROM badges_id_seq /*" + "*//*" * 25_000 + "x",
        ],
        ids=["dashes", "dashes after a sequence read", "blocks", "blocks after a sequence read"],
    )
    def test_pathological_comments_finish_fast(self, text):
        import time

        from src.agents.referee.cache_overlay import _strip_leading_comments
        from src.agents.referee.utility_statements import is_sequence_state_read

        start = time.perf_counter()
        is_sequence_state_read(text)
        is_utility_statement(text, ["unknown"])
        _strip_leading_comments(text)
        assert time.perf_counter() - start < 1.0
