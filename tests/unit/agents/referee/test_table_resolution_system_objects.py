"""``is_engine_system_object`` (#380 review): a database-engine-internal
object (a system catalog schema, a stats-extension table/view) is never
counted as part of "the whole database" wave 1 carries over -- but a real
application table must never be excluded just because no later wave happens
to map it, or because its name merely looks like a sequence.

#380 round 2 review: this is called on ``known_tables``, the collector's own
verified tables and views. The collector contract has no ``Sequence`` model
at all, so a name here that only *looks* like a sequence (``orders_seq``) is
always a real collected table -- an earlier revision's ``_seq$`` suffix
heuristic could only misclassify real tables (``order_seq``, ``shop.item_seq``)
and has been removed. A schema design's own hallucinated sequence name
(``public.badge_groupings_id_seq``) is a different problem, already solved
by ``build_table_mappings``'s own ``TableNameResolver`` check (see
``test_synthesis_table_mappings_noise.py``): it never reaches ``known_tables``
in the first place, because no such table exists in the collector's schema.
"""

from __future__ import annotations

from src.agents.referee.table_resolution import is_engine_system_object


class TestIsEngineSystemObject:
    def test_system_schema_is_an_engine_object(self) -> None:
        assert is_engine_system_object("information_schema.tables") is True
        assert is_engine_system_object("pg_catalog.pg_class") is True
        assert is_engine_system_object("performance_schema.events") is True
        assert is_engine_system_object("mysql.user") is True

    def test_stats_extension_object_is_an_engine_object_regardless_of_schema(self) -> None:
        assert is_engine_system_object("pg_stat_statements") is True
        assert is_engine_system_object("public.pg_stat_statements") is True
        assert is_engine_system_object("pg_stat_statements_info") is True

    def test_a_real_application_table_is_not_an_engine_object(self) -> None:
        assert is_engine_system_object("discourse.ar_internal_metadata") is False
        assert is_engine_system_object("discourse.chat_drafts") is False
        assert is_engine_system_object("discourse.github_commits") is False
        assert is_engine_system_object("orders") is False

    def test_a_real_collected_table_whose_name_merely_looks_like_a_sequence_stays(self) -> None:
        # #380 round 2: the collector has no Sequence model, so a "known
        # table" named this way is always real -- never an actual sequence.
        assert is_engine_system_object("orders_seq") is False
        assert is_engine_system_object("order_seq") is False
        assert is_engine_system_object("shop.item_seq") is False
        assert is_engine_system_object("inventory_seq") is False

    def test_empty_or_none_is_not_an_engine_object(self) -> None:
        assert is_engine_system_object("") is False


class TestOneSystemTableRule:
    """#831 review: routing's utility rule and schema design use this one
    function too, so the three system-table rules can't disagree."""

    def test_utility_statements_has_no_system_table_rule_of_its_own(self) -> None:
        import src.agents.referee.utility_statements as us

        assert not hasattr(us, "is_system_catalog_table")
        assert us.is_engine_system_object is is_engine_system_object

    def test_an_unqualified_pg_name_is_the_catalog(self) -> None:
        assert is_engine_system_object("pg_class") is True
        assert is_engine_system_object("pg_index") is True
        assert is_engine_system_object("information_schema") is True

    def test_a_pg_named_table_in_an_application_schema_is_an_application_table(self) -> None:
        assert is_engine_system_object("shop.pg_style_settings") is False

    def test_a_system_schema_counts_at_any_qualifier(self) -> None:
        assert is_engine_system_object("db.sys.objects") is True
        assert is_engine_system_object("discourse.information_schema.columns") is True
        assert is_engine_system_object("sys.app.orders") is True
        assert is_engine_system_object("sysadmin.jobs") is False
        assert is_engine_system_object("app.sys_users") is False

    def test_quotes_and_case_are_ignored(self) -> None:
        assert is_engine_system_object('"pg_catalog"."pg_class"') is True
        assert is_engine_system_object("[sys].[objects]") is True
        assert is_engine_system_object("Public.PG_STAT_ACTIVITY") is True


