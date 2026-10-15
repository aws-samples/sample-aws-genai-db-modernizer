"""#835: a source database literally named ``sys`` (or ``mysql``) is not a
system schema, so its application workload is not read as system/utility.
Every consumer of ``is_engine_system_object`` gets the collected database."""

from __future__ import annotations

from src.agents.referee.capability_registry import detect_required_capabilities
from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.triage import detect_query_capabilities
from src.agents.referee.utility_statements import is_utility_statement

ORDERS_SQL = "SELECT * FROM sys.public.orders WHERE id = 1"


def _collector(database: str, engine: str = "postgresql") -> dict:
    return {
        "metadata": {"source_database": {"engine": engine, "database_name": database}},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": ORDERS_SQL,
                    "tables_accessed": ["sys.public.orders"],
                }
            ]
        },
    }


class TestUtilityStatement:
    def test_orders_in_a_database_named_sys_is_application_traffic(self) -> None:
        scope = SourceScope.from_collector(_collector("sys"))
        assert is_utility_statement(ORDERS_SQL, ["sys.public.orders"], scope) is False

    def test_real_system_objects_in_that_database_are_still_utility(self) -> None:
        scope = SourceScope.from_collector(_collector("sys", "sqlserver"))
        assert is_utility_statement("SELECT name FROM sys.objects", ["sys.sys.objects"], scope)
        assert is_utility_statement("SELECT * FROM pg_class", ["pg_class"], scope)

    def test_without_a_collected_database_the_old_rule_applies(self) -> None:
        assert is_utility_statement(ORDERS_SQL, ["sys.public.orders"]) is True


class TestCapabilityGate:
    def test_no_sql_admin_requirement_for_the_application_table(self) -> None:
        scope = SourceScope("sys", "postgresql")
        assert "sql_admin" not in detect_required_capabilities(
            ORDERS_SQL, [], ["sys.public.orders"], scope
        )

    def test_triage_reads_the_collectors_database(self) -> None:
        assert "sql_admin" not in detect_query_capabilities(_collector("sys"), []).get("q1", [])


class TestMigrationWaves:
    def _waves(self, scope: SourceScope | None) -> list[dict]:
        tables = ["sys.public.orders", "sys.public.items"]
        waves = build_migration_waves(
            ranking=[
                {"target": "aurora_postgresql", "assigned_queries": 2, "workload_percent": 100}
            ],
            table_assignments=[
                {
                    "table_id": t,
                    "primary_engine": "aurora_postgresql",
                    "engines": ["aurora_postgresql"],
                    "query_count": 1,
                }
                for t in tables
            ],
            query_assignments=[],
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="postgresql",
            known_tables=set(tables),
            source=scope,
        )
        return waves or []

    def test_wave_one_carries_the_application_tables(self) -> None:
        waves = self._waves(SourceScope("sys", "postgresql"))
        assert sorted(waves[0]["tables"]) == ["sys.public.items", "sys.public.orders"]

    def test_without_the_database_name_they_were_dropped(self) -> None:
        assert not any(w.get("tables") for w in self._waves(None))
