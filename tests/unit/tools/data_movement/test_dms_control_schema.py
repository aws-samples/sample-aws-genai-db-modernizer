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
from tests.pg_server import PgServerUnavailable, start_pg_server
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
        # MySQL's failure table was already in awsdms_control, its documented default.
        pytest.param("mysql", ["3.5.4", "3.6.1", None], marks=pytest.mark.passes_on_main),
        ("postgresql", ["3.5.4", "3.6.0", "3.6.1", "3.6.2", None]),
    ],
)
def test_every_failure_table_step_1_names_is_in_the_control_schema(family, versions) -> None:
    for version in versions:
        assert failures_table(family, version).startswith(
            f"{CONTROL_SCHEMA}.awsdms_validation_failures_v"
        )


@pytest.mark.parametrize(
    "family", [pytest.param("mysql", marks=pytest.mark.passes_on_main), "postgresql", None]
)
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
    # The grant escapes the underscore (grant_database, #1970): `awsdms_control`.* would also
    # cover every database named like awsdms?control.
    assert "-- GRANT ALL PRIVILEGES ON `awsdms\\_control`.* TO 'dms_user'@'%';" in ddl
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


# Where a reader or a prompt could name the schema: Python, the SQL templates, the commands,
# subagents and skills. The README and docs/ describe it in prose behind claim markers.
_GUARDED = ("src/**/*.py", "scripts/**/*.py", "src/**/*.sql", "scripts/**/*.sql",
            ".claude/commands/*.md", ".claude/agents/*.md", "src/skills/**/*.md")  # fmt: skip


def test_no_reader_names_the_control_schema_or_a_default_one_outside_the_constant() -> None:
    # A hard-coded schema next to the constant would let one reader drift from the task.
    pattern = re.compile(r"awsdms_control|public\.awsdms_validation|public\.awsdms_apply")
    found, scanned = [], set()
    for glob in _GUARDED:
        for path in REPO.glob(glob):
            if path.name == "dms_control.py" or path in scanned:
                continue
            scanned.add(path)
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path.relative_to(REPO)}:{n}")
    assert found == []
    assert {p.suffix for p in scanned} == {".py", ".sql", ".md"}  # every kind was scanned


# --- A task deployed before step 1 set the control schema ----------------------------------


def test_task_control_schema_reads_the_live_task_settings() -> None:
    assert task_control_schema({}) is None
    assert task_control_schema({"ReplicationTaskSettings": "not json"}) is None
    assert task_control_schema({"ReplicationTaskSettings": json.dumps({"Logging": {}})}) == ""
    assert task_control_schema({"ReplicationTaskSettings": json.dumps(
        {"ControlTablesSettings": {"ControlSchema": CONTROL_SCHEMA}})}) == CONTROL_SCHEMA  # fmt: skip


def _mismatch_status(tmp_path, capsys, settings, engine="mysql"):
    _job(tmp_path)
    if engine != "mysql":
        plan = tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "plan.json"
        data = json.loads(plan.read_text())
        data["source"]["engine"] = engine
        plan.write_text(json.dumps(data))
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders", validation="Mismatched records", ValidationFailedRecords=1)],
             percent=100, settings=settings)  # fmt: skip
    _, _, out = _status(tmp_path, capsys, cred)
    return out


# What describe-replication-tasks returns for a task rendered before #2133: the full settings
# document, whose ControlTablesSettings has an empty ControlSchema (or none at all).
_CONTROL_DEFAULTS = {"HistoryTimeslotInMinutes": 5, "HistoryTableEnabled": False,
                     "SuspendedTablesTableEnabled": False, "StatusTableEnabled": False}  # fmt: skip
OLD_TASK = {"ValidationSettings": {"EnableValidation": True},
            "ControlTablesSettings": {**_CONTROL_DEFAULTS, "ControlSchema": ""}}  # fmt: skip
OLD_TASK_NO_KEY = {"ValidationSettings": {"EnableValidation": True},
                   "ControlTablesSettings": dict(_CONTROL_DEFAULTS)}  # fmt: skip
NEW_TASK = {"ValidationSettings": {"EnableValidation": True},
            "ControlTablesSettings": {**_CONTROL_DEFAULTS, "ControlSchema": CONTROL_SCHEMA}}  # fmt: skip


def _note(check) -> list[str]:
    return [s for s in check["check_on_target"] if "control schema" in s]


@pytest.mark.parametrize("settings", [OLD_TASK_NO_KEY], ids=["no-ControlSchema-key"])
def test_an_older_task_without_the_key_reads_as_no_control_schema(tmp_path, capsys, settings):
    check = _mismatch_status(tmp_path, capsys, settings)["validation_failures_check"]
    assert check["task_control_schema"] == "" and _note(check)


