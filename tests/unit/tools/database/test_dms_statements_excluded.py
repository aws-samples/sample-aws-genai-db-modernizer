# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A collection taken during an AWS DMS task leaves DMS's own statements out (#2375).

Every statement here is synthetic: written for the test in the shapes DMS and psqlODBC
produce, never copied from a collection.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector.offline_ingest import (
    build_offline_collector_output,
    build_offline_contract,
)
from src.agents.referee.triage_handler import run_triage
from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.triage_output import TriageOutputContract
from src.shared.excluded_statements import excluded_statement_facts
from src.storage.local_store import LocalArtifactStore
from src.tools.database.migration_tool_statements import (
    DMS_CURSOR,
    DMS_OBJECT,
    DMS_REPLICATION_SLOT,
    split_migration_tool_statements,
)
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from tests.unit.agents.test_collector_source_host_live import ENGINES, TABLE, _Checkpoints, _input

ROOT = Path(__file__).resolve().parents[4]

# Role ids, as pg_stat_statements' userid writes them.
APP = "16384"
DMS = "16385"

HEARTBEAT = 'UPDATE "public"."awsdms_heartbeat" SET "hb_timestamp" = $1 WHERE "hb_id" = $2'
SLOT = "select * from pg_logical_slot_get_changes($1, NULL, $2, $3, $4)"
CURSOR_DECLARE = (
    'declare "SQL_CUR0x5a1b2c3d4e50" cursor with hold for select "id", "title" '
    'from "public"."posts"'
)
CURSOR_FETCH = 'fetch 10000 in "SQL_CUR0x5a1b2c3d4e50"'
CURSOR_CLOSE = 'close "SQL_CUR0x5a1b2c3d4e50"'
CATALOG_CURSOR = (
    'declare "SQL_CUR0x5a1b2c3d4f00" cursor with hold for select column_name, data_type '
    "from information_schema.columns where table_schema = $1 and table_name = $2"
)
APP_READ = "SELECT id, title FROM posts WHERE id = $1"
APP_CURSOR = (
    'declare "SQL_CUR0x7f00aa11bb20" cursor with hold for select "id" from "public"."posts"'
)
# The review's probe: an application cursor that joins and filters.
APP_JOIN_CURSOR = (
    'declare "SQL_CUR0x7f00aa11cc30" cursor with hold for select o.id, o.total from orders o '
    "join customers c on c.id = o.cid where c.region = $1"
)
APP_JOIN_FETCH = 'fetch 100 in "SQL_CUR0x7f00aa11cc30"'


def _row(digest: str, text: str, role: str | None = None, calls: int = 10) -> dict[str, Any]:
    row: dict[str, Any] = {
        "digest": digest,
        "query_text": text,
        "schema_name": "app",
        "execution_count": calls,
        "total_time_ms": 1.0,
        "avg_time_ms": 0.1,
    }
    if role is not None:
        row["role_id"] = role
    return row


def _pg_collection(queries: list[dict]) -> dict[str, Any]:
    return {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "app"},
        "tables": [{"table_name": "posts", "schema_name": "public", "row_count": 10}],
        "columns": [
            {
                "table_name": "posts",
                "column_name": c,
                "data_type": "integer" if c == "id" else "text",
                "is_nullable": "NO",
            }
            for c in ("id", "title")
        ],
        "primary_keys": [{"table_name": "posts", "column_name": "id", "ordinal_position": 1}],
        "queries": queries,
    }


def _offline(queries: list[dict]) -> dict[str, Any]:
    out = build_offline_collector_output(
        parse_offline_collection(_pg_collection(queries)),
        job_id="j2375",
        database_name="app",
        s3_key="k",
    )
    CollectorOutputContract.model_validate(out)
    return out


def _query_ids(out: dict) -> list[str]:
    return [q["query_id"] for q in out["queries"]["query_patterns"]]


def _excluded(out: dict) -> dict[str, str]:
    return {e["query_id"]: e["rule"] for e in out["queries"].get("excluded_statements") or []}


