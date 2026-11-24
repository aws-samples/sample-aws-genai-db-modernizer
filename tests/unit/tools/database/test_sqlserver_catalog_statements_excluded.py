# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SQL Server collection leaves SQL Server's own catalog statements out (#1894).

SQL Server's tooling, telemetry and the collection script itself read the system catalog
views and DMVs (``sys.*``, ``INFORMATION_SCHEMA``, ``msdb``) and call the system catalog
procedures (``sp_help``, ``sp_columns``). Those statements read the catalog, not the
application's data, so the collector leaves them out of the workload and records each.
Every statement here is synthetic: written for the test, never copied from a collection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.referee.triage_handler import run_triage
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.triage_output import TriageOutputContract
from src.shared.excluded_statements import excluded_statement_count, excluded_statement_facts
from src.storage.local_store import LocalArtifactStore
from src.tools.database.migration_tool_statements import split_migration_tool_statements
from src.tools.database.offline_parser import parse_offline_collection
from tests.unit.agents.test_collector_source_host_live import ENGINES, TABLE, _Checkpoints, _input

# The rule ids, as the collector output records them.
SQLSERVER_CATALOG = "sqlserver_catalog"
SQLSERVER_SYSTEM_PROCEDURE = "sqlserver_system_procedure"

APP_READ = "SELECT OrderId, Total FROM dbo.Orders WHERE OrderId = @p1"
APP_WRITE = "UPDATE dbo.Orders SET Total = @p1 WHERE OrderId = @p2"
INDEXES = (
    "SELECT o.name, i.name, p.rows FROM sys.objects o "
    "JOIN sys.indexes i ON i.object_id = o.object_id "
    "JOIN sys.partitions p ON p.object_id = i.object_id AND p.index_id = i.index_id "
    "WHERE o.type = N'U'"
)
PARAMETERS = (
    "SELECT o.name, p.name, t.name FROM [sys].[objects] AS o "
    "INNER JOIN [sys].[parameters] AS p ON p.[object_id] = o.[object_id] "
    "INNER JOIN [sys].[types] AS t ON t.user_type_id = p.user_type_id"
)
# A telemetry-like count of scalar functions; the zero-width spaces (U+200B) keep the T-SQL
# parser from reading it as written.
TELEMETRY = (
    "SELECT​ COUNT(*) FROM sys.objects o WHERE o.type IN (N'FN', N'IF') "
    "AND​ OBJECT_DEFINITION(o.object_id) LIKE N'%getdate%'"
)
INFO_SCHEMA = "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS ORDER BY 1"
MSDB = "SELECT j.name FROM msdb.dbo.sysjobs j WHERE j.enabled = 1"
SERVER_WIDE = "SELECT name, state_desc FROM master.sys.databases"
DMV = (
    "SELECT r.session_id, t.text FROM sys.dm_exec_requests r "
    "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t"
)
SP_HELP = "EXEC sp_help N'dbo.Orders'"
SP_COLUMNS = "exec [sys].[sp_columns] @table_name = N'Orders', @table_owner = N'dbo'"


def _row(digest: str, text: str, calls: int = 10, **extra: Any) -> dict[str, Any]:
    return {
        "digest": digest,
        "query_text": text,
        "database_name": "Shop",
        "execution_count": calls,
        "total_time_ms": 1.0,
        "avg_time_ms": 0.1,
        **extra,
    }


def _collection(queries: list[dict], **extra: Any) -> dict[str, Any]:
    return {
        "metadata": {"version": "Microsoft SQL Server 2022 (RTM) 16.0", "database_name": "Shop"},
        "tables": [{"table_name": "Orders", "schema_name": "dbo", "row_count": 10}],
        "columns": [
            {"table_name": "Orders", "column_name": c, "data_type": "int", "is_nullable": "NO"}
            for c in ("OrderId", "Total", "Name")
        ],
        "primary_keys": [{"table_name": "Orders", "column_name": "OrderId", "ordinal_position": 1}],
        "queries": queries,
        **extra,
    }


