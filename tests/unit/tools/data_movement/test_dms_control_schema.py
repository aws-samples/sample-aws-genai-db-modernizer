# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DMS control schema (#2133): the task settings name one ``ControlSchema`` on both
target engines, the DDL file creates it and grants the DMS user on it, and every failure
query step 1 prints reads the failure table in it. With no ``ControlSchema`` the DMS 3.6.1
validator on an Aurora MySQL target could not create ``awsdms_validation_failures_v1``, so
the printed query found nothing."""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 -- runs the bundled psql with fixed argv
from pathlib import Path

import pytest

from scripts import run_dms_step as script
from src.tools.data_movement.dms_control import CONTROL_SCHEMA, task_control_schema
from src.tools.data_movement.dms_step import render_ddl, task_settings
from src.tools.data_movement.table_errors import failures_check, failures_table
from tests.unit.scripts.test_run_dms_step import TASK, _job
from tests.unit.scripts.test_run_dms_step_table_errors import _row, _running, _status
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr
from tests.unit.tools.data_movement.dms_step_fixtures import make_step1_inputs

ENGINES = ["aurora_mysql", "aurora_postgresql"]
REPO = Path(__file__).resolve().parents[4]


def test_the_control_schema_is_dms_s_own_name_for_its_control_database() -> None:
    assert CONTROL_SCHEMA == "awsdms_control"


@pytest.mark.parametrize("engine", ENGINES)
def test_the_task_settings_name_the_control_schema_on_both_engines(engine) -> None:
    settings = task_settings(make_step1_inputs(engine))
    assert settings["ControlTablesSettings"] == {"ControlSchema": CONTROL_SCHEMA}


@pytest.mark.parametrize(
    "family, versions",
    [
        ("mysql", ["3.5.4", "3.6.1", None]),
        ("postgresql", ["3.5.4", "3.6.0", "3.6.1", "3.6.2", None]),
    ],
)
def test_every_failure_table_step_1_names_is_in_the_control_schema(family, versions) -> None:
    for version in versions:
        assert failures_table(family, version).startswith(
            f"{CONTROL_SCHEMA}.awsdms_validation_failures_v"
        )


@pytest.mark.parametrize("family", ["mysql", "postgresql", None])
def test_every_printed_failure_query_reads_the_control_schema(family) -> None:
    check = failures_check(family, "arn:aws:dms:us-east-1:111122223333:task:TASK", "3.6.1")
    reads = [s for s in check["check_on_target"] if " FROM " in s and "information_schema" not in s]
    assert reads and len(reads) == (1 if family else 2)
    for sql in reads:
        table = re.search(r" FROM (\S+) WHERE", sql).group(1)
        assert table.startswith(f"{CONTROL_SCHEMA}.awsdms_validation_failures_v"), sql
    assert "public.awsdms_" not in json.dumps(check)


def test_the_postgresql_query_keeps_the_v1_v2_split_by_dms_version_under_the_control_schema():
    old = failures_check("postgresql", TASK, "3.5.4")["check_on_target"]
    new = failures_check("postgresql", TASK, "3.6.1")["check_on_target"]
    assert any(f"FROM {CONTROL_SCHEMA}.awsdms_validation_failures_v1 " in s for s in old)
    assert any(f"FROM {CONTROL_SCHEMA}.awsdms_validation_failures_v2 " in s for s in new)


# --- Target setup ------------------------------------------------------------------------------


def test_the_mysql_ddl_creates_the_control_database_and_the_dms_user_gets_it() -> None:
    ddl = render_ddl(make_step1_inputs("aurora_mysql"))
    create = f"\nCREATE DATABASE IF NOT EXISTS `{CONTROL_SCHEMA}`;\n"
    assert create in ddl and ddl.index(create) < ddl.index("CREATE TABLE")
    assert f"-- GRANT ALL PRIVILEGES ON `{CONTROL_SCHEMA}`.* TO 'dms_user'@'%';" in ddl
    # The target database stays the one the tables go in.
    assert ddl.index(create) < ddl.index("\nUSE `shop`;")
    assert "DROP " not in ddl


def test_the_postgresql_ddl_creates_the_control_schema_and_the_dms_user_gets_it() -> None:
    ddl = render_ddl(make_step1_inputs("aurora_postgresql"))
    create = f'\nCREATE SCHEMA IF NOT EXISTS "{CONTROL_SCHEMA}";\n'
    assert create in ddl and ddl.index(create) < ddl.index("CREATE TABLE")
    assert f'-- GRANT USAGE, CREATE ON SCHEMA "{CONTROL_SCHEMA}" TO dms_user;' in ddl
    # The tables still land in the design's schema, not in the control schema.
    assert ddl.index(create) < ddl.index('SET search_path TO "public";')
    assert "DROP " not in ddl


@pytest.mark.parametrize("engine", ENGINES)
def test_the_ddl_says_where_dms_records_its_control_tables(engine) -> None:
    flat = " ".join(render_ddl(make_step1_inputs(engine)).replace("-- ", "").split())
    assert f"DMS keeps its control tables in {CONTROL_SCHEMA}" in flat
    assert "awsdms_validation_failures" in flat


# --- One constant -----------------------------------------------------------------------------


def test_no_reader_names_the_control_schema_or_a_default_one_outside_the_constant() -> None:
    # A hard-coded schema next to the constant would let one reader drift from the task.
    pattern = re.compile(r"awsdms_control|public\.awsdms_validation|public\.awsdms_apply")
    found = []
    for base in ("src", "scripts"):
        for path in (REPO / base).rglob("*.py"):
            if path.name == "dms_control.py":
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(REPO)}:{n}")
    assert found == []


# --- A task deployed before step 1 set the control schema ----------------------------------


def test_task_control_schema_reads_the_live_task_settings() -> None:
    assert task_control_schema({}) is None
    assert task_control_schema({"ReplicationTaskSettings": "not json"}) is None
    assert task_control_schema({"ReplicationTaskSettings": json.dumps({"Logging": {}})}) == ""
    assert task_control_schema({"ReplicationTaskSettings": json.dumps(
        {"ControlTablesSettings": {"ControlSchema": CONTROL_SCHEMA}})}) == CONTROL_SCHEMA  # fmt: skip


def _mismatch_status(tmp_path, capsys, settings):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders", validation="Mismatched records", ValidationFailedRecords=1)],
             percent=100, settings=settings)  # fmt: skip
    _, _, out = _status(tmp_path, capsys, cred)
    return out["validation_failures_check"]


def test_a_task_from_an_older_bundle_is_told_to_render_and_deploy_again(tmp_path, capsys):
    check = _mismatch_status(tmp_path, capsys, {"ValidationSettings": {"EnableValidation": True}})
    first = check["check_on_target"][0]
    assert first.startswith("This task sets no DMS control schema")
    assert "render_dms_step.py" not in first and "/move step-1" in first
    assert CONTROL_SCHEMA in first and "may not have recorded" in first
    assert check["task_control_schema"] == ""


def test_a_task_with_the_control_schema_gets_no_rerun_note(tmp_path, capsys):
    check = _mismatch_status(
        tmp_path, capsys, {"ControlTablesSettings": {"ControlSchema": CONTROL_SCHEMA}}
    )
    assert not any("control schema" in s for s in check["check_on_target"])
    assert check["task_control_schema"] == CONTROL_SCHEMA


def test_a_task_whose_settings_dms_does_not_show_gets_no_rerun_note(tmp_path, capsys):
    check = _mismatch_status(tmp_path, capsys, None)
    assert not any("control schema" in s for s in check["check_on_target"])
    assert "task_control_schema" not in check


def test_the_progress_line_keeps_the_rerun_note_for_the_customer(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders", validation="Mismatched records", ValidationFailedRecords=1)],
             percent=100, settings={"Logging": {"EnableLogging": True}})  # fmt: skip
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["validation_failures_check"]["check_on_target"][0].startswith(
        "This task sets no DMS control schema"
    )
    assert script.progress_line(out)["validation_failures"]  # the failures stay on the line


# --- On a real PostgreSQL -----------------------------------------------------------------------


def _uncommented_grants(ddl: str) -> str:
    """The DMS user lines a customer uncomments, without the RDS-only role grant."""
    lines = [line[3:] for line in ddl.splitlines()
             if line.startswith(("-- CREATE USER", "-- GRANT", "-- ALTER DEFAULT"))
             and "rds_superuser" not in line and "ON PARAMETER" not in line]  # fmt: skip
    return "\n".join(lines) + "\n"


def test_on_postgresql_the_dms_user_can_create_its_failure_table_in_the_control_schema(tmp_path):
    pgserver = pytest.importorskip("pgserver")
    try:  # a Unix socket in a temp folder: no TCP listener
        srv = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    try:
        ddl = render_ddl(make_step1_inputs("aurora_postgresql"))
        srv.psql('CREATE DATABASE "shop";')
        psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
        uri = srv.get_uri("shop")

        def run(sql: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([str(psql), uri, "-X", "-v", "ON_ERROR_STOP=1"], input=sql,
                                  capture_output=True, text=True, check=False)  # fmt: skip

        setup = run(ddl.replace("<password>", "x") + _uncommented_grants(ddl))
        assert setup.returncode == 0, setup.stderr
        made = run(f"SET ROLE dms_user; CREATE TABLE {CONTROL_SCHEMA}.awsdms_validation_failures_v2 "
                   '("TASK_NAME" varchar(128) NOT NULL); SELECT 1;')  # fmt: skip
        assert made.returncode == 0, made.stderr
        # The tables stay where the design put them.
        tables = run("SELECT table_schema || '.' || table_name FROM information_schema.tables "
                     f"WHERE table_schema IN ('public', '{CONTROL_SCHEMA}') ORDER BY 1;")  # fmt: skip
        assert f"{CONTROL_SCHEMA}.awsdms_validation_failures_v2" in tables.stdout
        assert "public.orders" in tables.stdout
    finally:
        srv.cleanup()