def _split(rows: list[dict]) -> tuple[list[str], dict[str, str]]:
    kept, excluded = split_migration_tool_statements(rows, "postgresql")
    return [r["digest"] for r in kept], {e["query_id"]: e["rule"] for e in excluded}


# --- the offline ingest -------------------------------------------------------------


def test_a_collection_during_a_dms_task_keeps_only_the_application():
    out = _offline(
        [
            _row("1", APP_READ, APP, calls=500),
            _row("2", HEARTBEAT, DMS, calls=60),
            _row("3", SLOT, DMS, calls=40),
            _row("4", CURSOR_DECLARE, DMS, calls=1),
            _row("5", CURSOR_FETCH, DMS, calls=30),
            _row("6", CURSOR_CLOSE, DMS, calls=1),
            _row("7", CATALOG_CURSOR, DMS, calls=2),
        ]
    )
    assert _query_ids(out) == ["1"]
    assert _excluded(out) == {
        "2": DMS_OBJECT,
        "3": DMS_REPLICATION_SLOT,
        "4": DMS_CURSOR,
        "5": DMS_CURSOR,
        "6": DMS_CURSOR,
        "7": DMS_CURSOR,
    }
    by_id = {e["query_id"]: e for e in out["queries"]["excluded_statements"]}
    assert by_id["2"]["execution_count"] == 60
    assert "awsdms_heartbeat" in by_id["2"]["reason"]
    assert "pg_logical_slot_get_changes" in by_id["3"]["reason"]


def test_no_excluded_record_names_a_cursor_address():
    # psqlODBC's cursor names hold a memory address, which reads like an account id.
    out = _offline([_row("2", HEARTBEAT, DMS), _row("4", CURSOR_DECLARE, DMS)])
    assert "SQL_CUR" not in json.dumps(out["queries"]["excluded_statements"])


def test_an_application_cursor_is_kept_when_its_role_runs_no_dms_statement():
    out = _offline(
        [_row("2", HEARTBEAT, DMS), _row("7", APP_CURSOR, APP), _row("1", APP_READ, APP)]
    )
    assert _query_ids(out) == ["7", "1"]
    assert _excluded(out) == {"2": DMS_OBJECT}


def test_a_shared_role_keeps_the_applications_cursors_of_another_shape():
    # DMS and the application log in as one role (the RDS master user, say): DMS's
    # full-load cursor goes, the application's join with a WHERE stays with its fetch.
    kept, excluded = _split(
        [
            _row("2", HEARTBEAT, DMS),
            _row("4", CURSOR_DECLARE, DMS),
            _row("5", CURSOR_FETCH, DMS),
            _row("8", APP_JOIN_CURSOR, DMS),
            _row("9", APP_JOIN_FETCH, DMS),
        ]
    )
    assert kept == ["8", "9"]
    assert excluded == {"2": DMS_OBJECT, "4": DMS_CURSOR, "5": DMS_CURSOR}


@pytest.mark.parametrize(
    "body",
    [
        'select "id" from "public"."posts" where "id" > $1',
        'select p."id" from "public"."posts" p, "public"."users" u',
        'select count(*) from "public"."posts" group by "title"',
        'select "id" from "public"."posts" union select "id" from "public"."users"',
        'select "id" from "public"."posts" limit $1',
        # The review's round 2 probes: none is a whole-table read.
        'select "id" from "public"."posts" order by "id" offset $1',
        'select "id" from "public"."posts" order by "created_at" desc fetch first 10 rows only',
        'select "id" from "public"."jobs" for update skip locked',
        'select distinct "customer_id" from "public"."orders"',
        'select "id", row_number() over (order by "id") from "public"."posts"',
        'select "id" from "public"."posts" tablesample system (10)',
        'select "id" from "public"."orders", lateral unnest("tags") t',
        'select "id" from "public"."orders" o, (values (1)) v(x)',
        'select * from "public"."posts"',
        'select "id" from "posts"',
        # A table named pg_ is the application's, not a system catalog.
        "select o.id from pg_orders o join pg_customers c on c.id = o.cid where c.region = $1",
    ],
)
def test_a_cursor_not_of_the_full_load_shape_is_kept(body):
    declare = f'declare "SQL_CUR0x5a1b2c3d4e99" cursor with hold for {body}'
    kept, excluded = _split([_row("2", HEARTBEAT, DMS), _row("8", declare, DMS)])
    assert kept == ["8"]
    assert excluded == {"2": DMS_OBJECT}