def _offline(queries: list[dict], **extra: Any) -> dict[str, Any]:
    out = build_offline_collector_output(
        parse_offline_collection(_collection(queries, **extra)),
        job_id="j1894",
        database_name="Shop",
        s3_key="k",
    )
    CollectorOutputContract.model_validate(out)
    return out


def _query_ids(out: dict) -> list[str]:
    return [q["query_id"] for q in out["queries"]["query_patterns"]]


def _excluded(out: dict) -> dict[str, str]:
    return {e["query_id"]: e["rule"] for e in out["queries"].get("excluded_statements") or []}


def _split(rows: list[dict], engine: str = "sqlserver") -> tuple[list[str], dict[str, str]]:
    kept, excluded = split_migration_tool_statements(rows, engine)
    return [r["digest"] for r in kept], {e["query_id"]: e["rule"] for e in excluded}


CATALOG_ROWS = [
    _row("0x02", INDEXES, calls=2),
    _row("0x03", PARAMETERS, calls=1),
    _row("0x04", TELEMETRY, calls=1),
    _row("0x05", INFO_SCHEMA, calls=1),
    _row("0x06", MSDB, calls=1),
    _row("0x07", SERVER_WIDE, calls=1),
    _row("0x08", DMV, calls=1),
    _row("0x09", SP_HELP, calls=2),
    _row("0x0A", SP_COLUMNS, calls=1),
]


# --- the offline ingest -------------------------------------------------------------


def test_a_collection_leaves_out_sql_servers_own_catalog_statements():
    out = _offline([_row("0x01", APP_READ, calls=500), *CATALOG_ROWS])
    assert _query_ids(out) == ["0x01"]
    assert _excluded(out) == {
        **{r["digest"]: SQLSERVER_CATALOG for r in CATALOG_ROWS[:7]},
        "0x09": SQLSERVER_SYSTEM_PROCEDURE,
        "0x0A": SQLSERVER_SYSTEM_PROCEDURE,
    }
    by_id = {e["query_id"]: e for e in out["queries"]["excluded_statements"]}
    assert by_id["0x02"]["execution_count"] == 2
    assert by_id["0x02"]["reason"] == (
        "reads only SQL Server's system catalog views and DMVs "
        "(sys.indexes, sys.objects, sys.partitions)"
    )
    assert by_id["0x06"]["reason"].endswith("(msdb.dbo.sysjobs)")
    assert by_id["0x09"]["reason"] == "runs only SQL Server's system catalog procedure sp_help"
    assert by_id["0x0A"]["reason"] == "runs only SQL Server's system catalog procedure sp_columns"


