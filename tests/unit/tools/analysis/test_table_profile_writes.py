"""A write counts against the tables it writes, never one it only reads (#1287).

ADR-031 Amendment 9: a table's writes are its targets. ``build_table_profiles`` used to
count a write against every table the statement touches, including one it only reads in
a CTE or a FROM.
"""

from __future__ import annotations

import pytest

from src.contracts.analysis_output import WorkloadAnalysis
from src.tools.analysis.scoring import build_table_profiles


def _collector(engine: str, *queries: dict) -> dict:
    return {
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {
            "tables": [
                {"table_id": t}
                for t in ("groups", "group_users", "user_actions", "stats", "audit_log")
            ]
        },
        "queries": {"query_patterns": list(queries)},
    }


def _query(qid: str, qtype: str, text: str, tables: list[str]) -> dict:
    return {
        "query_id": qid,
        "query_type": qtype,
        "query_text": text,
        "tables_accessed": tables,
        "calls_per_second": 1.0,
    }


def _profiles(engine: str, *queries: dict):
    return build_table_profiles(
        _collector(engine, *queries), WorkloadAnalysis(patterns_detected=[])
    )


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("qtype", "text"),
    [
        (
            "UPDATE",
            "WITH a AS (SELECT user_id FROM user_actions) "
            "UPDATE groups g SET user_count = 1 FROM a WHERE g.id = a.user_id",
        ),
        (
            "UPDATE",
            "UPDATE groups g SET user_count = 1 FROM group_users gu WHERE gu.group_id = g.id",
        ),
        ("DELETE", "DELETE FROM groups USING group_users gu WHERE gu.group_id = groups.id"),
        (
            "INSERT",
            "INSERT INTO groups (id) SELECT group_id FROM group_users",
        ),
    ],
)
def test_a_table_read_only_by_a_write_has_zero_writes(qtype: str, text: str) -> None:
    tables = [t for t in ("groups", "group_users", "user_actions") if t in text]
    profiles = _profiles("postgresql", _query("w", qtype, text, tables))
    assert profiles["groups"].write_query_count == 1
    for read_only in ("group_users", "user_actions"):
        assert profiles[read_only].write_query_count == 0, read_only


def test_a_with_led_write_counts_against_its_target() -> None:
    """Typed UPDATE since #1287; the CTE source table stays unwritten."""
    text = (
        "WITH a AS (SELECT user_id FROM user_actions) "
        "UPDATE groups g SET user_count = 1 FROM a WHERE g.id = a.user_id"
    )
    profiles = _profiles("postgresql", _query("w", "UPDATE", text, ["groups", "user_actions"]))
    assert profiles["groups"].write_query_count == 1
    assert profiles["user_actions"].write_query_count == 0


@pytest.mark.passes_on_main
def test_a_read_still_counts_as_a_read_on_every_table_it_touches() -> None:
    profiles = _profiles(
        "postgresql",
        _query(
            "r",
            "SELECT",
            "SELECT 1 FROM groups JOIN group_users USING (id)",
            ["groups", "group_users"],
        ),
    )
    assert profiles["groups"].read_query_count == 1
    assert profiles["group_users"].read_query_count == 1


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("qtype", "text", "target"),
    [
        (
            "UPDATE",
            "UPDATE groups SET a = 1 OUTPUT deleted.a INTO audit_log (a) WHERE id = 2",
            "audit_log",
        ),
        (
            "DELETE",
            "DELETE FROM groups OUTPUT deleted.id INTO dbo.audit_log WHERE id = 2",
            "audit_log",
        ),
        (
            "INSERT",
            "INSERT INTO groups (a) OUTPUT inserted.id INTO [audit_log] VALUES (1)",
            "audit_log",
        ),
    ],
)
def test_a_sql_server_output_into_target_is_written(qtype: str, text: str, target: str) -> None:
    """``OUTPUT ... INTO <table>`` writes that table as well as the statement's own target."""
    profiles = _profiles("sqlserver", _query("w", qtype, text, ["groups", target]))
    assert profiles["groups"].write_query_count == 1
    assert profiles[target].write_query_count == 1
    assert profiles["group_users"].write_query_count == 0