def test_a_role_that_runs_only_dms_statements_loses_a_fetch_whose_declare_is_missing():
    # The DECLARE can fall below the collection's floor; the fetch is DMS's all the same.
    kept, excluded = _split([_row("2", HEARTBEAT, DMS), _row("5", CURSOR_FETCH, DMS)])
    assert kept == []
    assert excluded == {"2": DMS_OBJECT, "5": DMS_CURSOR}
    shared, _ = _split(
        [_row("2", HEARTBEAT, DMS), _row("5", CURSOR_FETCH, DMS), _row("1", APP_READ, DMS)]
    )
    assert shared == ["5", "1"]


def test_cursors_and_slot_reads_are_kept_when_the_collection_records_no_role():
    # A collection written before #2375 has no role ids: only DMS's own tables can be
    # told apart, so every cursor statement and slot read stays.
    out = _offline(
        [
            _row("2", HEARTBEAT),
            _row("3", SLOT),
            _row("4", CURSOR_DECLARE),
            _row("5", CURSOR_FETCH),
        ]
    )
    assert _query_ids(out) == ["3", "4", "5"]
    assert _excluded(out) == {"2": DMS_OBJECT}


def test_an_applications_own_slot_poller_is_kept_and_marks_no_role():
    # A wal2json poller of the application's own: no heartbeat from its role, so the slot
    # read stays and the role's cursors with it.
    poller = "SELECT data FROM pg_logical_slot_get_changes($1, NULL, NULL, $2, $3)"
    kept, excluded = _split(
        [_row("2", HEARTBEAT, DMS), _row("3", poller, APP), _row("7", APP_CURSOR, APP)]
    )
    assert kept == ["3", "7"]
    assert excluded == {"2": DMS_OBJECT}
    alone, none = _split([_row("3", poller, APP), _row("7", APP_CURSOR, APP)])
    assert (alone, none) == (["3", "7"], {})


def test_a_heartbeat_read_is_kept_and_marks_no_role():
    # The review's lag-monitor probe: an application role reads the heartbeat, and runs its
    # own wal2json poller and a cursor over one table. All of it is the application's.
    monitor = [
        _row("10", "SELECT hb_timestamp FROM awsdms_heartbeat", APP),
        _row("11", "SELECT * FROM orders WHERE id = $1", APP),
        _row("12", "SELECT data FROM pg_logical_slot_get_changes($1, NULL, NULL)", APP),
        _row("13", 'declare "SQL_CUR0x7f00aa11dd40" cursor with hold for select "k", "v" '
             'from "public"."settings"', APP),
        _row("14", 'fetch 100 in "SQL_CUR0x7f00aa11dd40"', APP),
    ]  # fmt: skip
    kept, excluded = _split(monitor)
    assert kept == ["10", "11", "12", "13", "14"]
    assert excluded == {}
    # Next to DMS's own heartbeat update, the monitor's role is still not DMS's.
    kept, excluded = _split([_row("2", HEARTBEAT, DMS), *monitor])
    assert kept == ["10", "11", "12", "13", "14"]
    assert excluded == {"2": DMS_OBJECT}


def test_an_insert_into_the_heartbeat_marks_the_role_too():
    insert = 'INSERT INTO "public"."awsdms_heartbeat" ("hb_timestamp") VALUES ($1)'
    kept, excluded = _split([_row("2", insert, DMS), _row("3", SLOT, DMS)])
    assert (kept, excluded) == ([], {"2": DMS_OBJECT, "3": DMS_REPLICATION_SLOT})


