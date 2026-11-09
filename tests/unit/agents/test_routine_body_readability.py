# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine body opt-out and per-routine readability on every engine (#1933).

Each collection script has an ``include_routine_bodies`` switch at its top, and each
routine whose body the collecting user cannot read is recorded as ``not_readable``
with the reason, never failing the collection. A collection records the privileges
that decide what it can see; one written before records none and builds as before."""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.routine_sources import mark_body_readability, routine_capture
from src.tools.database.offline_parser import parse_offline_collection
from tests.unit.agents.routine_collections import SEED

REPO = Path(__file__).resolve().parents[3]
SQLSERVER = (REPO / "scripts/collect-sqlserver.sql").read_text()
ORACLE = (REPO / "scripts/collect-oracle.sql").read_text()
MYSQL = (REPO / "scripts/collect-mysql.sql").read_text()
LIVE_SQLSERVER = (REPO / "src/tools/database/sqlserver_tools.py").read_text()
LIVE_ORACLE = (REPO / "src/tools/database/oracle_tools.py").read_text()
LIVE_MYSQL = (REPO / "src/tools/database/mysql_tools.py").read_text()


def _output(raw: dict) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j", database_name="shop", s3_key="k"
    )


def _sqlserver(include: bool = True, capture: bool = True) -> dict:
    def row(name: str, definition: str | None, encrypted: int) -> dict:
        return {"routine_name": name, "schema_name": "dbo", "routine_type": "PROCEDURE",
                "language": "TSQL", "is_encrypted": encrypted, "body_chars": 40,
                "definition": definition if include else None,
                **({} if include else {"body_status": "opted_out"})}  # fmt: skip

    raw: dict[str, Any] = {
        "metadata": {"database_name": "shop", "engine": "sqlserver"},
        "tables": [{"table_name": "orders", "schema_name": "dbo", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [
            row("usp_open", f"CREATE PROCEDURE dbo.usp_open AS SELECT '{SEED}'", 0),
            row("usp_locked", None, 1),
            row("usp_hidden", None, 0),
        ],
        "triggers": [],
    }  # fmt: skip
    if capture:
        raw["metadata"]["routine_capture"] = {
            "include_bodies": include,
            "privileges_not_seen": ["VIEW_DEFINITION"],
        }
    return raw


def _oracle(include: bool = True) -> dict:
    status = {} if include else {"body_status": "opted_out"}
    return {
        "metadata": {"database_name": "app", "engine": "oracle", "version": "19.0.0",
                     "routine_capture": {"include_bodies": include, "privileges_not_seen": []}},
        "tables": [{"table_name": "orders", "schema_name": "app", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "NUMBER",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [
            {"routine_name": "place_order", "schema_name": "app", "routine_type": "PROCEDURE",
             "body_chars": 30, **status},
            {"routine_name": "gone", "schema_name": "app", "routine_type": "PROCEDURE", **status},
        ],
        "triggers": [],
        "routine_sources": [] if not include else [
            {"owner": "app", "name": "place_order", "type": "PROCEDURE", "line": 1,
             "text": "PROCEDURE place_order IS BEGIN NULL; END;\n"}],
    }  # fmt: skip


def _by_name(output: dict) -> dict[str, dict]:
    return {p["procedure_name"]: p for p in output["database_schema"]["procedures"]}


# -- per-routine readability --------------------------------------------------------


def test_a_sqlserver_body_the_user_cannot_read_is_not_readable_with_its_reason():
    procs = _by_name(_output(_sqlserver()))
    assert procs["usp_open"]["body_status"] == "collected"
    assert (procs["usp_locked"]["body_status"], procs["usp_hidden"]["body_status"]) == (
        "not_readable", "not_readable",
    )  # fmt: skip
    assert "WITH ENCRYPTION" in procs["usp_locked"]["body_status_reason"]
    assert "VIEW DEFINITION" in procs["usp_hidden"]["body_status_reason"]
    assert procs["usp_hidden"]["body_chars"] == 40


def test_an_oracle_unit_with_no_source_lines_is_not_readable():
    procs = _by_name(_output(_oracle()))
    assert procs["place_order"]["body_status"] == "collected"
    assert procs["gone"]["body_status"] == "not_readable"
    assert "ALL_SOURCE" in procs["gone"]["body_status_reason"]


def _edited_after_opt_out(raw: dict) -> dict:
    """A file whose capture says include_bodies false but that still holds bodies (an
    edited script): a SQL Server definition, or Oracle source lines."""
    raw = copy.deepcopy(raw)
    for row in raw["procedures"]:
        row.pop("body_status", None)
        if raw["metadata"]["engine"] == "sqlserver":
            row["definition"] = f"CREATE PROCEDURE dbo.{row['routine_name']} AS SELECT '{SEED}'"
    if raw["metadata"]["engine"] == "oracle":
        raw["routine_sources"] = [
            {"owner": "app", "name": "place_order", "type": "PROCEDURE", "line": 1,
             "text": f"PROCEDURE place_order IS BEGIN log('{SEED}'); END;\n"}]  # fmt: skip
    return raw


@pytest.mark.parametrize(
    "raw", [_sqlserver(include=False), _oracle(include=False)], ids=["sqlserver", "oracle"]
)
def test_the_opt_out_drops_every_body_a_file_still_holds_and_keeps_sizes(raw, tmp_path, caplog):
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    edited = _edited_after_opt_out(raw)
    with caplog.at_level(logging.WARNING):
        procs = _by_name(_output(edited))
    assert {p["body_status"] for p in procs.values()} == {"opted_out"}
    assert all(p["definition"] is None for p in procs.values())
    assert {p["body_chars"] for p in procs.values()} - {None}  # sizes kept
    assert "include_routine_bodies = 0 but holds" in caplog.text
    # and no file of the job holds one
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(edited))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), "shop", LocalArtifactStore(base_dir=str(root)))
    assert not list(root.rglob("routine_sources*"))
    assert not [f for f in root.rglob("*") if f.is_file() and SEED in f.read_text(errors="ignore")]


def test_an_opted_out_file_writes_no_routine_sources_even_when_it_holds_creates():
    from src.agents.collector.routine_sources import iter_collection_sources
    from tests.unit.agents.routine_collections import pg_collection

    raw = pg_collection()  # every row collected, with its CREATE
    raw["metadata"]["routine_capture"]["include_bodies"] = False
    assert list(iter_collection_sources(raw)) == []


@pytest.mark.passes_on_main
def test_a_collection_that_records_no_capture_builds_as_before():
    procs = _by_name(_output(_sqlserver(capture=False)))
    assert {p.get("body_status") for p in procs.values()} == {None}


def test_a_wrapped_oracle_unit_is_not_readable_and_keeps_no_text():
    raw = _oracle()
    raw["routine_sources"][0]["text"] = "PROCEDURE place_order wrapped \na000000\n1f\nabcd\n"
    p = _by_name(_output(raw))["place_order"]
    assert (p["body_status"], p["definition"]) == ("not_readable", None)
    assert "wrapped" in p["body_status_reason"]


def test_marking_never_overrides_a_status_and_never_fails():
    rows = [{"routine_name": "a", "body_status": "opted_out"}, {"routine_name": "b"},
            "not a row", {"routine_name": "c", "definition": "x"}]  # fmt: skip
    mark_body_readability("sqlserver", rows, [{"trigger_name": "t", "definition": None}])
    assert [r["body_status"] for r in rows if isinstance(r, dict)] == [
        "opted_out", "not_readable", "collected",
    ]  # fmt: skip
    mark_body_readability("postgresql", [{"routine_name": "p"}], [])  # the script says


def test_what_the_user_may_not_see_is_named_once_at_ingest(caplog):
    with caplog.at_level(logging.WARNING):
        capture = routine_capture(
            {"include_bodies": True, "privileges_not_seen": ["TRIGGER", "SHOW_ROUTINE"]},
            listed=3,
        )
    assert capture is not None and capture["privileges_not_seen"] == ["TRIGGER", "SHOW_ROUTINE"]
    (message,) = [r.getMessage() for r in caplog.records if "may not see" in r.getMessage()]
    assert "triggers" in message and "SHOW_ROUTINE" in message


# -- the switch at the top of each script, and the live reads -----------------------


def _header(script: str) -> str:
    """The comment block at the top of a script: its title banner and the header under
    it, up to the closing banner line."""
    return "".join(script.split("-- =====", 4)[1:3])


def test_the_sqlserver_script_has_the_switch_at_its_top_and_reads_readability():
    head = SQLSERVER.split("DECLARE @metadata", 1)[0]
    assert "DECLARE @include_routine_bodies BIT = 1;" in head
    assert "@include_routine_bodies" in _header(SQLSERVER)  # documented
    for section in ("9. Procedures", "10. Triggers"):
        body = SQLSERVER.split(f"-- ===================== {section}")[1].split("-- =====")[0]
        assert "WHEN @include_routine_bodies = 1 THEN OBJECT_DEFINITION(" in body, section
        assert "OBJECTPROPERTY(" in body and "'IsEncrypted') AS is_encrypted" in body, section
        assert "DATALENGTH(OBJECT_DEFINITION(" in body, section
        assert "'opted_out'" in body, section
    assert ") AS routine_capture" in SQLSERVER
    assert "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')" in SQLSERVER


def test_the_live_sqlserver_collector_reads_the_same():
    for text in ("'IsEncrypted') AS is_encrypted", "DATALENGTH(OBJECT_DEFINITION(",
                 "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')"):  # fmt: skip
        assert text in LIVE_SQLSERVER, text


def test_the_oracle_script_has_the_switch_at_its_top():
    declare = ORACLE.split("DECLARE\n", 1)[1].split("BEGIN\n", 1)[0]
    assert re.search(r"v_include_routine_bodies\s+CONSTANT\s+PLS_INTEGER\s*:=\s*1;", declare)
    assert "v_include_routine_bodies" in _header(ORACLE)  # documented
    sources = ORACLE.split("10b. Routine and trigger bodies")[1].split("-- =====")[1]
    assert "v_include_routine_bodies = 1" in sources
    assert "'routine_capture' VALUE JSON_OBJECT(" in ORACLE
    # One grouped read of ALL_SOURCE for every unit's size, not a read per unit.
    for text in (ORACLE, LIVE_ORACLE):
        assert "SUM(LENGTH(TEXT)) AS SRC_CHARS" in text and "GROUP BY NAME" in text
        assert "WHERE s.OWNER = o.OWNER" not in text


_ACCOUNT = "'app'@'%'"


def _not_seen(mariadb: bool, grants: list[tuple[str, ...]]) -> list[str]:
    """The privilege query the live MySQL collector and the script run, on SQLite with
    information_schema's three privilege tables holding ``grants``: ``("global", priv)``,
    ``("schema", schema, priv)`` or ``("table", schema, table, priv)``."""
    import sqlite3

    from src.tools.database.mysql_tools import privileges_not_seen_sql

    db = sqlite3.connect(":memory:")
    db.execute("ATTACH ':memory:' AS information_schema")
    db.execute("CREATE TABLE information_schema.user_privileges (grantee, privilege_type)")
    db.execute(
        "CREATE TABLE information_schema.schema_privileges (grantee, table_schema, privilege_type)"
    )
    db.execute(
        "CREATE TABLE information_schema.table_privileges "
        "(grantee, table_schema, table_name, privilege_type)"
    )
    inserts = {
        "global": "INSERT INTO information_schema.user_privileges VALUES (?, ?)",
        "schema": "INSERT INTO information_schema.schema_privileges VALUES (?, ?, ?)",
        "table": "INSERT INTO information_schema.table_privileges VALUES (?, ?, ?, ?)",
    }
    for grant in grants:
        db.execute(inserts[grant[0]], (_ACCOUNT, *grant[1:]))
    sql = privileges_not_seen_sql("?", "'shop'", "1" if mariadb else "0")
    rows = db.execute(sql, [_ACCOUNT] * sql.count("?")).fetchall()
    return sorted(r[0] for r in rows)


_ALL = ["EVENT", "SHOW_ROUTINE", "TRIGGER"]


@pytest.mark.parametrize(
    ("mariadb", "grants", "missing"),
    [
        # MySQL 8.0.0-8.0.19: only a global SELECT reads another definer's bodies.
        (False, [], _ALL),
        (False, [("global", "SELECT")], ["EVENT", "TRIGGER"]),
        # MySQL 8.0.20+: SHOW_ROUTINE.
        (False, [("global", "SHOW_ROUTINE")], ["EVENT", "TRIGGER"]),
        # MySQL 8.0 has no mysql.proc: SELECT on the mysql schema or table reads none.
        (False, [("schema", "mysql", "SELECT")], _ALL),
        (False, [("table", "mysql", "proc", "SELECT")], _ALL),
        (False, [("global", "SHOW CREATE ROUTINE")], _ALL),
        (False, [("schema", "shop", "TRIGGER"), ("schema", "shop", "EVENT")], ["SHOW_ROUTINE"]),
        # MariaDB: SELECT on mysql.* or on mysql.proc, or (11.3+) SHOW CREATE ROUTINE.
        (True, [], _ALL),
        (True, [("global", "SELECT")], ["EVENT", "TRIGGER"]),
        (True, [("schema", "mysql", "SELECT")], ["EVENT", "TRIGGER"]),
        (True, [("table", "mysql", "proc", "SELECT")], ["EVENT", "TRIGGER"]),
        (True, [("global", "SHOW CREATE ROUTINE")], ["EVENT", "TRIGGER"]),
        (True, [("schema", "shop", "SHOW CREATE ROUTINE")], ["EVENT", "TRIGGER"]),
        (True, [("schema", "other", "SHOW CREATE ROUTINE")], _ALL),
    ],
)
def test_what_reads_routine_bodies_depends_on_the_server(mariadb, grants, missing):
    assert _not_seen(mariadb, grants) == missing


def test_the_script_runs_the_live_privilege_query():
    from src.tools.database.mysql_tools import _PRIVILEGES_NOT_SEEN_SQL, privileges_not_seen_sql

    def flat(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip()

    script = flat(privileges_not_seen_sql("@grantee", "@db", "@is_mariadb")).replace(
        "SELECT p.privilege FROM (",
        "SELECT JSON_ARRAYAGG(p.privilege) INTO @privileges_not_seen FROM (",
        1,
    )
    assert script in flat(MYSQL)
    assert "SET @is_mariadb = VERSION() LIKE '%MariaDB%';" in MYSQL
    assert "(VERSION() LIKE '%MariaDB%')" in _PRIVILEGES_NOT_SEEN_SQL


# -- the live SQL Server and Oracle collectors ---------------------------------------


class _LiveDb:
    """A live collector's reads, answered from canned rows (an Exception raises)."""

    def __init__(self, **rows):
        self.rows = rows

    def __getattr__(self, name: str):
        if not name.startswith("collect_"):
            raise AttributeError(name)

        def read(*args, **kwargs):
            value = self.rows.get(name, [])
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)

        return read