def test_an_output_into_a_temp_table_or_variable_writes_no_collected_table() -> None:
    for text in (
        "DELETE FROM groups OUTPUT deleted.id INTO #removed WHERE id = 2",
        "DELETE FROM groups OUTPUT deleted.id INTO @removed WHERE id = 2",
    ):
        profiles = _profiles("sqlserver", _query("w", "DELETE", text, ["groups", "audit_log"]))
        assert profiles["groups"].write_query_count == 1
        assert profiles["audit_log"].write_query_count == 0


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ["postgresql", "mysql", "sqlserver"])
def test_an_insert_select_from_a_catalog_still_writes_its_target(engine: str) -> None:
    text = "INSERT INTO stats (n) SELECT COUNT(*) FROM information_schema.tables"
    profiles = _profiles(
        engine, _query("w", "INSERT", text, ["stats", "information_schema.tables"])
    )
    assert profiles["stats"].write_query_count == 1


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ["postgresql", "mysql", "sqlserver"])
def test_an_administration_statement_still_writes_no_table(engine: str) -> None:
    for qtype, text in (("OTHER", "TRUNCATE TABLE stats"), ("OTHER", "ANALYZE stats")):
        profiles = _profiles(engine, _query("w", qtype, text, ["stats"]))
        assert profiles["stats"].write_query_count == 0


# #1348: the profile counts a write exactly when the write group's target helper
# (``written_tables``) names the table, whatever the statement's ``query_type``.

_CTE_INSERT = (
    "WITH w AS (INSERT INTO groups (id) SELECT group_id FROM group_users RETURNING id) "
    "SELECT id FROM w"
)
_CTE_DELETE = "WITH d AS (DELETE FROM stats WHERE n < 0 RETURNING n) SELECT count(*) FROM d"


@pytest.mark.parametrize(
    ("text", "target", "read_only"),
    [
        (_CTE_INSERT, "groups", ["group_users"]),
        (_CTE_DELETE, "stats", []),
    ],
)
def test_a_data_modifying_cte_ending_in_select_writes_its_target(
    text: str, target: str, read_only: list[str]
) -> None:
    """Collected as ``OTHER`` (the facade reads it as a select): one write of its target,
    no write of a table it only reads, and no read counted for it."""
    profiles = _profiles("postgresql", _query("w", "OTHER", text, [target, *read_only]))
    assert profiles[target].write_query_count == 1
    assert profiles[target].read_query_count == 0
    for table in read_only:
        assert profiles[table].write_query_count == 0, table


@pytest.mark.passes_on_main
@pytest.mark.parametrize("qtype", ["SELECT", "OTHER"])
def test_a_plain_select_writes_no_table(qtype: str) -> None:
    text = "WITH a AS (SELECT id FROM group_users) SELECT g.id FROM groups g JOIN a USING (id)"
    profiles = _profiles("postgresql", _query("r", qtype, text, ["groups", "group_users"]))
    for table in ("groups", "group_users"):
        assert profiles[table].write_query_count == 0, (qtype, table)


@pytest.mark.passes_on_main
def test_a_dml_keyword_in_a_mysql_comment_is_no_write() -> None:
    """The text is read in the source dialect: ``#`` opens a MySQL comment."""
    text = "SELECT n FROM stats # DELETE FROM stats"
    profiles = _profiles("mysql", _query("r", "OTHER", text, ["stats"]))
    assert profiles["stats"].write_query_count == 0


def test_a_locking_read_counts_as_a_write_of_the_tables_it_locks() -> None:
    """A locking read is in its tables' write groups (ADR-031 Amendment 9), so the
    profile counts it with their writes, once, not also as a read."""
    text = "SELECT id FROM groups WHERE id = 1 FOR UPDATE"
    profiles = _profiles("postgresql", _query("l", "SELECT", text, ["groups"]))
    assert profiles["groups"].write_query_count == 1
    assert profiles["groups"].read_query_count == 0


def test_every_profile_write_count_equals_its_write_group() -> None:
    from src.agents.referee.write_groups import collector_write_groups

    queries = [
        _query("cte", "OTHER", _CTE_INSERT, ["groups", "group_users"]),
        _query("del", "OTHER", _CTE_DELETE, ["stats"]),
        _query("upd", "UPDATE", "UPDATE groups SET a = 1 WHERE id = 2", ["groups"]),
        _query("sel", "SELECT", "SELECT 1 FROM group_users", ["group_users"]),
        _query("trunc", "OTHER", "TRUNCATE TABLE audit_log", ["audit_log"]),
    ]
    collector = _collector("postgresql", *queries)
    groups = collector_write_groups(collector)
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    assert {t: p.write_query_count for t, p in profiles.items() if p.write_query_count} == {
        t: len(m) for t, m in groups.items()
    }
    assert profiles["groups"].write_query_count == 2