def test_the_ddl_capture_role_is_not_taken_for_the_dms_role():
    # DMS's DDL capture inserts run as the role that issued the DDL, maybe the application's.
    ddl = "INSERT INTO public.awsdms_ddl_audit (c_time, c_user, c_ddlqry) VALUES ($1, $2, $3)"
    out = _offline([_row("8", ddl, APP), _row("7", APP_CURSOR, APP)])
    assert _query_ids(out) == ["7"]
    assert _excluded(out) == {"8": DMS_OBJECT}


@pytest.mark.parametrize(
    "text",
    [
        # Customer tables whose names only resemble DMS's.
        "UPDATE heartbeat SET hb_timestamp = $1 WHERE hb_id = $2",
        "SELECT * FROM awsdms_heartbeats_archive WHERE id = $1",
        # A read of DMS's heartbeat is an application's lag monitor.
        "SELECT hb_timestamp FROM awsdms_heartbeat",
        "SELECT * FROM my_awsdms_status",
        # DMS's control tables live on the target: on a source they are the application's.
        "SELECT * FROM awsdms_status WHERE id = $1",
        "UPDATE awsdms_history SET note = $1 WHERE id = $2",
        "SELECT * FROM awsdms_control.my_app_table",
        "SELECT * FROM awsdms_control.awsdms_apply_exceptions",
        # A DMS table joined to an application table is the application's statement.
        "SELECT p.id FROM posts p JOIN awsdms_heartbeat h ON h.hb_id = p.id",
        # A slot function name in a comment or a string, or with a table read.
        "/* pg_logical_slot_get_changes( */ SELECT id FROM posts",
        "SELECT 'pg_logical_slot_get_changes(' AS note",
        "SELECT p.id FROM posts p, pg_logical_slot_peek_changes($1, NULL, $2) c",
        # A cursor with another name, or a statement that only mentions a cursor name.
        'declare "C1" cursor for select id from posts',
        "SELECT 'fetch 10 in \"SQL_CUR0x5a1b2c3d4e50\"' AS note",
    ],
)
def test_a_customer_statement_that_only_resembles_dms_is_kept(text):
    out = _offline([_row("2", HEARTBEAT, DMS), _row("9", text, DMS)])
    assert "9" in _query_ids(out)
    assert "9" not in _excluded(out)


def test_a_collection_without_dms_statements_gives_todays_result():
    queries = [_row("1", APP_READ, APP), _row("7", APP_CURSOR, APP)]
    out = _offline(queries)
    assert out["queries"]["excluded_statements"] is None
    without_roles = _offline([{k: v for k, v in q.items() if k != "role_id"} for q in queries])
    assert without_roles["queries"] == out["queries"]
    assert "role_id" not in json.dumps(out)


def test_rows_of_one_statement_from_two_roles_keep_the_applications():
    # pg_stat_statements keys an entry on its role too: DMS's row goes, the app's stays,
    # and the left-out record's id is not the id the workload keeps.
    out = _offline(
        [
            _row("2", HEARTBEAT, DMS),
            _row("4", CURSOR_DECLARE, DMS),
            _row("4", CURSOR_DECLARE, APP, calls=3),
        ]
    )
    patterns = out["queries"]["query_patterns"]
    assert [(p["query_id"], p["frequency_per_hour"] > 0) for p in patterns] == [("4", True)]
    assert _excluded(out) == {"2": DMS_OBJECT, "4:dms": DMS_CURSOR}
    record = next(e for e in out["queries"]["excluded_statements"] if e["query_id"] == "4:dms")
    assert record["reason"].endswith("stay in the workload under id 4")


def test_the_mysql_family_leaves_out_dms_source_tables_only():
    rows = [
        _row("a", "UPDATE `awsdms_heartbeat` SET `hb_timestamp` = ?"),
        _row("b", "SELECT `id` , `title` FROM `app` . `posts`"),
        _row("c", "SELECT * FROM pg_logical_slot_get_changes ( ? )"),
        _row("d", "SELECT * FROM `awsdms_control` . `awsdms_status`"),
    ]
    kept, excluded = split_migration_tool_statements(rows, "mysql")
    assert [r["digest"] for r in kept] == ["b", "c", "d"]
    assert [(e["query_id"], e["rule"]) for e in excluded] == [("a", DMS_OBJECT)]