@pytest.mark.parametrize(
    "text",
    [
        # A customer statement that also reads an application table.
        "SELECT o.OrderId FROM dbo.Orders o JOIN sys.objects s ON s.name = o.Name",
        "SELECT name FROM sys.objects WHERE name IN (SELECT Name FROM dbo.Orders)",
        "SELECT name FROM sys.objects; SELECT OrderId FROM dbo.Orders",
        "INSERT INTO dbo.Orders (OrderId) SELECT object_id FROM sys.objects",
        "SELECT OrderId FROM Orders WHERE EXISTS (SELECT 1 FROM sys.tables WHERE name = N'x')",
        # A catalog read that calls the application's own function or procedure.
        "SELECT dbo.fn_total(object_id) FROM sys.objects",
        "SELECT o.name FROM sys.objects o CROSS APPLY dbo.fn_parts(o.object_id) f",
        "SELECT o.name FROM sys.objects o CROSS APPLY fn_parts(o.object_id) f",
        "SELECT name FROM sys.objects; EXEC dbo.usp_refresh",
        # sp_executesql and the cursor procedures carry the application's own statements.
        "exec sp_executesql N'SELECT OrderId FROM dbo.Orders WHERE OrderId = @p', N'@p int', 1",
        "exec sp_executesql N'SELECT name FROM sys.objects'",
        "EXEC sp_cursoropen @c OUTPUT, N'SELECT OrderId FROM dbo.Orders'",
        "EXEC sp_prepexec @h OUTPUT, NULL, N'SELECT OrderId FROM dbo.Orders'",
        # The application's own procedures, and a procedure call with a second statement.
        "EXEC dbo.sp_help_orders @id = 1",
        "EXEC sp_help N'dbo.Orders'; SELECT OrderId FROM dbo.Orders",
        "EXEC usp_GetOrders @id = 1",
        # Catalog words only in a comment or a string, and statements naming no table.
        "/* sys.objects */ SELECT OrderId FROM dbo.Orders",
        "SELECT N'sys.objects' AS note, OrderId FROM dbo.Orders",
        "SELECT @@VERSION",
        "SELECT SERVERPROPERTY(N'ProductVersion')",
        # A schema or a table whose name only resembles a catalog's.
        "SELECT * FROM sysobjects WHERE xtype = N'U'",
        "SELECT * FROM dbo.sysjobs WHERE enabled = 1",
        "SELECT * FROM system.objects",
        "SELECT * FROM sys_audit.objects",
        "SELECT * FROM Shop.dbo.information_schema_notes",
        # Linked-server (four-part) and ``..`` names, which name no table the parser
        # returns, joined to or filtered by a catalog view (review of #2590).
        "DELETE FROM lnk.appdb.dbo.orders WHERE id IN (SELECT object_id FROM sys.objects)",
        "UPDATE lnk.appdb.dbo.orders SET n = 1 FROM sys.objects s",
        "SELECT o.id FROM [lnk].[appdb].[dbo].[orders] o JOIN sys.objects s ON s.name = o.n",
        "SELECT o.id FROM lnk.appdb..orders o JOIN sys.objects s ON s.name = o.n",
        "SELECT name FROM sys.objects WHERE name IN (SELECT n FROM lnk.appdb.dbo.orders)",
        # Comma-joined row sources and remote reads.
        "SELECT * FROM sys.objects o, GetOrders(1) g",
        "SELECT * FROM sys.objects o, dbo.GetOrders(1) g",
        "SELECT * FROM sys.objects, OPENQUERY(lnk, 'select * from orders') q",
        "SELECT * FROM sys.objects s JOIN OPENROWSET('SQLNCLI', 'x', 'select 1') r ON 1 = 1",
        "SELECT * FROM sys.objects s JOIN OPENDATASOURCE('SQLNCLI', 'x').appdb.dbo.orders o "
        "ON 1 = 1",
        # Writes, temp tables and table variables, and an application sequence.
        "SELECT name INTO dbo.app_names FROM sys.objects",
        "SELECT name INTO #work FROM sys.objects",
        "SELECT name INTO ##shared FROM sys.objects",
        "INSERT INTO #work SELECT name FROM sys.objects",
        "SELECT * FROM #orders o JOIN sys.objects s ON s.name = o.n",
        "SELECT * FROM sys.objects o WHERE EXISTS (SELECT 1 FROM @ids i WHERE i.id = o.object_id)",
        "SELECT NEXT VALUE FOR dbo.order_seq, name FROM sys.objects",
        "DELETE FROM dbo.Orders WHERE OrderId IN (SELECT object_id FROM sys.objects)",
        # An msdb table that is not one of SQL Server's own.
        "SELECT * FROM msdb.dbo.my_app_jobs",
        # A procedure of the application that shares a catalog procedure's name.
        "EXEC dbo.sp_help N'dbo.Orders'",
        # A zero-width character inside a schema name: the text parses as it is.
        "SELECT * FROM s\u200bys.orders",
    ],
)
def test_a_customer_statement_is_kept(text):
    out = _offline([_row("0x01", APP_READ), _row("0x02", INDEXES), _row("0x99", text)])
    assert "0x99" in _query_ids(out)
    assert _excluded(out) == {"0x02": SQLSERVER_CATALOG}