def test_an_older_mysql_task_gets_steps_that_work_on_a_running_task(tmp_path, capsys):
    check = _mismatch_status(tmp_path, capsys, OLD_TASK)["validation_failures_check"]
    steps = check["check_on_target"]
    assert steps[0].startswith("This task sets no DMS control schema")
    assert check["task_control_schema"] == ""
    note = " ".join(_note(check))
    # Where DMS put them, first: the lookup query is in the same check.
    assert "the first query below" in note and any("information_schema" in s for s in steps)
    # Failures already counted never show up by themselves.
    assert "will not appear later by themselves" in note
    # DMS modifies only a stopped task: stop, modify the full settings, resume, revalidate.
    flat = " ".join(steps)
    order = ["aws dms stop-replication-task", "aws dms describe-replication-tasks",
             "aws dms modify-replication-task", "--replication-task-settings file://",
             "aws dms start-replication-task", "resume-processing", "revalidate"]  # fmt: skip
    at = [flat.index(w) for w in order]
    assert at == sorted(at)
    # The saved document already has a ControlTablesSettings object: set the key inside it,
    # never add a second object (a duplicate key).
    assert (f'set "ControlSchema" to "{CONTROL_SCHEMA}" inside its existing '
            '"ControlTablesSettings" object') in flat  # fmt: skip
    assert '"ControlTablesSettings": {' not in flat and "add " not in flat
    # A stop costs the source: its binary logs are purged once the retention period passes.
    assert "as long as the source still keeps its change log" in flat
    assert "purges its binary logs once their retention period passes" in flat
    assert "the source keeps its change log for it" not in flat
    assert f"--replication-task-arn {TASK}" in flat and "--region us-east-1" in flat
    # Never a redeploy through /move, which goes back to Prove on a running task.
    assert "render" not in note and "deploy" not in note
    for word in ("cutover", "caught up", "in sync", "proven", "ready"):
        assert not re.search(rf"\b{word}\b", flat.casefold()), word


def test_the_note_steps_lead_back_to_prove_with_no_note_once_the_task_carries_it(tmp_path, capsys):
    # Prove's status before and after the customer's modify: the note goes, the queries stay.
    # (/move's Resume routing to Prove is prose in move.md, not exercised here.)
    before = _mismatch_status(tmp_path, capsys, OLD_TASK)["validation_failures_check"]
    after = _mismatch_status(tmp_path, capsys, NEW_TASK)["validation_failures_check"]
    assert _note(before) and not _note(after)
    assert after["task_control_schema"] == CONTROL_SCHEMA
    assert [s for s in after["check_on_target"] if s.startswith("SELECT")] == [
        s for s in before["check_on_target"] if s.startswith("SELECT")]  # fmt: skip


def test_an_older_postgresql_task_gets_no_note_its_failures_are_in_public(tmp_path, capsys):
    # With no ControlSchema DMS writes them to PostgreSQL's documented default, public: the
    # lookup query finds them there, nothing is missing.
    out = _mismatch_status(tmp_path, capsys, OLD_TASK, engine="postgresql")
    check = out["validation_failures_check"]
    assert not _note(check) and check["task_control_schema"] == ""
    assert any("information_schema" in s for s in check["check_on_target"])


def test_a_task_with_the_control_schema_gets_no_rerun_note(tmp_path, capsys):
    check = _mismatch_status(tmp_path, capsys, NEW_TASK)["validation_failures_check"]
    assert not _note(check)
    assert check["task_control_schema"] == CONTROL_SCHEMA


@pytest.mark.passes_on_main  # no settings to read: the check is main's
def test_a_task_whose_settings_dms_does_not_show_gets_no_rerun_note(tmp_path, capsys):
    check = _mismatch_status(tmp_path, capsys, None)["validation_failures_check"]
    assert not _note(check)
    assert "task_control_schema" not in check


def test_the_progress_line_keeps_the_failures_with_the_note(tmp_path, capsys):
    out = _mismatch_status(tmp_path, capsys, OLD_TASK)
    assert out["validation_failures_check"]["check_on_target"][0].startswith(
        "This task sets no DMS control schema"
    )
    assert script.progress_line(out)["validation_failures"]  # the failures stay on the line


def _gate(tmp_path, capsys, settings, engine="mysql"):
    from scripts import check_dms_step_gate
    from src.storage.local_store import LocalArtifactStore
    from src.tools.transformation_contract.hashing import canonical_json
    from tests.unit.tools.data_movement import gate_stubs as g
    from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle

    b = make_bundle()
    plan = b.plan.model_dump(mode="json")
    plan["source"]["engine"] = engine
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", plan)
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    cred = StubbedCredMgr()
    stats = [g.stat("shop.audit_log", rows=12, validation="No primary key"), g.stat("shop.customers"),
             g.stat("shop.orders", validation="Mismatched records", failed=2)]  # fmt: skip
    g.queue(cred, b.plan.contract.sha256, stats=stats, settings=settings)
    argv = ["--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path),
            "--region", g.REGION, "--stack-name", g.STACK]  # fmt: skip
    assert check_dms_step_gate.main(argv, clients=g.clients(cred)) == 0
    return json.loads(capsys.readouterr().out)["validation_failures_check"]