# --- the collection script and the live collector ------------------------------------


def test_the_postgresql_script_records_each_statements_role_id_not_its_name():
    script = (ROOT / "scripts" / "collect-postgresql.sql").read_text()
    match = re.search(r"queryid::text AS digest,(.*?)FROM pg_stat_statements s", script, re.S)
    assert match, "the statements block of collect-postgresql.sql"
    assert "s.userid::text AS role_id" in match.group(1)
    assert "pg_get_userbyid" not in match.group(1)


def test_the_live_tools_read_each_statements_role_id():
    db = PostgreSQLRemoteCollector.__new__(PostgreSQLRemoteCollector)

    def query(sql: str) -> list[dict]:
        if "AS role_id" in sql:
            return [{"queryid": 7, "query": HEARTBEAT, "calls": 5, "role_id": DMS}]
        return []

    db._query = query  # type: ignore[method-assign]
    db.stats_window = lambda: None  # type: ignore[method-assign]
    rows = db.collect_query_patterns(min_executions=1, limit=None, rate_sample_seconds=0)
    assert [r.get("role_id") for r in rows] == [DMS]


def _live_pattern(qid: str, text: str, role: str | None = None) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "execution_count": 10,
        "frequency_per_hour": 10.0,
        "tables_accessed": ["app.posts"],
        **({"role_id": role} if role else {}),
    }


HEARTBEAT_ANY = "UPDATE awsdms_heartbeat SET hb_timestamp = ?"


