# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static checks on the collection scripts' routine sections and the live reads that
mirror them (#1652). No server runs here: the PostgreSQL reads run on a real server in
``tests/unit/tools/database/test_postgres_routine_round_trip.py`` and the MySQL ones in
``tests/integration/test_mysql_routine_create_round_trip.py``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _read(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text()


SQLSERVER = _read("scripts", "collect-sqlserver.sql")
POSTGRESQL = _read("scripts", "collect-postgresql.sql")
ORACLE = _read("scripts", "collect-oracle.sql")
MYSQL = _read("scripts", "collect-mysql.sql")
LIVE = {
    "sqlserver": _read("src", "tools", "database", "sqlserver_tools.py"),
    "postgresql": _read("src", "tools", "database", "postgres_tools.py"),
    "oracle": _read("src", "tools", "database", "oracle_tools.py"),
    "mysql": _read("src", "tools", "database", "mysql_tools.py"),
}


def _section(sql: str, title: str) -> str:
    return sql.split(f"-- ===================== {title}")[1].split("-- =====")[0]


def _code(text: str) -> str:
    """``text`` without its SQL ``--`` and Python ``#`` comments."""
    return re.sub(r"(--|#)[^\n]*", "", text)


# -- SQL Server -------------------------------------------------------------------


def test_sqlserver_procedures_and_triggers_carry_their_bodies():
    assert "OBJECT_DEFINITION(o.object_id) AS definition" in _section(SQLSERVER, "9. Procedures")
    triggers = _section(SQLSERVER, "10. Triggers")
    assert "OBJECT_DEFINITION(tr.object_id) AS definition" in triggers
    assert "CASE WHEN tr.is_disabled = 1 THEN 0 ELSE 1 END AS is_enabled" in triggers
    assert "'STATEMENT' AS for_each" in triggers


def test_sqlserver_counts_are_guarded_by_the_permission_and_recorded():
    section = _section(SQLSERVER, "10b. Routine stats")
    assert "IF HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') = 1" in section
    for view in ("dm_exec_procedure_stats", "dm_exec_function_stats", "dm_exec_trigger_stats"):
        assert f"sys.{view} WHERE database_id = DB_ID()" in section
    assert '"status":"not_tracked","reason":"VIEW SERVER STATE is not granted"' in section
    for key in ('"routine_stats":', '"routine_stats_status":'):
        assert key in SQLSERVER


@pytest.mark.passes_on_main
def test_sqlserver_procedures_section_reads_no_dmv():
    assert "dm_exec" not in _section(SQLSERVER, "9. Procedures")


def test_the_live_sqlserver_collector_reads_what_the_script_reads():
    live = LIVE["sqlserver"]
    for text in (
        "CASE WHEN {flag} = 1 THEN OBJECT_DEFINITION(o.object_id) END AS definition",
        "CASE WHEN {flag} = 1 THEN OBJECT_DEFINITION(tr.object_id) END AS definition",
        "CASE WHEN tr.is_disabled = 1 THEN 0 ELSE 1 END AS is_enabled",
        "HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')",
        "FROM sys.dm_exec_function_stats WHERE database_id = DB_ID()",
    ):
        assert text in live, text


# -- PostgreSQL -------------------------------------------------------------------


def test_postgresql_routines_come_from_pg_proc_with_their_whole_create():
    section = _section(POSTGRESQL, "8. Procedures & Functions")
    assert "FROM pg_proc pr" in section
    assert "information_schema.routines" not in _code(section)
    assert "pg_get_functiondef(pr.oid) END AS definition" in section
    assert "pr.prokind IN ('f', 'p')" in section
    for text in (
        "pr.proname || '_' || pr.oid AS specific_name",
        "pg_get_function_identity_arguments(pr.oid) AS identity_arguments",
        "pg_get_userbyid(pr.proowner) AS owner",
        "pr.prosecdef AS security_definer",
        "d.deptype = 'n'",
        "'[]'::json) AS depends_on",
        "length(pg_get_functiondef(pr.oid)) AS body_chars",
    ):
        assert text in section, text


def test_postgresql_leaves_out_the_routines_an_extension_owns():
    section = _section(POSTGRESQL, "8. Procedures & Functions")
    assert "x.refclassid = 'pg_extension'::regclass AND x.deptype = 'e')" in section
    assert "NOT EXISTS (" in section


def test_postgresql_triggers_come_from_pg_trigger_with_their_whole_create():
    section = _section(POSTGRESQL, "9. Triggers")
    assert "FROM pg_trigger t" in section and "NOT t.tgisinternal" in section
    assert "information_schema.triggers" not in _code(section)
    for text in (
        "pg_get_triggerdef(t.oid) END AS definition",
        "t.tgenabled::text AS is_enabled",
        "t.tgfoid::regprocedure::text AS function_signature",
        "THEN 'ROW' ELSE 'STATEMENT' END AS for_each",
        "n.nspname AS schema_name",
        # #1651's guard stays: a partition's clone is its parent's trigger.
        "coalesce(to_jsonb(t) ->> 'tgparentid', '0') = '0'",
    ):
        assert text in section, text


def test_postgresql_bodies_are_opt_out_and_the_capture_says_so():
    assert "\\set include_routine_bodies 1" in POSTGRESQL
    assert POSTGRESQL.count("CAST(:'routine_bodies_on' AS boolean)") == 5
    assert "NOT IN ('0', 'false', 'f', 'no', 'off') AS routine_bodies_on \\gset" in POSTGRESQL
    assert "'opted_out'" in POSTGRESQL
    assert "'routine_capture', json_build_object(" in POSTGRESQL


def test_postgresql_toplevel_is_read_so_older_servers_still_run():
    section = _section(POSTGRESQL, "10. Query Patterns")
    assert "(to_jsonb(s) ->> 'toplevel')::boolean AS toplevel" in section


def test_postgresql_function_counts_and_their_status():
    section = _section(POSTGRESQL, "9b. Routine stats")
    assert "FROM pg_stat_user_functions" in section
    assert "current_setting('track_functions') = 'none'" in section
    for key in ("'routine_stats'", "'routine_stats_status'"):
        assert key in POSTGRESQL


def test_the_live_postgresql_collector_reads_what_the_script_reads():
    """The live reads are the script's sections 8 and 9 for schema public (#1652)."""
    from src.tools.database.postgres_tools import _PROCEDURES_SQL, _TRIGGERS_SQL

    def body(name: str) -> str:
        cte = POSTGRESQL.split(f"{name} AS (", 1)[1].split("\n),\n", 1)[0]
        return cte.split("FROM (\n", 1)[1].rsplit("\n  ) ", 1)[0]

    def norm(sql: str) -> str:
        sql = sql.replace("CAST(:'routine_bodies_on' AS boolean)", "{flag} = 1")
        sql = re.sub(r"ORDER BY[^\n]*\s*$", "", sql.strip())
        return re.sub(r"\s+", " ", sql).strip()

    script_procs = norm(body("procedures_data")).replace(
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
        "AND pr.proname NOT LIKE 'pg_%'",
        "AND n.nspname = 'public'",
    )
    script_trigs = norm(body("triggers_data")).replace(
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema')", "AND n.nspname = 'public'"
    )
    assert norm(_PROCEDURES_SQL) == script_procs
    assert norm(_TRIGGERS_SQL) == script_trigs


# -- Oracle -----------------------------------------------------------------------


def test_oracle_bodies_come_one_source_line_per_row():
    assert "DBMS_OUTPUT.PUT_LINE('\"routine_sources\": [');" in ORACLE
    assert "FROM ALL_SOURCE" in ORACLE
    assert "'PACKAGE BODY'" in ORACLE and "'TRIGGER'" in ORACLE


def test_oracle_links_use_program_id_and_the_query_digest_rule():
    assert "DBMS_OUTPUT.PUT_LINE('\"query_routines\": [');" in ORACLE
    assert "JOIN ALL_OBJECTS o ON o.OBJECT_ID = s.PROGRAM_ID" in ORACLE
    digest = (
        "CASE WHEN NVL(s.FORCE_MATCHING_SIGNATURE, 0) <> 0 THEN 'fms_' || "
        "TO_CHAR(s.FORCE_MATCHING_SIGNATURE) ELSE s.SQL_ID END"
    )
    assert digest in ORACLE
    assert digest.replace("s.", "") in ORACLE  # the query section's own rule
    for key in ("'digest' VALUE", "'routine_schema' VALUE", "'routine_name' VALUE"):
        assert key in ORACLE


def test_oracle_triggers_keep_orientation_and_state():
    assert "'for_each' VALUE TRIGGER_TYPE" in ORACLE
    assert "'is_enabled' VALUE STATUS" in ORACLE


def test_oracle_records_that_it_keeps_no_routine_counts():
    assert '"routine_stats_status": {"status": "not_tracked"' in ORACLE


def test_the_live_oracle_collector_reads_what_the_script_reads():
    live = LIVE["oracle"]
    for text in ("FROM ALL_SOURCE", "'PACKAGE BODY'", "s.PROGRAM_ID <> 0", "STATUS AS is_enabled"):
        assert text in live, text


# -- MySQL ------------------------------------------------------------------------


def test_mysql_routine_rows_carry_what_their_create_is_rebuilt_from():
    section = _section(MYSQL, "8. Procedures & Functions")
    for key in (
        "'schema_name', routine_schema", "'specific_name', specific_name",
        "'definer', definer", "'security_type', security_type",
        "'is_deterministic', is_deterministic", "'sql_data_access', sql_data_access",
        "'routine_comment', routine_comment", "'external_language', external_language",
        "'character_set_client', character_set_client",
        "'collation_connection', collation_connection",
        "'database_collation', database_collation",
        "'body_chars', CHAR_LENGTH(routine_definition)",
    ):  # fmt: skip
        assert key in section, key
    assert "WHEN routine_definition IS NULL THEN 'not_readable'" in section


def test_mysql_trigger_rows_carry_their_schema_orientation_definer_and_order():
    section = _section(MYSQL, "9. Triggers")
    for key in ("'schema_name', trigger_schema", "'for_each', action_orientation",
                "'definer', definer", "'action_order', action_order"):  # fmt: skip
        assert key in section, key


def test_mysql_program_counts_are_read_only_when_the_table_exists():
    section = _section(MYSQL, "9b. Routine stats")
    assert "table_name = 'events_statements_summary_by_program'" in section
    assert "PREPARE routine_stats_stmt FROM @routine_stats_sql" in section
    assert "FROM performance_schema.events_statements_summary_by_program" in section
    assert "WHERE OBJECT_SCHEMA = @db" in section


def test_mysql_parameters_events_and_the_capture_are_read():
    assert "FROM information_schema.parameters" in MYSQL
    assert "'dtd_identifier', dtd_identifier" in MYSQL
    assert "FROM information_schema.events" in MYSQL
    assert "'interval_field', interval_field" in MYSQL
    assert "information_schema.user_privileges" in MYSQL
    for line in (
        "'routine_stats', IFNULL(JSON_EXTRACT(@routine_stats, '$'), JSON_ARRAY())",
        "'routine_stats_status', JSON_EXTRACT(@routine_stats_status, '$')",
        "'routine_parameters', IF(@parameters_counted = 0, JSON_ARRAY(), "
        "JSON_EXTRACT(@routine_parameters, '$'))",
        "'events', IFNULL(JSON_EXTRACT(@events, '$'), JSON_ARRAY())",
        "'$.routine_capture', JSON_EXTRACT(@routine_capture, '$')",
    ):
        assert line in MYSQL, line


def test_mysql_bodies_are_opt_out():
    assert (
        "SET @include_routine_bodies = IF(CAST(@include_routine_bodies AS CHAR) = '0', 0, 1);"
        in MYSQL
    )
    # routines, triggers and events: the body and its status; the capture
    assert MYSQL.count("IF(@include_routine_bodies = 1,") == 6
    assert "WHEN @include_routine_bodies = 0 THEN 'opted_out'" in MYSQL


def test_the_live_mysql_collector_reads_what_the_script_reads():
    for text in (
        "'not_readable'", "FROM information_schema.parameters", "FROM information_schema.events",
        "information_schema.user_privileges", "CHAR_LENGTH(routine_definition) AS body_chars",
        "action_order", "dtd_identifier", "events_statements_summary_by_program",
    ):  # fmt: skip
        assert text in LIVE["mysql"], text


# -- every engine -----------------------------------------------------------------


# Statements that write, grant or change a setting; none may appear in a collection
# script or a live collector's SQL (case-sensitive: SQL keywords, not prose).
_WRITES = re.compile(
    r"(?<!SHOW )\bCREATE\s+(?:OR\s+\w+\s+)?(?:TABLE|INDEX|VIEW|PROCEDURE|FUNCTION|TRIGGER|EVENT"
    r"|ROLE|USER|DATABASE|SCHEMA|EXTENSION|SEQUENCE|TYPE)\b"
    r"|\b(?:ALTER|DROP)\s+(?:TABLE|INDEX|VIEW|PROCEDURE|FUNCTION|TRIGGER|EVENT|ROLE|USER"
    r"|DATABASE|SCHEMA|EXTENSION|SEQUENCE|SYSTEM|SESSION|SERVER)\b"
    r"|\b(?:GRANT|REVOKE)\s+\w+.*\b(?:ON|TO|FROM)\b"
    r"|\bINSERT\s+INTO\s+(?!@)\w|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b|\bTRUNCATE\s+"
    r"|\bSET\s+(?:GLOBAL|PERSIST|PERSIST_ONLY)\b|\bset_config\s*\(|\bpg_reload_conf\b"
    r"|\bALTER\s+SYSTEM\b|\bEXEC(?:UTE)?\s+sp_configure\b|\bDBMS_STATS\.\w+|\bSHOW\s+\w+"
)
# The reads that predate #1652 and #1652's own SHOW CREATE, each where it is allowed.
_ALLOWED = {
    "mysql": {"SHOW GLOBAL", "SHOW CREATE"},
    "postgresql": set(),
    "sqlserver": set(),
    "oracle": set(),
}


# #1918's LOB probe keeps a flag in its own session variable, modernizer.lob_xml_ok; that
# private name is not a server setting, so only it is let through.
_OWN_FLAG = re.compile(r"\bset_config\s*\(\s*'modernizer\.lob_xml_ok'")


def _writes(text: str) -> set[str]:
    code = _OWN_FLAG.sub("", _code(text))
    return {" ".join(m.group(0).split()[:2]) for m in _WRITES.finditer(code)}


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", ["mysql", "postgresql", "sqlserver", "oracle"])
def test_no_script_or_live_read_writes_or_changes_a_setting(name):
    """Collectors stay read-only (#1652): no DDL, DML on a table, grant, setting change or
    SHOW in a script or a live collector's SQL, but for the reads named in _ALLOWED (live
    MySQL's SHOW GLOBAL STATUS and SHOW CREATE, the server's own routine text)."""
    script = _read("scripts", f"collect-{name}.sql")
    assert _writes(script) == set(), name
    assert _writes(LIVE[name]) <= _ALLOWED[name], name
    assert "track_functions =" not in _code(script).replace("track_functions is", "")


def test_the_only_show_create_is_the_live_mysql_read_of_a_routine():
    live = _code(LIVE["mysql"])
    assert live.count("SHOW CREATE {keyword}") == 1
    assert not re.search(r"SHOW\s+CREATE", _code(MYSQL))


@pytest.mark.passes_on_main
def test_the_write_check_catches_each_form():
    for text in ("CREATE TABLE t (a int)", "DROP INDEX i", "GRANT SELECT ON t TO u",
                 "INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t",
                 "TRUNCATE t", "SET GLOBAL x = 1", "SELECT set_config('a', 'b', false)",
                 "ALTER SYSTEM SET x = 1", "EXEC sp_configure 'x'", "SHOW VARIABLES"):  # fmt: skip
        assert _writes(text), text
    assert _writes("INSERT INTO @rate_sample_start SELECT 1; SET @db = DATABASE();") == set()