def test_the_live_sqlserver_collector_marks_each_body_and_reads_the_capture():
    from src.agents.collector import sqlserver_collector

    db = _LiveDb(
        collect_tables=[],
        collect_procedures=_sqlserver()["procedures"],
        collect_routine_capture={
            "include_bodies": True,
            "privileges_not_seen": ["VIEW_DEFINITION"],
        },
    )
    raw = sqlserver_collector._collect_schema_raw(db)
    statuses = {p["routine_name"]: (p["body_status"], p.get("body_status_reason") or "")
                for p in raw["procedures"]}  # fmt: skip
    assert statuses["usp_open"][0] == "collected"
    assert (
        statuses["usp_locked"][0] == "not_readable"
        and "WITH ENCRYPTION" in statuses["usp_locked"][1]
    )
    assert (
        statuses["usp_hidden"][0] == "not_readable"
        and "VIEW DEFINITION" in statuses["usp_hidden"][1]
    )
    assert raw["routine_capture"]["privileges_not_seen"] == ["VIEW_DEFINITION"]


def test_a_failed_sqlserver_capture_read_never_fails_the_schema_stage():
    from src.agents.collector import sqlserver_collector

    db = _LiveDb(collect_tables=[], collect_routine_capture=RuntimeError("no permission"))
    assert sqlserver_collector._collect_schema_raw(db)["routine_capture"] is None


