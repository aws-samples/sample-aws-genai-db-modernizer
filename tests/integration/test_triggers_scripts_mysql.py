# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The triggers-enable scripts on a real MySQL (#1948), run as the customer runs them.

Needs ``mysqld`` and the ``mysql`` client on PATH (the throwaway server of
``test_routine_scripts_mysql.py``: a Unix socket in a temp folder, no TCP listener, no
binary log). Step 1's deploy script creates the procedure; then ``mysql <
triggers-enable.sql`` creates the trigger and the events step 1 deferred, each under its
own sql_mode with the DEFINER rewritten, and ``mysql --batch --skip-column-names <
triggers-enable-check.sql > triggers-enable-check.tsv`` is read. Run it with ``uv run pytest -m
integration tests/integration/test_triggers_scripts_mysql.py``.
"""

from __future__ import annotations

import pytest

from src.tools.data_movement.routine_scripts import DEPLOY_FILE
from src.tools.data_movement.triggers_check import read_triggers_check
from src.tools.data_movement.triggers_scripts import (
    TRIGGERS_CHECK_FILE,
    TRIGGERS_CHECK_OUTPUT,
    TRIGGERS_FILE,
    write_triggers_scripts,
)
from tests.integration import test_routine_scripts_mysql as twin
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    mysql_source,
    row,
)
from tests.unit.tools.data_movement.test_routine_scripts import SHA, _job, _write

pytestmark = pytest.mark.integration
_mysql = twin._mysql
mysql_socket = twin.mysql_socket  # the throwaway mysqld fixture, one server for this module


def test_the_triggers_enable_script_creates_the_deferred_trigger_and_events_as_on_the_source(
    mysql_socket, tmp_path
):
    client, sock = mysql_socket
    _mysql(client, sock, "CREATE DATABASE cut_new; CREATE TABLE cut_new.orders "
           "(id int PRIMARY KEY, note varchar(64)); CREATE USER IF NOT EXISTS 'admin'@'%'; "
           "GRANT ALL ON *.* TO 'admin'@'%';")  # fmt: skip
    rows = [
        row("shop.stamp", "procedure", schema="shop", status="rewritten", signature="",
            specific="stamp"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders", status="rewritten",
            specific="orders.orders_bi"),
        row("shop.nightly", "event", schema="shop", status="rewritten", specific="nightly",
            dependencies=[dep_routine("procedure", "shop.stamp")]),
        row("shop.paused", "event", schema="shop", status="rewritten", specific="paused"),
        row("shop.replica", "event", schema="shop", status="rewritten", specific="replica"),
    ]  # fmt: skip
    event = ("CREATE DEFINER=`app`@`%` EVENT `{name}` ON SCHEDULE EVERY 1 DAY STARTS "
             "'2036-01-01 00:00:00' ON COMPLETION PRESERVE {state} COMMENT 'DISABLE' DO {body}")  # fmt: skip
    sources = [
        mysql_source("stamp", "CREATE DEFINER=`app`@`%` PROCEDURE `stamp`()\nBEGIN SELECT 1; END"),
        mysql_source("orders.orders_bi", "CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE INSERT "
                     f"ON `orders` FOR EACH ROW SET NEW.note = \"{SENTINEL}\"", kind="trigger",
                     sql_mode="NO_ENGINE_SUBSTITUTION"),
        mysql_source("nightly", event.format(name="nightly", state="ENABLE", body="CALL stamp()"),
                     kind="event"),
        mysql_source("paused", event.format(name="paused", state="DISABLE", body="SELECT 1"),
                     kind="event"),
        mysql_source("replica", event.format(name="replica", state="DISABLE ON SLAVE",
                                             body="SELECT 1"), kind="event"),
    ]  # fmt: skip
    artifact = migration(rows, "mysql")
    store = _job(tmp_path, artifact, sources, "mysql")
    contract = contract_with_routines(artifact)
    _write(tmp_path, store, contract, target_database="cut_new")
    step = tmp_path / "step"
    deployed = _mysql(client, sock, None, stdin=step / DEPLOY_FILE)
    assert deployed.returncode == 0, deployed.stderr.decode()
    scripts = write_triggers_scripts(store, "shop", "job1", contract, SHA, step,
                                    target_database="cut_new")  # fmt: skip
    assert (scripts.expected, scripts.in_script, scripts.events_enabled) == (4, 4, 1)

    def check():
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names",
                     stdin=step / TRIGGERS_CHECK_FILE)  # fmt: skip
        assert ran.returncode == 0, ran.stderr.decode()
        (step / TRIGGERS_CHECK_OUTPUT).write_bytes(ran.stdout)
        return read_triggers_check(step / TRIGGERS_CHECK_OUTPUT, contract, store, job_id="job1",
                                  database_name="shop", contract_sha=SHA,
                                  target_database="cut_new")  # fmt: skip

    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "shop.orders_bi": "missing", "shop.nightly": "missing", "shop.paused": "missing",
        "shop.replica": "missing"}  # fmt: skip
    ran = _mysql(client, sock, None, stdin=step / TRIGGERS_FILE)
    assert ran.returncode == 0, ran.stderr.decode()
    result = check()
    assert (result.status, result.confirmed, result.mismatches) == ("pass", 4, [])
    found = _mysql(client, sock, "SELECT EVENT_NAME, STATUS, DEFINER FROM information_schema.EVENTS "
                   "WHERE EVENT_SCHEMA = 'cut_new' ORDER BY 1", "--batch", "--skip-column-names")  # fmt: skip
    events = [line.split("\t") for line in found.stdout.decode().splitlines()]
    assert [(n, s) for n, s, _ in events[:2]] == [("nightly", "ENABLED"), ("paused", "DISABLED")]
    assert events[2][0] == "replica" and events[2][1].endswith("SIDE_DISABLED")
    assert {definer for *_, definer in events} == {"admin@%"}
    mode = _mysql(client, sock, "SELECT SQL_MODE FROM information_schema.TRIGGERS WHERE "
                  "TRIGGER_NAME = 'orders_bi'", "--batch", "--skip-column-names")  # fmt: skip
    assert mode.stdout.decode().strip() == "NO_ENGINE_SUBSTITUTION"
    _mysql(client, sock, "INSERT INTO cut_new.orders (id) VALUES (1)")
    note = _mysql(client, sock, "SELECT note FROM cut_new.orders", "--batch", "--skip-column-names")
    assert note.stdout.decode().strip() == SENTINEL  # the trigger fires
    assert SENTINEL not in (step / TRIGGERS_CHECK_OUTPUT).read_text()
    _mysql(client, sock, "ALTER EVENT cut_new.nightly DISABLE")
    assert {m.routine_id: m.problem for m in check().mismatches} == {
        "shop.nightly": "event_status_differs"}  # fmt: skip
    # An enabled event with the scheduler off never runs: the check fails, naming it.
    _mysql(client, sock, "ALTER EVENT cut_new.nightly ENABLE; SET GLOBAL event_scheduler = OFF")
    try:
        off = check()
        assert (off.status, off.mismatches, off.event_scheduler) == ("fail", [], "OFF")
    finally:
        _mysql(client, sock, "SET GLOBAL event_scheduler = ON")
    assert check().status == "pass"


def test_a_triggers_enable_run_that_stopped_part_way_resumes_in_firing_order(
    mysql_socket, tmp_path
):
    from src.tools.data_movement.triggers_check import parse_triggers_output
    from src.tools.data_movement.triggers_scripts import EVENT_SCHEDULER, RESUME_FILE

    client, sock = mysql_socket
    # orders has no note column yet: a_bi's CREATE fails on it, after b_bi was created.
    _mysql(client, sock, "CREATE DATABASE cut_two; CREATE TABLE cut_two.orders (id int PRIMARY KEY);"
           "CREATE USER IF NOT EXISTS 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';")  # fmt: skip
    rows = [
        row("shop.a_bi", "trigger", schema="shop", table="shop.orders", status="rewritten",
            specific="orders.a_bi"),
        row("shop.b_bi", "trigger", schema="shop", table="shop.orders", status="rewritten",
            specific="orders.b_bi"),
        row("shop.later", "event", schema="shop", status="rewritten", specific="later"),
    ]  # fmt: skip
    trigger = ("CREATE DEFINER=`app`@`%` TRIGGER `{name}` BEFORE INSERT ON `orders` FOR EACH ROW "
               "SET NEW.{column} = NEW.{column}")  # fmt: skip
    sources = [
        mysql_source("orders.a_bi", trigger.format(name="a_bi", column="note"), kind="trigger"),
        mysql_source("orders.b_bi", trigger.format(name="b_bi", column="id"), kind="trigger"),
        mysql_source("later", "CREATE DEFINER=`app`@`%` EVENT `later` ON SCHEDULE EVERY 1 DAY "
                     "STARTS '2036-01-01 00:00:00' DISABLE DO SELECT 1", kind="event"),
    ]  # fmt: skip
    artifact = migration(rows, "mysql")
    store = _job(tmp_path, artifact, sources, "mysql")
    # On the source b_bi fires first (ACTION_ORDER 1), then a_bi.
    contract = contract_with_routines(artifact, triggers={
        "shop.a_bi": {"fires": "BEFORE INSERT", "action_order": 2},
        "shop.b_bi": {"fires": "BEFORE INSERT", "action_order": 1}})  # fmt: skip
    step = tmp_path / "step"
    write_triggers_scripts(store, "shop", "job1", contract, SHA, step, target_database="cut_two")

    def check():
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names",
                     stdin=step / TRIGGERS_CHECK_FILE)  # fmt: skip
        assert ran.returncode == 0, ran.stderr.decode()
        (step / TRIGGERS_CHECK_OUTPUT).write_bytes(ran.stdout)
        return read_triggers_check(step / TRIGGERS_CHECK_OUTPUT, contract, store, job_id="job1",
                                  database_name="shop", contract_sha=SHA,
                                  target_database="cut_two")  # fmt: skip

    stopped = _mysql(client, sock, None, stdin=step / TRIGGERS_FILE)
    assert stopped.returncode != 0 and b"note" in stopped.stderr
    assert {m.routine_id for m in check().mismatches} == {"shop.a_bi", "shop.later"}
    # Running the whole script again stops at once, on the trigger it already created.
    again = _mysql(client, sock, None, stdin=step / TRIGGERS_FILE)
    assert again.returncode != 0 and b"already exists" in again.stderr
    # The customer fixes the cause, runs the check, and resumes from what it shows missing.
    _mysql(client, sock, "ALTER TABLE cut_two.orders ADD COLUMN note varchar(64)")
    check()
    parsed = parse_triggers_output(step / TRIGGERS_CHECK_OUTPUT, contract, SHA, "cut_two")
    present = {k for k in parsed if k != (EVENT_SCHEDULER,)}
    resumed = write_triggers_scripts(store, "shop", "job1", contract, SHA, step,
                                    target_database="cut_two", present=present)  # fmt: skip
    assert (resumed.in_script, resumed.already_there) == (2, 1)
    ran = _mysql(client, sock, None, stdin=step / RESUME_FILE)
    assert ran.returncode == 0, ran.stderr.decode()
    assert check().status == "pass"
    order = _mysql(client, sock, "SELECT TRIGGER_NAME, ACTION_ORDER FROM information_schema.TRIGGERS "
                   "WHERE TRIGGER_SCHEMA = 'cut_two' ORDER BY ACTION_ORDER", "--batch",
                   "--skip-column-names")  # fmt: skip
    assert order.stdout.decode().split() == ["b_bi", "1", "a_bi", "2"]  # as on the source


def test_the_triggers_enable_script_creates_the_database_of_an_event_nothing_else_creates(
    mysql_socket, tmp_path
):
    client, sock = mysql_socket
    _mysql(client, sock, "CREATE USER IF NOT EXISTS 'admin'@'%'; GRANT ALL ON *.* TO 'admin'@'%';"
           "DROP DATABASE IF EXISTS jobs; DROP DATABASE IF EXISTS shop_functions;")  # fmt: skip
    # `jobs` holds only an event; `shop_functions` holds a carried routine (the deploy creates
    # it) and an event (the triggers-enable script finds it there already).
    rows = [
        row("shop_functions.tally", "procedure", schema="shop_functions", status="rewritten",
            signature="", specific="tally"),
        row("shop_functions.sweep", "event", schema="shop_functions", status="rewritten",
            specific="sweep"),
        row("jobs.nightly", "event", schema="jobs", status="rewritten", specific="nightly"),
    ]  # fmt: skip
    event = ("CREATE DEFINER=`app`@`%` EVENT `{name}` ON SCHEDULE EVERY 1 DAY STARTS "
             "'2036-01-01 00:00:00' DISABLE DO SELECT 1")  # fmt: skip
    sources = [
        mysql_source("tally", "CREATE DEFINER=`app`@`%` PROCEDURE `tally`()\nBEGIN SELECT 1; END",
                     schema="shop_functions"),
        mysql_source("sweep", event.format(name="sweep"), kind="event", schema="shop_functions"),
        mysql_source("nightly", event.format(name="nightly"), kind="event", schema="jobs"),
    ]  # fmt: skip
    artifact = migration(rows, "mysql")
    store = _job(tmp_path, artifact, sources, "mysql")
    contract = contract_with_routines(artifact)
    _write(tmp_path, store, contract)
    step = tmp_path / "step"
    try:
        deployed = _mysql(client, sock, None, stdin=step / DEPLOY_FILE)
        assert deployed.returncode == 0, deployed.stderr.decode()
        scripts = write_triggers_scripts(store, "shop", "job1", contract, SHA, step)
        assert (scripts.expected, scripts.in_script) == (2, 2)
        ran = _mysql(client, sock, None, stdin=step / TRIGGERS_FILE)
        assert ran.returncode == 0, ran.stderr.decode()
        found = _mysql(client, sock, "SELECT EVENT_SCHEMA, EVENT_NAME FROM information_schema.EVENTS "
                       "WHERE EVENT_SCHEMA IN ('jobs', 'shop_functions') ORDER BY 1",
                       "--batch", "--skip-column-names")  # fmt: skip
        assert found.stdout.decode().split() == ["jobs", "nightly", "shop_functions", "sweep"]
        again = _mysql(client, sock, None, "--batch", "--skip-column-names",
                       stdin=step / TRIGGERS_CHECK_FILE)  # fmt: skip
        assert again.returncode == 0, again.stderr.decode()
        (step / TRIGGERS_CHECK_OUTPUT).write_bytes(again.stdout)
        result = read_triggers_check(step / TRIGGERS_CHECK_OUTPUT, contract, store, job_id="job1",
                                    database_name="shop", contract_sha=SHA,
                                    target_database=None)  # fmt: skip
        assert (result.status, result.confirmed, result.mismatches) == ("pass", 2, [])
    finally:
        _mysql(
            client, sock, "DROP DATABASE IF EXISTS jobs; DROP DATABASE IF EXISTS shop_functions;"
        )
