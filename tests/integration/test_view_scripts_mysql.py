# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's views on a real MySQL (#2082): collected, then created again as the customer
runs the scripts.

Needs ``mysqld`` and the ``mysql`` client on PATH (the throwaway Unix-socket server of
``test_routine_scripts_mysql.py``: no TCP listener, no binary log, its data folder removed
when the module ends). ``collect-mysql.sql`` and the live collector read the same view
fields; the collection goes through the offline ingest (lineage included) into the step
contract's views section; the views are dropped, ``mysql < views-deploy.sql`` runs twice and
``mysql --batch --skip-column-names < views-check.sql > views-check.tsv`` once, as
``views.md`` says. Run it with ``uv run pytest -m integration
tests/integration/test_view_scripts_mysql.py``. The PostgreSQL twin is hermetic (pgserver):
``tests/unit/tools/data_movement/test_view_scripts_engine.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.routine_sources import iter_collection_sources
from src.agents.collector.view_sources import view_create, view_fields
from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.data_movement.step1_views import build_step_views
from src.tools.data_movement.view_check import read_view_check
from src.tools.data_movement.view_scripts import CHECK_FILE, CHECK_OUTPUT, DEPLOY_FILE
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_collection_check import read_collection_text
from src.tools.database.offline_parser import parse_offline_collection
from tests.integration.test_routine_scripts_mysql import _mysql, mysql_socket  # noqa: F401
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract
from tests.unit.tools.data_movement.view_fixtures import SENTINEL, SHA, job, write

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-mysql.sql"
SETUP = f"""
    DROP DATABASE IF EXISTS shop;
    CREATE DATABASE shop CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    CREATE TABLE shop.orders (id bigint PRIMARY KEY, note varchar(40));
    CREATE TABLE shop.customers (id bigint PRIMARY KEY, bio text);
    CREATE TABLE shop.audit_log (msg varchar(200));
    CREATE SQL SECURITY INVOKER VIEW shop.base AS
      SELECT id, note FROM shop.orders WHERE note <> '{SENTINEL}; void';
    CREATE VIEW shop.top (order_id, note) AS SELECT b.id, b.note FROM shop.base b
      WITH CASCADED CHECK OPTION;
    CREATE VIEW shop.`we``ird` AS SELECT count(*) AS n FROM shop.customers;
"""  # fmt: skip
DROP = "DROP VIEW shop.top; DROP VIEW shop.base; DROP VIEW shop.`we``ird`;"


@pytest.fixture(scope="module")
def shop(mysql_socket):  # noqa: F811
    client, sock = mysql_socket
    done = _mysql(client, sock, SETUP)
    assert done.returncode == 0, done.stderr.decode()
    yield client, sock
    _mysql(client, sock, "DROP DATABASE IF EXISTS shop;")


class _Live(MySQLRemoteCollector):
    def __init__(self, client, sock):  # the client on the socket, as SSM runs it
        self.client, self.sock = client, sock

    def _query(self, sql: str) -> list[dict]:
        done = _mysql(self.client, self.sock, sql, "-B", "-D", "shop")
        assert done.returncode == 0, done.stderr.decode()
        return _parse_tabular_output(done.stdout.decode(), "mysql")


def _collect(client, sock) -> tuple[dict, dict]:
    done = _mysql(client, sock, None, "-N", "--raw", "-D", "shop",
                  "--init-command=SET @rate_sample_seconds=0", stdin=SCRIPT)  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()
    raw = read_collection_text(done.stdout.decode(), source="collection.json")
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job1", database_name="shop", s3_key="k"
    )
    return raw, output


def test_the_script_and_the_live_collector_read_the_same_views(shop):
    raw, output = _collect(*shop)
    script_rows = {r["view_name"]: r for r in raw["views"]}
    live_rows = {r["view_name"]: r for r in _Live(*shop).collect_views()}
    assert sorted(script_rows) == sorted(live_rows) == ["base", "top", "we`ird"]
    for name, row in script_rows.items():
        assert view_create("mysql", live_rows[name]) == view_create("mysql", row) is not None
        assert view_fields(live_rows[name]) == view_fields(row)
    top = view_create("mysql", script_rows["top"])
    assert top.startswith(
        "CREATE OR REPLACE SQL SECURITY DEFINER VIEW `shop`.`top` (`order_id`, `note`) AS"
    )
    assert top.endswith("WITH CASCADED CHECK OPTION;")
    assert "SQL SECURITY INVOKER" in view_create("mysql", script_rows["base"])
    views = {v["view_id"]: v for v in output["database_schema"]["views"]}
    assert {v: views[v]["body_status"] for v in views} == {"base": "collected", "top": "collected",
                                                         "we`ird": "collected"}  # fmt: skip
    assert SENTINEL not in str(output)  # the collector output's definitions stay redacted


def test_the_scripts_deploy_twice_and_confirm_on_mysql_then_name_each_drift(shop, tmp_path):
    client, sock = shop
    raw, output = _collect(client, sock)
    contract = make_contract("aurora_mysql")
    section = build_step_views(output["database_schema"], contract, source_engine="mysql")
    assert [(v.view_id, v.carry) for v in section.views] == [
        ("base", "carried"), ("top", "carried"), ("we`ird", "carried")]  # fmt: skip
    contract = contract.model_copy(update={"views_section": section})
    store = job(tmp_path, list(iter_collection_sources(raw)), "mysql")
    assert write(tmp_path, store, contract).in_script == 3
    step = tmp_path / "step"
    assert _mysql(client, sock, DROP).returncode == 0
    for _ in range(2):  # the second run replaces each view with itself
        deployed = _mysql(client, sock, None, stdin=step / DEPLOY_FILE)
        assert deployed.returncode == 0, deployed.stderr.decode()

    def check():
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names", stdin=step / CHECK_FILE)
        assert ran.returncode == 0, ran.stderr.decode()
        (step / CHECK_OUTPUT).write_bytes(ran.stdout)
        return read_view_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                               contract_sha=SHA, target_database=None)  # fmt: skip

    result = check()
    assert (result.status, result.confirmed, result.mismatches) == ("pass", 3, [])
    kept = _mysql(client, sock, "SELECT TABLE_NAME, SECURITY_TYPE, CHECK_OPTION, DEFINER FROM "
                  "information_schema.VIEWS WHERE TABLE_SCHEMA = 'shop' ORDER BY 1",
                  "--batch", "--skip-column-names").stdout.decode().splitlines()  # fmt: skip
    assert [ln.split("\t")[:3] for ln in kept] == [
        ["base", "INVOKER", "NONE"], ["top", "DEFINER", "CASCADED"], ["we`ird", "DEFINER", "NONE"]]  # fmt: skip
    assert {ln.split("\t")[3] for ln in kept} == {"root@localhost"}  # the user who ran it
    assert SENTINEL not in (step / CHECK_OUTPUT).read_text()

    _mysql(
        client, sock, "DROP VIEW shop.top; CREATE VIEW shop.top AS SELECT note, id FROM shop.base;"
    )
    assert {m.view_id: (m.problem, m.columns) for m in check().mismatches} == {
        "top": ("columns_differ", ["order_id", "note"])}  # fmt: skip
    _mysql(
        client,
        sock,
        "DROP VIEW shop.top; CREATE TABLE shop.top (order_id bigint, note varchar(40));",
    )
    assert {m.view_id: m.problem for m in check().mismatches} == {"top": "not_a_view"}
    _mysql(client, sock, "DROP TABLE shop.top;")
    assert {m.view_id: m.problem for m in check().mismatches} == {"top": "missing"}