@pytest.mark.parametrize(
    ("granted", "missing"), [("1", []), ("0", ["VIEW_DEFINITION"]), (None, ["VIEW_DEFINITION"])]
)
def test_the_sqlserver_capture_names_view_definition_when_it_is_not_granted(granted, missing):
    from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

    class Db(SQLServerRemoteCollector):
        def _query(self, sql):
            assert "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')" in sql
            return [] if granted is None else [{"granted": granted}]

    capture = Db.__new__(Db).collect_routine_capture(include_bodies=False)
    assert capture == {"include_bodies": False, "privileges_not_seen": missing}


def test_the_live_oracle_collector_marks_each_body():
    from src.agents.collector import oracle_collector

    db = _LiveDb(
        collect_tables=[],
        collect_procedures=[
            {"routine_name": "p", "schema_name": "app", "routine_type": "PROCEDURE"},
            {"routine_name": "w", "schema_name": "app", "routine_type": "PROCEDURE"},
            {"routine_name": "gone", "schema_name": "app", "routine_type": "PROCEDURE"},
        ],
        collect_routine_sources=[
            {"owner": "app", "name": "p", "type": "PROCEDURE", "line": 1, "text": "PROCEDURE p IS BEGIN NULL; END;"},
            {"owner": "app", "name": "w", "type": "PROCEDURE", "line": 1, "text": "PROCEDURE w wrapped\na000000\n"},
        ],
    )  # fmt: skip
    raw = oracle_collector._collect_schema_raw(db)
    statuses = {p["routine_name"]: p["body_status"] for p in raw["procedures"]}
    assert statuses == {"p": "collected", "w": "not_readable", "gone": "not_readable"}
    assert raw["routine_capture"] == {"include_bodies": True, "privileges_not_seen": []}


def test_a_failed_oracle_body_read_says_the_read_failed_not_a_permission():
    from src.agents.collector import oracle_collector

    db = _LiveDb(
        collect_tables=[],
        collect_procedures=[
            {"routine_name": "p", "schema_name": "app", "routine_type": "PROCEDURE"}
        ],
        collect_routine_sources=TimeoutError("ORA-01013"),
    )
    (p,) = oracle_collector._collect_schema_raw(db)["procedures"]
    assert (p["body_status"], p["body_status_reason"]) == (
        "not_readable", "the read failed: TimeoutError",
    )  # fmt: skip