def test_the_gate_says_what_prove_says_about_the_same_task(tmp_path, capsys):
    gate_old = _gate(tmp_path / "g1", capsys, OLD_TASK)
    prove_old = _mismatch_status(tmp_path / "p1", capsys, OLD_TASK)["validation_failures_check"]
    gate_new = _gate(tmp_path / "g2", capsys, NEW_TASK)
    assert _note(gate_old) and gate_old["task_control_schema"] == ""
    assert _note(gate_old)[0].split(":", 1)[0] == _note(prove_old)[0].split(":", 1)[0]
    assert not _note(gate_new) and gate_new["task_control_schema"] == CONTROL_SCHEMA
    assert not _note(_gate(tmp_path / "g3", capsys, OLD_TASK, engine="postgresql"))


# --- On a real PostgreSQL -----------------------------------------------------------------------


def _uncommented_grants(ddl: str) -> str:
    """The DMS user lines a customer uncomments, without the RDS-only role grant."""
    lines = [line[3:] for line in ddl.splitlines()
             if line.startswith(("-- CREATE USER", "-- GRANT", "-- ALTER DEFAULT"))
             and "rds_superuser" not in line and "ON PARAMETER" not in line]  # fmt: skip
    return "\n".join(lines) + "\n"


@pytest.fixture
def pg():
    """A PostgreSQL from ``pgserver`` on a Unix socket in a temp folder (no TCP listener),
    with an empty ``shop`` database; ``run`` sends SQL to it through psql."""
    pgserver = pytest.importorskip("pgserver")
    try:
        srv = start_pg_server()
    except PgServerUnavailable as exc:  # the one failure that skips
        pytest.skip(f"no pgserver PostgreSQL here: {exc}")
    srv.psql('CREATE DATABASE "shop";')
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    uri = srv.get_uri("shop")

    def run(sql: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(psql), uri, "-X", "-v", "ON_ERROR_STOP=1"], input=sql,
                              capture_output=True, text=True, check=False)  # fmt: skip

    yield run
    srv.cleanup()


_MAKE_FAILURES = (f"SET ROLE dms_user; CREATE TABLE {CONTROL_SCHEMA}.awsdms_validation_failures_v2 "
                  '("TASK_NAME" varchar(128) NOT NULL); SELECT 1;')  # fmt: skip
_CONTROL_GRANT = f'GRANT USAGE, CREATE ON SCHEMA "{CONTROL_SCHEMA}" TO dms_user;'


def test_on_postgresql_the_dms_user_can_create_its_failure_table_in_the_control_schema(pg):
    ddl = render_ddl(make_step1_inputs("aurora_postgresql"))
    setup = pg(ddl.replace("<password>", "x") + _uncommented_grants(ddl))
    assert setup.returncode == 0, setup.stderr
    made = pg(_MAKE_FAILURES)
    assert made.returncode == 0, made.stderr
    # The tables stay where the design put them.
    tables = pg("SELECT table_schema || '.' || table_name FROM information_schema.tables "
                f"WHERE table_schema IN ('public', '{CONTROL_SCHEMA}') ORDER BY 1;")  # fmt: skip
    assert f"{CONTROL_SCHEMA}.awsdms_validation_failures_v2" in tables.stdout
    assert "public.orders" in tables.stdout


def test_on_postgresql_an_earlier_dms_user_gets_the_one_grant_the_hint_names(pg):
    # A DMS user made from an earlier step-1.ddl.sql (every grant but the control schema's),
    # then this DDL: the administrator now owns awsdms_control, so DMS cannot create its
    # tables there. The real error matches the hint, and the DDL's one grant fixes it.
    from src.tools.data_movement.dms_failures import match_failure

    ddl = render_ddl(make_step1_inputs("aurora_postgresql"))
    earlier = _uncommented_grants(ddl).replace(_CONTROL_GRANT + "\n", "")
    assert _CONTROL_GRANT not in earlier
    first = pg(ddl.replace("<password>", "x").split("CREATE TABLE", 1)[0] + "SELECT 1;")
    assert first.returncode == 0, first.stderr
    assert pg(earlier.replace("<password>", "x")).returncode == 0
    refused = pg(_MAKE_FAILURES)
    assert refused.returncode != 0
    assert f"permission denied for schema {CONTROL_SCHEMA}" in refused.stderr
    hint = match_failure(refused.stderr)
    assert hint is not None and hint.id == "postgresql_control_schema"
    assert 'GRANT USAGE, CREATE ON SCHEMA "awsdms_control" TO' in hint.hint
    flat = " ".join(ddl.replace("-- ", "").split())
    assert "A DMS user created from an earlier step-1.ddl.sql needs only the" in flat
    assert pg(_CONTROL_GRANT).returncode == 0
    made = pg(_MAKE_FAILURES)
    assert made.returncode == 0, made.stderr
