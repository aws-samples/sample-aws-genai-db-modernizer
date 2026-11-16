# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routine scripts on a real MySQL (#1947), run as the customer runs them.

Needs ``mysqld`` and the ``mysql`` client on PATH: the test starts a throwaway server on
a Unix socket in a temp folder (no TCP listener, no binary log), runs
``mysql < routines-deploy.sql`` and ``mysql --batch --skip-column-names <
routines-check.sql > routines-check.tsv`` as ``routines.md`` says, and reads the check
output. Run it with ``uv run pytest -m integration
tests/integration/test_routine_scripts_mysql.py``. The PostgreSQL twin is hermetic
(pgserver) and stays in ``tests/unit/tools/data_movement/test_routine_scripts_engine.py``.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs mysqld and the mysql client with fixed argv
from pathlib import Path

import pytest

from src.tools.data_movement.routine_check import read_check
from src.tools.data_movement.routine_scripts import CHECK_FILE, CHECK_OUTPUT, DEPLOY_FILE
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    migration,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job, _write

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mysql_socket():
    """A throwaway mysqld on a Unix socket (no TCP, no binary log), or a skip."""
    import shutil
    import tempfile
    import time

    mysqld, client = shutil.which("mysqld"), shutil.which("mysql")
    if not mysqld or not client:
        pytest.skip("no mysqld and mysql client on PATH")
    work = Path(tempfile.mkdtemp(prefix="my1947", dir="/tmp"))
    base = [
        mysqld,
        "--no-defaults",
        f"--datadir={work / 'data'}",
        f"--log-error={work / 'err.log'}",
    ]
    if subprocess.run(
        [*base, "--initialize-insecure"], capture_output=True, check=False
    ).returncode:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip("mysqld cannot initialise a data folder here")
    sock = work / "m.sock"
    proc = subprocess.Popen([*base, "--skip-networking", f"--socket={sock}", "--skip-log-bin",
                             "--mysqlx=OFF", f"--pid-file={work / 'm.pid'}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(120):
            if sock.exists() and _mysql(client, sock, "SELECT 1").returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.skip("mysqld did not start")
        yield client, sock
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=60)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _mysql(client, sock, sql: str | None = None, *args: str, stdin: Path | None = None):
    argv = [client, "--no-defaults", "-uroot", f"--socket={sock}", *args]
    if sql is not None:
        argv += ["-e", sql]
    data = stdin.read_bytes() if stdin else None
    return subprocess.run(argv, input=data, capture_output=True, check=False)


def test_the_scripts_deploy_and_confirm_on_mysql_with_triggers_left_to_the_triggers_enable_script(
    mysql_socket, tmp_path
):
    from tests.unit.tools.data_movement.routine_fixtures import mysql_source

    client, sock = mysql_socket
    _mysql(client, sock, "CREATE DATABASE shop_new; CREATE TABLE shop_new.orders (id int PRIMARY KEY);"
           "CREATE USER 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';")  # fmt: skip
    rows = [
        row("shop.place_order", "procedure", schema="shop", status="rewritten",
            signature="IN INT", specific="place_order"),
        row("shop.total", "function", schema="shop", status="rewritten", signature="INT",
            returns="INT", specific="total"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders", specific="orders.orders_bi"),
    ]  # fmt: skip
    artifact = migration(rows, "mysql")
    sources = [
        mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN p_id INT)\n"
                     f"BEGIN\n  SELECT '{SENTINEL};;', p_id;\nEND"),
        mysql_source("total", "CREATE DEFINER=`app`@`%` FUNCTION `total`(p INT) RETURNS int\n"
                     "    DETERMINISTIC\nRETURN p + 1", kind="function"),
        mysql_source("orders.orders_bi", "CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE INSERT "
                     "ON `orders` FOR EACH ROW SET NEW.id = NEW.id", kind="trigger"),
    ]  # fmt: skip
    store = _job(tmp_path, artifact, sources, "mysql")
    contract = contract_with_routines(artifact)
    assert _write(tmp_path, store, contract, target_database="shop_new").in_script == 2
    step = tmp_path / "step"
    deployed = _mysql(client, sock, None, stdin=step / DEPLOY_FILE)
    assert deployed.returncode == 0, deployed.stderr.decode()

    def check():
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names", stdin=step / CHECK_FILE)
        assert ran.returncode == 0, ran.stderr.decode()
        (step / CHECK_OUTPUT).write_bytes(ran.stdout)
        return read_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                          contract_sha=SHA, target_database="shop_new")  # fmt: skip

    result = check()
    assert (result.status, result.confirmed, result.deferred, result.mismatches) == (
        "pass",
        2,
        1,
        [],
    )
    definer = _mysql(client, sock, "SELECT DEFINER, SQL_MODE FROM information_schema.ROUTINES "
                     "WHERE ROUTINE_NAME = 'place_order'", "--batch", "--skip-column-names")  # fmt: skip
    assert definer.stdout.decode().split("\t") == ["admin@%", "STRICT_TRANS_TABLES\n"]
    _mysql(client, sock, "CREATE TRIGGER shop_new.orders_bi BEFORE INSERT ON shop_new.orders "
           "FOR EACH ROW SET NEW.id = NEW.id")  # fmt: skip
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "shop.orders_bi": "present_before_triggers_enable"}  # fmt: skip