class TestCollectedDatabaseNameIsNotASchema:
    """#835: ``<database>.<table>`` ids lead with the collected database's own
    name, so a database literally named ``sys`` or ``mysql`` is not a system
    schema. Only a real system-schema position for the engine is."""

    def test_a_database_named_sys_holds_application_tables(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        source = SourceScope(name="sys", engine="postgresql")
        assert is_engine_system_object("sys.public.orders", source) is False
        assert is_engine_system_object("sys.orders", SourceScope("sys", "mysql")) is False
        assert is_engine_system_object("mysql.users", SourceScope("mysql", "mysql")) is False
        performance = SourceScope("performance_schema", "mysql")
        assert is_engine_system_object("performance_schema.t", performance) is False

    def test_the_real_system_schema_inside_that_database_is_still_system(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        assert is_engine_system_object("sys.sys.objects", SourceScope("sys", "sqlserver")) is True
        assert is_engine_system_object("shop.sys.objects", SourceScope("shop", "sqlserver")) is True
        assert is_engine_system_object("shop.mysql.user", SourceScope("shop", "mysql")) is True
        assert is_engine_system_object("mysql.user", SourceScope("shop", "mysql")) is True
        assert is_engine_system_object("sys.public.pg_stat_statements", SourceScope("sys")) is True
        info = SourceScope("sys", "postgresql")
        assert is_engine_system_object("sys.information_schema.tables", info) is True

    def test_only_the_engines_own_system_schemas_count(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        pg = SourceScope("shop", "postgresql")
        assert is_engine_system_object("shop.sys.orders", pg) is False
        assert is_engine_system_object("shop.mysql.orders", pg) is False
        assert is_engine_system_object("shop.pg_catalog.pg_class", pg) is True
        mysql = SourceScope("shop", "mysql")
        assert is_engine_system_object("shop.pg_catalog.pg_class", mysql) is False

    def test_a_pg_named_table_behind_the_database_prefix_stays_an_application_table(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        assert (
            is_engine_system_object("shop.pg_style_settings", SourceScope("shop", "mysql")) is False
        )

    def test_no_source_keeps_the_old_rule(self) -> None:
        assert is_engine_system_object("sys.public.orders") is True
        assert is_engine_system_object("sys.public.orders", None) is True

    def test_scope_reads_the_collector_output(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        scope = SourceScope.from_collector(
            {"metadata": {"source_database": {"engine": "PostgreSQL", "database_name": "sys"}}}
        )
        assert (scope.name, scope.engine) == ("sys", "postgresql")
        assert SourceScope.from_collector({}) == SourceScope(None, None)

    def test_every_pg_schema_is_a_postgresql_system_schema(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        pg = SourceScope("shop", "postgresql")
        assert is_engine_system_object("shop.pg_toast.pg_toast_1", pg) is True
        assert is_engine_system_object("shop.pg_temp_3.t", pg) is True
        assert is_engine_system_object("shop.pg_toast_temp_3.t", pg) is True
        assert is_engine_system_object("shop.pg_toast.t", SourceScope("shop", "mysql")) is False

    def test_oracle_and_db2_have_their_own_system_schemas(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        oracle = SourceScope("orcl", "oracle")
        assert is_engine_system_object("orcl.system.logmnr_uid$", oracle) is True
        assert is_engine_system_object("orcl.xdb.t", oracle) is True
        assert is_engine_system_object("orcl.mysql.t", oracle) is False
        assert is_engine_system_object("orcl.pg_catalog.t", oracle) is False
        db2 = SourceScope("sample", "db2")
        assert is_engine_system_object("sample.syscat.tables", db2) is True
        assert is_engine_system_object("sample.sysibm.sysdummy1", db2) is True
        assert is_engine_system_object("sample.sys.orders", db2) is False

    def test_scope_normalizes_its_fields(self) -> None:
        from src.agents.referee.table_resolution import SourceScope

        scope = SourceScope(name="  ", engine="PostgreSQL")
        assert (scope.name, scope.engine) == (None, "postgresql")
        assert (
            is_engine_system_object("shop.sys.orders", SourceScope("shop", "PostgreSQL")) is False
        )


def test_a_partly_qualified_name_resolves_when_the_bare_name_is_in_two_schemas():
    """#1207: ``s1.b`` is the tail of exactly one id; the bare ``b`` stays ambiguous."""
    from src.agents.referee.table_resolution import TableNameResolver

    resolver = TableNameResolver.from_known_ids(["db.s1.b", "db.s2.b", "db.s1.c"])
    assert resolver is not None
    assert resolver.resolve("b") is None
    assert resolver.resolve("s1.b") == "db.s1.b"
    assert resolver.resolve('"S2"."B"') == "db.s2.b"
    assert resolver.resolve("s3.b") is None
    assert resolver.resolve("c") == "db.s1.c"