@pytest.mark.passes_on_main  # a negative case: main keeps it too
def test_a_statement_of_an_application_routine_is_kept():
    # The collection links the statement to the application's own procedure: it is part of
    # that routine's code, whatever it reads.
    links = [{"digest": "0x02", "routine_schema": "dbo", "routine_name": "usp_CheckSchema"}]
    out = _offline([_row("0x01", APP_READ), _row("0x02", INDEXES)], query_routines=links)
    assert _query_ids(out) == ["0x01", "0x02"]
    assert _excluded(out) == {}


def test_a_database_named_sys_keeps_its_own_tables():
    kept, excluded = split_migration_tool_statements(
        [_row("0x01", "SELECT OrderId FROM sys.dbo.Orders"), _row("0x02", INDEXES)],
        "sqlserver",
        database_name="sys",
    )
    assert [r["digest"] for r in kept] == ["0x01"]
    assert [e["query_id"] for e in excluded] == ["0x02"]


@pytest.mark.passes_on_main  # a negative case: main keeps it too
@pytest.mark.parametrize("engine", ["postgresql", "mysql", "mariadb", "oracle"])
def test_another_engines_statements_are_never_read_as_sql_server_catalog_reads(engine):
    # A schema named sys is the application's on PostgreSQL; MySQL's and Oracle's own sys
    # schemas are not this rule's to judge.
    rows = [
        _row("0x02", "SELECT name FROM sys.objects WHERE type = 'U'"),
        _row("0x09", "EXEC sp_help 'orders'"),
        _row("0x05", INFO_SCHEMA),
    ]
    assert _split(rows, engine) == (["0x02", "0x09", "0x05"], {})


def test_the_rules_read_the_statements_in_the_t_sql_dialect():
    from src.tools.sql import facade

    engines: list[str] = []
    real = facade.row_sources

    def spy(sql: str, source_engine: str) -> Any:
        engines.append(source_engine)
        return real(sql, source_engine)

    with patch.object(facade, "row_sources", spy):
        _, excluded = _split([_row("0x01", APP_READ), _row("0x03", PARAMETERS)])
    assert excluded == {"0x03": SQLSERVER_CATALOG}
    assert engines and set(engines) == {"sqlserver"}


def test_old_collections_give_todays_result_except_the_excluded_statements():
    app = [_row("0x01", APP_READ, calls=500), _row("0x0B", APP_WRITE, calls=40)]
    with_catalog = _offline([app[0], *CATALOG_ROWS, app[1]])
    without = _offline(app)
    assert with_catalog["queries"]["query_patterns"] == without["queries"]["query_patterns"]
    assert without["queries"]["excluded_statements"] is None
    assert with_catalog["database_schema"] == without["database_schema"]


def test_a_collection_with_dms_and_catalog_statements_records_both():
    heartbeat = "UPDATE awsdms_heartbeat SET hb_timestamp = @p1"
    kept, excluded = _split([_row("0x01", APP_READ), _row("0x0C", heartbeat), *CATALOG_ROWS[:1]])
    assert kept == ["0x01"]
    assert excluded == {"0x0C": "dms_object", "0x02": SQLSERVER_CATALOG}


# --- the live collector ---------------------------------------------------------------


def _live_pattern(qid: str, text: str) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "execution_count": 10,
        "frequency_per_hour": 10.0,
        "tables_accessed": ["app.t"],
    }