def test_the_deploy_creates_a_routine_database_the_step_tables_are_not_in(mysql_socket, tmp_path):
    from tests.unit.tools.data_movement.routine_fixtures import mysql_source

    client, sock = mysql_socket
    _mysql(client, sock, "CREATE USER IF NOT EXISTS 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';"
           "DROP DATABASE IF EXISTS shop_functions;")  # fmt: skip
    rows = [row("shop_functions.tally", "procedure", schema="shop_functions", status="rewritten",
                signature="IN INT", specific="tally")]  # fmt: skip
    artifact = migration(rows, "mysql")
    sources = [mysql_source("tally", "CREATE DEFINER=`app`@`%` PROCEDURE `tally`(IN p INT)\n"
                            "BEGIN\n  SELECT p;\nEND", schema="shop_functions",
                            database_collation="utf8mb4_unicode_ci")]  # fmt: skip
    store = _job(tmp_path, artifact, sources, "mysql")
    contract = contract_with_routines(artifact)
    assert _write(tmp_path, store, contract).in_script == 1
    step = tmp_path / "step"
    try:
        deployed = _mysql(client, sock, None, stdin=step / DEPLOY_FILE)
        assert deployed.returncode == 0, deployed.stderr.decode()
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names", stdin=step / CHECK_FILE)
        assert ran.returncode == 0, ran.stderr.decode()
        (step / CHECK_OUTPUT).write_bytes(ran.stdout)
        result = read_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                            contract_sha=SHA, target_database=None)  # fmt: skip
        assert (result.status, result.confirmed, result.mismatches) == ("pass", 1, [])
        made = _mysql(client, sock, "SELECT DEFAULT_COLLATION_NAME = @@collation_server FROM "
                      "information_schema.SCHEMATA WHERE SCHEMA_NAME = 'shop_functions'",
                      "--batch", "--skip-column-names")  # fmt: skip
        assert (
            made.stdout.decode().strip() == "1"
        )  # the server default, as step-1.ddl.sql's tables get
    finally:
        _mysql(client, sock, "DROP DATABASE IF EXISTS shop_functions;")


def _collation_job(tmp_path):
    """A MySQL job on utf8mb4_unicode_ci (#2276): step-1.ddl.sql, and routines-deploy.sql
    with a procedure in the tables' database and one in a database of its own, each
    comparing its VARCHAR parameter with the moved ``shop.audit_log.msg``."""
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.transformation_contract import StepContract
    from src.tools.data_movement.dms_step import load_step1, render_ddl
    from src.tools.data_movement.routine_scripts import write_routine_scripts
    from src.tools.routines.migration import routine_migration_path
    from tests.unit.tools.data_movement.routine_fixtures import mysql_source
    from tests.unit.tools.data_movement.test_dms_step_collations import _job as collation_job

    store = collation_job(tmp_path)
    step = tmp_path / "step"
    step.mkdir()
    (step / "step-1.ddl.sql").write_text(render_ddl(load_step1(store, "shop", "job1")))
    rows, sources = [], []
    for schema in ("shop", "shop_functions"):
        rows.append(row(f"{schema}.find_msg", "procedure", schema=schema, name="find_msg",
                        specific="find_msg", status="rewritten", signature="IN VARCHAR(200)"))  # fmt: skip
        sources.append(mysql_source("find_msg", "CREATE DEFINER=`app`@`%` PROCEDURE `find_msg`(IN p VARCHAR(200))\n"
                                    "BEGIN\n  SELECT COUNT(*) FROM shop.audit_log WHERE msg = p;\nEND",
                                    schema=schema, collation="utf8mb4_unicode_ci",
                                    database_collation="utf8mb4_unicode_ci"))  # fmt: skip
    artifact = migration(rows, "mysql")
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    write_routine_sources(store, "shop", "job1",
                          {"metadata": {"source_database": {"engine": "mysql", "version": "8.0.32"}}},
                          sources)  # fmt: skip
    contract = StepContract.model_validate(
        store.read_json("shop/job1/transformation-contract/v2/step-1.json")
    )
    scripts = write_routine_scripts(store, "shop", "job1", contract_with_routines(artifact, contract=contract),
                                    SHA, step)  # fmt: skip
    assert scripts.in_script == 2, scripts.refused
    return step


def _value(client, sock, sql: str):
    ran = _mysql(client, sock, sql, "--batch", "--skip-column-names")
    assert ran.returncode == 0, ran.stderr.decode()
    return ran.stdout.decode().strip()