@pytest.mark.parametrize(
    ("engine", "rows", "expected"),
    [
        ("postgresql", [("2", HEARTBEAT, DMS), ("4", CURSOR_DECLARE, DMS)],
         [("2", DMS_OBJECT), ("4", DMS_CURSOR), ("pi", DMS_OBJECT)]),
        ("mysql", [("2", HEARTBEAT_ANY, None)], [("2", DMS_OBJECT), ("pi", DMS_OBJECT)]),
        ("mariadb", [("2", HEARTBEAT_ANY, None)], [("2", DMS_OBJECT), ("pi", DMS_OBJECT)]),
    ],
)  # fmt: skip
def test_a_live_collection_leaves_dms_statements_out_too(engine, rows, expected):
    module, cls, entry, _ = ENGINES[engine]
    stages = {
        "metadata": {"version": "16.4", "db_size_gb": 1.0},
        "schema": {"tables": [TABLE], "global_stats": {}},
        "queries": [_live_pattern("1", APP_READ, APP), *(_live_pattern(*r) for r in rows)],
        # Performance Insights reports DMS's statements too.
        "aws_metrics": {"query_patterns": [_live_pattern("pi", HEARTBEAT_ANY)]},
    }
    with (
        patch.object(module, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(module, "SSMExecutor", MagicMock()),
        patch.object(module, cls, MagicMock()),
    ):
        result = getattr(module, entry)(_input(engine, "live"), _Checkpoints(stages))
    assert [p.query_id for p in result.queries.query_patterns] == ["1"]
    excluded = result.queries.excluded_statements or []
    assert [(e.query_id, e.rule) for e in excluded] == expected
    # The capture still counts every row the statement query returned.
    capture = result.metadata.query_capture
    assert capture is not None and capture.captured == 1 + len(rows)
    # The role id is read by the rules only, never written to the collector output.
    assert "role_id" not in result.model_dump_json()


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_a_ddl_collection_leaves_dms_statements_from_performance_insights_out(engine):
    module = ENGINES[engine][0]
    aws = {
        "query_patterns": [_live_pattern("1", APP_READ, APP), _live_pattern("pi", HEARTBEAT_ANY)]
    }
    stages = {"ddl_schema": [TABLE], "aws_metrics": aws}
    with patch.object(module, "_build_cred_mgr", return_value=MagicMock()):
        result = module._collect_ddl(_input(engine, "ddl"), _Checkpoints(stages))
    assert [p.query_id for p in result.queries.query_patterns] == ["1"]
    assert [e.query_id for e in result.queries.excluded_statements or []] == ["pi"]
    assert "role_id" not in result.model_dump_json()


def test_an_offline_collection_with_aws_metrics_records_both_sources_once():
    parsed = parse_offline_collection(
        _pg_collection([_row("1", APP_READ, APP), _row("2", HEARTBEAT, DMS)])
    )
    inp = CollectorInput.model_validate(
        {
            "job_id": "j2375",
            "engine": "postgresql",
            "cluster_endpoint": "offline",
            "port": 5432,
            "database_name": "app",
            "mode": "offline",
            "offline_config": {"s3_bucket": "b", "s3_key": "k"},
        }
    )
    aws = {"query_patterns": [_live_pattern("2", HEARTBEAT), _live_pattern("pi", HEARTBEAT_ANY)]}
    out = build_offline_contract(parsed, inp, aws_raw=aws)
    assert [p.query_id for p in out.queries.query_patterns] == ["1"]
    assert [e.query_id for e in out.queries.excluded_statements or []] == ["2", "pi"]
    assert "role_id" not in out.model_dump_json()


# --- triage --------------------------------------------------------------------------


def _triage(tmp_path: Path, out: dict) -> dict:
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("app/j2375/collector/output.json", out)
    run_triage("j2375", "app", store)
    doc = store.read_json("app/j2375/referee-triage/triage.json")
    TriageOutputContract.model_validate(doc)
    return doc


DURING_DMS = [
    _row("1", APP_READ, APP),
    _row("2", HEARTBEAT, DMS, calls=60),
    _row("4", CURSOR_DECLARE, DMS, calls=1),
    _row("5", CURSOR_FETCH, DMS, calls=30),
]
SUMMARY = "3 statements from AWS DMS, with 91 executions, were left out of the workload"


def test_triage_counts_and_names_the_excluded_statements(tmp_path):
    excluded = _triage(tmp_path, _offline(DURING_DMS))["excluded_statements"]
    assert excluded["statement_count"] == 3
    assert excluded["execution_count"] == 91
    assert excluded["summary"] == SUMMARY
    rules = {r["rule"]: r for r in excluded["rules"]}
    assert rules[DMS_OBJECT]["query_ids"] == ["2"]
    assert rules[DMS_CURSOR]["query_ids"] == ["4", "5"]
    assert rules[DMS_CURSOR]["statement_count"] == 2
    assert all(r["reasons"] for r in excluded["rules"])


def test_triage_records_none_when_nothing_was_left_out(tmp_path):
    doc = _triage(tmp_path, _offline([_row("1", APP_READ, APP)]))
    assert doc["excluded_statements"] is None


def test_a_triage_written_before_loads_with_none():
    old = {
        "contract_version": "1.4",
        "job_id": "j",
        "database_name": "db",
        "selected_agents": [],
        "skipped_agents": [],
        "baseline": {},
        "deferred_agents": [],
        "signals": [],
        "query_capabilities": {},
        "confidence_score": 50,
        "timestamp": "2026-11-01T00:00:00Z",
    }
    assert TriageOutputContract.model_validate(old).excluded_statements is None


def test_a_collector_output_written_before_loads_with_none():
    out = _offline([_row("1", APP_READ, APP)])
    assert CollectorOutputContract.model_validate(out).queries.excluded_statements is None


# --- where the user reads the results ------------------------------------------------

STATEMENT = SUMMARY + "; referee-triage/triage.json lists them"


def test_the_facts_state_the_left_out_statements_once():
    facts = excluded_statement_facts(_offline(DURING_DMS))
    assert facts == {"statement_count": 3, "execution_count": 91, "statement": STATEMENT}
    assert excluded_statement_facts(_offline([_row("1", APP_READ, APP)])) is None
    assert excluded_statement_facts(None) is None


def _collection_file(tmp_path: Path) -> Path:
    path = tmp_path / "app-collection.json"
    path.write_text(json.dumps(_pg_collection(DURING_DMS)), encoding="utf-8")
    return path


def test_the_run_assessment_collect_line_counts_them(tmp_path, monkeypatch, capsys):
    import scripts.run_assessment as run_assessment

    path = _collection_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    run_assessment.phase_collect(str(path), "app", store)
    status = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"phase": "collect"' in line
    )
    assert status["dms_statements_excluded"] == 3
    # The rows left out are not literal variants merged into the kept shapes.
    assert "query_merge" not in status


