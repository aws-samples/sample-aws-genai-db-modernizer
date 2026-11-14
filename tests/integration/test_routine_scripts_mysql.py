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
        proc.terminate()
        proc.wait(timeout=60)
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
                            "BEGIN\n  SELECT p;\nEND", schema="shop_functions")]  # fmt: skip
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
    finally:
        _mysql(client, sock, "DROP DATABASE IF EXISTS shop_functions;")