def test_a_live_collection_leaves_them_out_too():
    module, cls, entry, _ = ENGINES["sqlserver"]
    stages = {
        "metadata": {"version": "Microsoft SQL Server 2022 (RTM) 16.0", "db_size_gb": 1.0},
        "schema": {"tables": [TABLE], "global_stats": {}},
        "queries": [_live_pattern("1", "SELECT id FROM dbo.t WHERE id = @p1"),
                    _live_pattern("2", INDEXES)],
        # Performance Insights reports SQL Server's own statements too.
        "aws_metrics": {"query_patterns": [_live_pattern("pi", SP_HELP)]},
    }  # fmt: skip
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, MagicMock()),
    ):
        result = getattr(module, entry)(_input("sqlserver", "live"), _Checkpoints(stages))
    assert [p.query_id for p in result.queries.query_patterns] == ["1"]
    excluded = result.queries.excluded_statements or []
    assert [(e.query_id, e.rule) for e in excluded] == [
        ("2", SQLSERVER_CATALOG),
        ("pi", SQLSERVER_SYSTEM_PROCEDURE),
    ]
    # The capture still counts every row the statement query returned.
    capture = result.metadata.query_capture
    assert capture is not None and capture.captured == 2


def test_a_ddl_collection_leaves_them_out_of_performance_insights():
    module = ENGINES["sqlserver"][0]
    aws = {"query_patterns": [_live_pattern("1", APP_READ), _live_pattern("pi", DMV)]}
    stages = {"ddl_schema": [TABLE], "aws_metrics": aws}
    with patch.object(module, "_build_cred_mgr", return_value=MagicMock()):
        result = module._collect_ddl(_input("sqlserver", "ddl"), _Checkpoints(stages))
    assert [p.query_id for p in result.queries.query_patterns] == ["1"]
    assert [e.query_id for e in result.queries.excluded_statements or []] == ["pi"]


# --- where the user reads the results ------------------------------------------------

CATALOG_SUMMARY = (
    "9 statements that read only SQL Server's system catalogs, with 11 executions, were "
    "left out of the workload"
)


def _triage(tmp_path: Path, out: dict) -> dict:
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("Shop/j1894/collector/output.json", out)
    run_triage("j1894", "Shop", store)
    doc = store.read_json("Shop/j1894/referee-triage/triage.json")
    TriageOutputContract.model_validate(doc)
    return doc


def test_triage_counts_and_names_them(tmp_path):
    out = _offline([_row("0x01", APP_READ), *CATALOG_ROWS])
    excluded = _triage(tmp_path, out)["excluded_statements"]
    assert excluded["statement_count"] == 9
    assert excluded["execution_count"] == 11
    assert excluded["summary"] == CATALOG_SUMMARY
    rules = {r["rule"]: r for r in excluded["rules"]}
    assert rules[SQLSERVER_CATALOG]["query_ids"] == [r["digest"] for r in CATALOG_ROWS[:7]]
    assert rules[SQLSERVER_SYSTEM_PROCEDURE]["query_ids"] == ["0x09", "0x0A"]


def test_the_facts_and_the_gate_state_them_once():
    from src.report.printed_summary import render_gate_block
    from src.shared.gate_facts import gate_block_facts

    collector = _offline([_row("0x01", APP_READ), *CATALOG_ROWS])
    statement = CATALOG_SUMMARY + "; referee-triage/triage.json lists them"
    facts = {"statement_count": 9, "execution_count": 11, "statement": statement}
    assert excluded_statement_facts(collector) == facts
    assert gate_block_facts({}, collector=collector)["excluded_statements"] == facts
    block = render_gate_block({}, database_name="Shop", job_id="j1894", collector=collector)
    assert block.count(f"{statement}.") == 1


def test_one_sentence_names_both_origins():
    heartbeat = "UPDATE awsdms_heartbeat SET hb_timestamp = @p1"
    collector = _offline(
        [_row("0x01", APP_READ), _row("0x0C", heartbeat, calls=60), *CATALOG_ROWS[:2]]
    )
    facts = excluded_statement_facts(collector)
    assert facts is not None
    assert facts["statement"] == (
        "1 statement from AWS DMS and 2 that read only SQL Server's system catalogs, with "
        "63 executions, were left out of the workload; referee-triage/triage.json lists them"
    )
    assert excluded_statement_count(collector, origin="dms") == 1
    assert excluded_statement_count(collector, origin="catalog") == 2
    assert excluded_statement_count(collector) == 3