def test_the_run_collect_line_counts_them(tmp_path):
    import subprocess  # nosec B404 -- runs this repo's own run_collect.py with fixed argv
    import sys

    out = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_collect.py", "--file", str(_collection_file(tmp_path)),
         "--db", "app", "--job-id", "dms00001", "--artifact-root", str(tmp_path / "artifacts")],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=300,
    )  # fmt: skip
    status = json.loads(out.stdout.strip().splitlines()[-1])
    assert status["status"] == "complete"
    assert status["dms_statements_excluded"] == 3


def test_a_clean_collect_line_has_no_count(tmp_path, monkeypatch, capsys):
    import scripts.run_assessment as run_assessment

    path = tmp_path / "app-collection.json"
    path.write_text(json.dumps(_pg_collection([_row("1", APP_READ, APP)])), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run_assessment.phase_collect(
        str(path), "app", LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    )
    assert "dms_statements_excluded" not in capsys.readouterr().out


def test_the_gate_facts_and_the_chat_gate_state_them():
    from src.report.printed_summary import render_gate_block
    from src.shared.gate_facts import gate_block_facts

    collector = _offline(DURING_DMS)
    facts = gate_block_facts({}, collector=collector)["excluded_statements"]
    assert facts == {"statement_count": 3, "execution_count": 91, "statement": STATEMENT}
    assert (
        gate_block_facts({}, collector=_offline([_row("1", APP_READ, APP)]))["excluded_statements"]
        is None
    )
    block = render_gate_block({}, database_name="app", job_id="j2375", collector=collector)
    assert block.count(f"{STATEMENT}.") == 1
    assert STATEMENT not in render_gate_block({}, database_name="app", job_id="j2375")


def test_the_assignments_api_returns_them_in_the_gate_facts(tmp_path, monkeypatch):
    from src.api.routes import assignments
    from tests.unit.api.test_assignment_cache_reads import DB, JOB, _store, _url, client

    _store(tmp_path, monkeypatch, cached=False)
    store = assignments.artifact_store
    store.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {"before_distribution": {"dynamodb": 3}, "after_distribution": {"dynamodb": 3}},
    )
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    collector["queries"]["excluded_statements"] = _offline(DURING_DMS)["queries"][
        "excluded_statements"
    ]
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    facts = client.get(_url()).json()["gate_facts"]
    assert facts["excluded_statements"]["statement"] == STATEMENT


def _report(excluded: dict | None) -> dict[str, Any]:
    from src.shared.report_facts import compute_report_facts

    report: dict[str, Any] = {
        "database_name": "app",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": [{"target": "aurora_postgresql", "workload_percent": 100.0}],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 300.0,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_postgresql", "monthly_cost_usd": 300.0}],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    report["facts"] = compute_report_facts(report, excluded_statements=excluded)
    return report


def test_every_deliverable_states_them_once():
    from src.contracts.synthesis_output import ReportFacts
    from src.report.renderers import render_engineering_report_md
    from tests.e2e.deliverable_figures import md_plain
    from tests.executive_pdf import executive_text

    report = _report(excluded_statement_facts(_offline(DURING_DMS)))
    facts = ReportFacts.model_validate(report["facts"]).excluded_statements
    assert facts is not None and facts.statement_count == 3
    line = f"Statements left out: {STATEMENT}."
    assert md_plain(render_engineering_report_md(report)).count(line) == 1
    assert " ".join(executive_text(report).split()).count(line) == 1


def test_a_report_with_nothing_left_out_says_nothing():
    from src.contracts.synthesis_output import ReportFacts
    from src.report.renderers import render_engineering_report_md

    report = _report(None)
    assert report["facts"]["excluded_statements"] is None
    assert "Statements left out:" not in render_engineering_report_md(report)
    assert ReportFacts.model_validate(report["facts"]).excluded_statements is None
