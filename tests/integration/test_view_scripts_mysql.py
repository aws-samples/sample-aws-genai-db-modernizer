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
        "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`top` (`order_id`, `note`) AS"
    )
    assert view_fields(script_rows["top"])["security_type"] == "DEFINER"
    assert top.endswith("WITH CASCADED CHECK OPTION;")
    assert "SQL SECURITY INVOKER" in view_create("mysql", script_rows["base"])
    views = {v["view_id"]: v for v in output["database_schema"]["views"]}
    assert {v: views[v]["body_status"] for v in views} == {
        "shop.base": "collected", "shop.top": "collected", "shop.we`ird": "collected"}  # fmt: skip
    assert SENTINEL not in str(output)  # the collector output's definitions stay redacted


def test_the_scripts_deploy_twice_and_confirm_on_mysql_then_name_each_drift(shop, tmp_path):
    client, sock = shop
    raw, output = _collect(client, sock)
    contract = make_contract("aurora_mysql")
    section = build_step_views(output["database_schema"], contract, source_engine="mysql")
    assert [(v.view_id, v.carry) for v in section.views] == [
        ("shop.base", "carried"), ("shop.top", "carried"), ("shop.we`ird", "carried")]  # fmt: skip
    contract = contract.model_copy(update={"views_section": section})
    store = job(tmp_path, list(iter_collection_sources(raw)), "mysql")
    scripts = write(tmp_path, store, contract)
    assert scripts.in_script == 3
    # The source's DEFINER views are named: they read with their readers' rights here.
    assert scripts.definer_views == ["shop.top", "shop.we`ird"]
    assert "`shop.top`" in (tmp_path / "step" / "views.md").read_text()
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
        ["base", "INVOKER", "NONE"], ["top", "INVOKER", "CASCADED"], ["we`ird", "INVOKER", "NONE"]]  # fmt: skip
    assert {ln.split("\t")[3] for ln in kept} == {"root@localhost"}  # the user who ran it
    assert SENTINEL not in (step / CHECK_OUTPUT).read_text()

    _mysql(
        client, sock, "DROP VIEW shop.top; CREATE VIEW shop.top AS SELECT note, id FROM shop.base;"
    )
    assert {m.view_id: (m.problem, m.columns) for m in check().mismatches} == {
        "shop.top": ("columns_differ", ["order_id", "note"])}  # fmt: skip
    _mysql(
        client,
        sock,
        "DROP VIEW shop.top; CREATE TABLE shop.top (order_id bigint, note varchar(40));",
    )
    assert {m.view_id: m.problem for m in check().mismatches} == {"shop.top": "not_a_view"}
    _mysql(client, sock, "DROP TABLE shop.top;")
    assert {m.view_id: m.problem for m in check().mismatches} == {"shop.top": "missing"}
    _mysql(client, sock, "CREATE VIEW shop.top (order_id, note) AS SELECT b.id, b.note FROM "
           "shop.base b WITH CASCADED CHECK OPTION;")  # fmt: skip


def test_with_another_target_database_the_check_looks_where_the_customer_creates_the_view(
    shop, tmp_path
):
    """A view refused for another target database is created there by hand; the check then
    looks for it in the target database, not the source's (#2333 review)."""
    client, sock = shop
    raw, output = _collect(client, sock)
    contract = make_contract("aurora_mysql")
    section = build_step_views(output["database_schema"], contract, source_engine="mysql")
    contract = contract.model_copy(update={"views_section": section})
    store = job(tmp_path, list(iter_collection_sources(raw)), "mysql")
    scripts = write(tmp_path, store, contract, target_database="shop_tgt")
    assert {c for _, c in scripts.refused} == {"target_database_differs"}
    step = tmp_path / "step"
    assert "('shop_tgt', 'base')" in (step / CHECK_FILE).read_text()
    done = _mysql(client, sock, "CREATE DATABASE shop_tgt; CREATE TABLE shop_tgt.orders (id bigint "
                  "PRIMARY KEY, note varchar(40)); CREATE TABLE shop_tgt.customers (id bigint PRIMARY "
                  "KEY, bio text); CREATE VIEW shop_tgt.base AS SELECT id, note FROM shop_tgt.orders;"
                  "CREATE VIEW shop_tgt.top (order_id, note) AS SELECT b.id, b.note FROM shop_tgt.base b;"
                  "CREATE VIEW shop_tgt.`we``ird` AS SELECT count(*) AS n FROM shop_tgt.customers;")  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()
    try:
        ran = _mysql(client, sock, None, "--batch", "--skip-column-names", stdin=step / CHECK_FILE)
        assert ran.returncode == 0, ran.stderr.decode()
        (step / CHECK_OUTPUT).write_bytes(ran.stdout)
        result = read_view_check(step / CHECK_OUTPUT, contract, job_id="job1", database_name="shop",
                                 contract_sha=SHA, target_database="shop_tgt")  # fmt: skip
        assert (result.status, result.confirmed, result.mismatches) == ("pass", 3, [])
    finally:
        _mysql(client, sock, "DROP DATABASE shop_tgt;")


@pytest.mark.parametrize(
    "definition,code",
    __import__("tests.unit.tools.sql.test_view_statement", fromlist=["MY_HOSTILE"]).MY_HOSTILE,
)
def test_a_hostile_definition_never_runs_when_the_customer_runs_the_script(
    shop, tmp_path, definition, code
):
    """An edited collection file's view text (ADR-034 Decision 5): refused by code, and the
    script the customer runs drops nothing and runs no client command."""
    from tests.unit.tools.data_movement.view_fixtures import (
        contract_with_views,
        mysql_view_source,
        view,
    )

    client, sock = shop
    marker = Path("/tmp/v2082-pwned")
    marker.unlink(missing_ok=True)
    hostile = (
        "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`evil` (`id`) AS\n" + definition + ";"
    )
    good = "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`fine` (`id`) AS\nselect 1 AS `id`;"
    contract = contract_with_views([view("evil", engine="aurora_mysql"),
                                    view("fine", engine="aurora_mysql")], "aurora_mysql")  # fmt: skip
    store = job(tmp_path, [mysql_view_source("evil", hostile), mysql_view_source("fine", good)],
                "mysql")  # fmt: skip
    scripts = write(tmp_path, store, contract)
    assert scripts.refused == [("evil", code)] and scripts.in_script == 1
    ran = _mysql(client, sock, None, stdin=tmp_path / "step" / DEPLOY_FILE)
    assert ran.returncode == 0, ran.stderr.decode()
    tables = _mysql(client, sock, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE "
                    "TABLE_SCHEMA = 'shop' ORDER BY 1", "--batch", "--skip-column-names")  # fmt: skip
    names = tables.stdout.decode().split()
    assert {"audit_log", "orders", "customers", "fine"} <= set(names) and "evil" not in names
    assert not marker.exists()
    _mysql(client, sock, "DROP VIEW IF EXISTS shop.fine;")