def test_every_deliverable_states_them_once():
    from src.report.renderers import render_engineering_report_md
    from tests.e2e.deliverable_figures import md_plain
    from tests.executive_pdf import executive_text
    from tests.unit.tools.database.test_dms_statements_excluded import _report

    facts = excluded_statement_facts(_offline([_row("0x01", APP_READ), *CATALOG_ROWS]))
    report = _report(facts)
    line = f"Statements left out: {CATALOG_SUMMARY}; referee-triage/triage.json lists them."
    assert md_plain(render_engineering_report_md(report)).count(line) == 1
    assert " ".join(executive_text(report).split()).count(line) == 1


def _collection_file(tmp_path: Path) -> Path:
    path = tmp_path / "shop-collection.json"
    rows = [_row("0x01", APP_READ), *CATALOG_ROWS]
    path.write_text(json.dumps(_collection(rows)), encoding="utf-8")
    return path


def test_the_run_assessment_collect_line_counts_them(tmp_path, monkeypatch, capsys):
    import scripts.run_assessment as run_assessment

    path = _collection_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    run_assessment.phase_collect(str(path), "Shop", store)
    status = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"phase": "collect"' in line
    )
    assert status["catalog_statements_excluded"] == 9
    assert "dms_statements_excluded" not in status
    assert "query_merge" not in status


def test_the_run_collect_line_counts_them(tmp_path):
    import subprocess  # nosec B404 -- runs this repo's own run_collect.py with fixed argv
    import sys

    root = Path(__file__).resolve().parents[4]
    out = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_collect.py", "--file", str(_collection_file(tmp_path)),
         "--db", "Shop", "--job-id", "cat01894", "--artifact-root", str(tmp_path / "artifacts")],
        cwd=root, capture_output=True, text=True, check=True, timeout=300,
    )  # fmt: skip
    status = json.loads(out.stdout.strip().splitlines()[-1])
    assert status["status"] == "complete"
    assert status["catalog_statements_excluded"] == 9
    assert "dms_statements_excluded" not in status


# --- the adversarial corpus -----------------------------------------------------------

# The corpus entries that read an application source beside a catalog view (review of
# #2590): a linked-server or ``..`` name, a comma-joined function or OPENQUERY.
CORPUS_KEPT = (
    "linked_server_name_next_to_catalog",
    "bracketed_linked_server_join_to_catalog",
    "double_dot_name_join_to_catalog",
    "comma_joined_function_beside_catalog",
    "comma_joined_openquery_beside_catalog",
)


def test_the_rule_keeps_the_corpus_entries_beside_a_catalog_view():
    from src.tools.database.system_catalog_statements import catalog_statement_match
    from tests.corpus.adversarial_inputs import SQL_ENTRIES

    by_id = {e.id: e for e in SQL_ENTRIES}
    for entry_id in CORPUS_KEPT:
        entry = by_id[entry_id]
        assert catalog_statement_match({}, entry.text, "sqlserver") is None, entry_id
        assert catalog_statement_match({}, entry.twin, "sqlserver") is None, entry_id


def test_the_rule_answers_each_corpus_entry_as_its_twin():
    from src.tools.database.system_catalog_statements import catalog_statement_match
    from tests.corpus.adversarial_inputs import SQL_ENTRIES

    for entry in SQL_ENTRIES:
        if "sqlserver" in entry.dialects:
            text = catalog_statement_match({}, entry.text, "sqlserver")
            assert text == catalog_statement_match({}, entry.twin, "sqlserver"), entry.id