def test_tables_and_routines_land_on_the_source_collation_and_a_parameter_compares_with_a_column(
    mysql_socket, tmp_path
):
    # The #2276 example: a source on utf8mb4_unicode_ci, a cluster on utf8mb4_0900_ai_ci.
    client, sock = mysql_socket
    reset = "DROP DATABASE IF EXISTS shop; DROP DATABASE IF EXISTS shop_functions;"
    _mysql(
        client,
        sock,
        reset + "CREATE USER IF NOT EXISTS 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';",
    )
    assert _value(client, sock, "SELECT @@collation_server") == "utf8mb4_0900_ai_ci"
    step = _collation_job(tmp_path)
    try:
        for script in ("step-1.ddl.sql", DEPLOY_FILE):
            ran = _mysql(client, sock, None, stdin=step / script)
            assert ran.returncode == 0, ran.stderr.decode()
        _mysql(client, sock, "INSERT INTO shop.audit_log (msg) VALUES ('x');")
        assert _value(client, sock, "SELECT GROUP_CONCAT(DEFAULT_COLLATION_NAME ORDER BY SCHEMA_NAME) "
                      "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ('shop', "
                      "'shop_functions')") == "utf8mb4_unicode_ci,utf8mb4_unicode_ci"  # fmt: skip
        assert _value(client, sock, "SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE "
                      "TABLE_SCHEMA = 'shop' AND TABLE_NAME = 'audit_log' AND COLUMN_NAME = 'msg'"
                      ) == "utf8mb4_unicode_ci"  # fmt: skip
        for schema in ("shop", "shop_functions"):
            assert _value(client, sock, f"CALL {schema}.find_msg('x')") == "1"
        # The control: tables as step-1.ddl.sql made them before #2276 (no clause, so the
        # server default) and a routine database on the source collation (made here, as PR
        # #2272's round 2 deploy made it) fail the same call.
        _mysql(client, sock, reset + "CREATE DATABASE shop_functions COLLATE utf8mb4_unicode_ci;")
        ddl = (step / "step-1.ddl.sql").read_text()
        old = tmp_path / "old.ddl.sql"
        old.write_text(ddl.replace(" CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;", ";"))
        assert _mysql(client, sock, None, stdin=old).returncode == 0
        assert _mysql(client, sock, None, stdin=step / DEPLOY_FILE).returncode == 0
        _mysql(client, sock, "INSERT INTO shop.audit_log (msg) VALUES ('x');")
        mixed = _mysql(client, sock, "CALL shop_functions.find_msg('x')")
        assert mixed.returncode != 0 and b"ERROR 1267" in mixed.stderr, mixed.stderr.decode()
    finally:
        _mysql(client, sock, reset)


def test_a_database_that_already_exists_lands_on_the_source_collation_with_the_alter_the_scripts_name(
    mysql_socket, tmp_path
):
    # Review of #2282: CREATE DATABASE IF NOT EXISTS keeps an existing database's collation.
    # Created beforehand on the server default, `shop` gives the tables utf8mb4_0900_ai_ci next
    # to routines on utf8mb4_unicode_ci (ERROR 1267); the ALTER DATABASE the scripts name, run
    # first while the database holds nothing else, puts it on the source's.
    import re

    client, sock = mysql_socket
    reset = "DROP DATABASE IF EXISTS shop; DROP DATABASE IF EXISTS shop_functions;"
    _mysql(
        client,
        sock,
        reset + "CREATE USER IF NOT EXISTS 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';",
    )
    step = _collation_job(tmp_path)
    ddl = (step / "step-1.ddl.sql").read_text()
    assert len(re.findall(r"^-- ALTER DATABASE `shop` [^\n]*;$", ddl, re.M)) == 1
    uncommented = tmp_path / "uncommented"
    uncommented.mkdir()
    for script in ("step-1.ddl.sql", DEPLOY_FILE):  # each ALTER uncommented where it stands
        text = (step / script).read_text()
        (uncommented / script).write_text(re.sub(r"^-- (ALTER DATABASE )", r"\1", text, flags=re.M))
    try:
        # As written, a `shop` made beforehand; then uncommented in place, with `shop` made
        # beforehand and on a fresh cluster (where an ALTER above its CREATE would fail, 3503).
        for folder, before in ((step, True), (uncommented, True), (uncommented, False)):
            _mysql(client, sock, reset + ("CREATE DATABASE shop;" if before else ""))
            for script in ("step-1.ddl.sql", DEPLOY_FILE):
                ran = _mysql(client, sock, None, stdin=folder / script)
                assert ran.returncode == 0, ran.stderr.decode()
            _mysql(client, sock, "INSERT INTO shop.audit_log (msg) VALUES ('x');")
            called = _mysql(
                client, sock, "CALL shop_functions.find_msg('x')", "--batch", "--skip-column-names"
            )
            if folder is step:
                assert (
                    called.returncode != 0 and b"ERROR 1267" in called.stderr
                ), called.stderr.decode()
            else:
                assert (
                    called.returncode == 0 and called.stdout.decode().strip() == "1"
                ), called.stderr.decode()
    finally:
        _mysql(client, sock, reset)
