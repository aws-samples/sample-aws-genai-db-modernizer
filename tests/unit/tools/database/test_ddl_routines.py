# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DDL uploads carry procedure and trigger bodies (#1652): the zero-load path, bodies
and no execution counts."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.agents.collector import (
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.agents.collector.mysql_collector import NoopCheckpointStore
from src.contracts.collector_input import CollectorInput
from src.tools.database.ddl_parser import parse_ddl_routines

TSQL = """
CREATE TABLE dbo.orders (id INT PRIMARY KEY);
GO
CREATE OR ALTER PROCEDURE dbo.usp_place @id INT AS
BEGIN
  UPDATE dbo.orders SET note = 'jane@example.com' WHERE id = @id;
END
GO
CREATE TRIGGER dbo.tr_orders ON dbo.orders AFTER INSERT, UPDATE AS
BEGIN
  INSERT INTO dbo.audit (id) SELECT id FROM inserted;
END
GO
"""

PLPGSQL = """
CREATE TABLE public.orders (id int PRIMARY KEY);
CREATE FUNCTION public.log_order() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO audit (id, note) VALUES (NEW.id, 'jane@example.com; not the end');
  RETURN NEW;
END;
$$;
CREATE TRIGGER tg AFTER INSERT OR UPDATE ON public.orders
  FOR EACH ROW EXECUTE FUNCTION public.log_order();
"""

MYSQL = """
CREATE TABLE orders (id INT PRIMARY KEY);
DELIMITER //
CREATE DEFINER=`app`@`%` PROCEDURE place_order(IN p_id INT)
BEGIN
  UPDATE orders SET note = 'jane@example.com' WHERE id = p_id;
END //
CREATE TRIGGER tg_audit AFTER INSERT ON orders FOR EACH ROW
BEGIN
  INSERT INTO audit (id) VALUES (NEW.id);
END //
DELIMITER ;
"""

ORACLE = """
CREATE TABLE app.orders (id NUMBER PRIMARY KEY);
CREATE OR REPLACE PROCEDURE app.place_order(p_id NUMBER) IS
BEGIN
  UPDATE orders SET note = 'jane@example.com' WHERE id = p_id;
END;
/
CREATE OR REPLACE TRIGGER app.tg BEFORE INSERT OR UPDATE ON app.orders FOR EACH ROW
BEGIN
  :NEW.id := :NEW.id;
END;
/
"""


def test_tsql_batches():
    out = parse_ddl_routines(TSQL, "sqlserver")
    (p,) = out["procedures"]
    assert (p["schema_name"], p["routine_name"], p["routine_type"]) == (
        "dbo",
        "usp_place",
        "PROCEDURE",
    )
    assert "UPDATE dbo.orders" in p["definition"]
    (t,) = out["triggers"]
    assert (t["table_name"], t["timing"], t["events"], t["for_each"]) == (
        "orders", "AFTER", "INSERT OR UPDATE", "STATEMENT",
    )  # fmt: skip
    assert out["routine_stats_status"]["status"] == "offline"


def test_plpgsql_dollar_quoted_body_and_trigger():
    out = parse_ddl_routines(PLPGSQL, "postgresql")
    (p,) = out["procedures"]
    assert (p["routine_name"], p["routine_type"]) == ("log_order", "FUNCTION")
    assert "RETURN NEW" in p["definition"]
    (t,) = out["triggers"]
    assert (t["table_name"], t["schema_name"], t["events"], t["for_each"]) == (
        "orders", "public", "INSERT OR UPDATE", "ROW",
    )  # fmt: skip
    assert "EXECUTE FUNCTION public.log_order()" in t["definition"]


def test_mysql_delimiter_blocks():
    out = parse_ddl_routines(MYSQL, "mysql")
    assert [p["routine_name"] for p in out["procedures"]] == ["place_order"]
    assert out["procedures"][0]["definition"].rstrip().endswith("END")
    (t,) = out["triggers"]
    assert (t["trigger_name"], t["timing"], t["events"], t["for_each"]) == (
        "tg_audit", "AFTER", "INSERT", "ROW",
    )  # fmt: skip


def test_oracle_slash_terminated_blocks():
    out = parse_ddl_routines(ORACLE, "oracle")
    assert [p["routine_name"] for p in out["procedures"]] == ["place_order"]
    (t,) = out["triggers"]
    assert (t["timing"], t["events"], t["for_each"]) == ("BEFORE", "INSERT OR UPDATE", "ROW")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql", "sqlserver", "oracle"])
def test_a_ddl_file_with_only_tables_gives_nothing(dialect):
    out = parse_ddl_routines("CREATE TABLE t (id INT);", dialect)
    assert out["procedures"] == [] and out["triggers"] == []
    assert parse_ddl_routines("", dialect)["procedures"] == []


def test_a_trigger_whose_head_cannot_be_read_is_left_out():
    out = parse_ddl_routines("CREATE TRIGGER t\n/\n", "oracle")
    assert out["triggers"] == []


_DDL = {
    "mysql": (mysql_collector, MYSQL),
    "postgresql": (postgres_collector, PLPGSQL),
    "sqlserver": (sqlserver_collector, TSQL),
    "oracle": (oracle_collector, ORACLE),
}


@pytest.mark.parametrize("engine", list(_DDL))
def test_a_ddl_upload_brings_its_routines_to_the_contract(engine):
    module, text = _DDL[engine]
    inp = CollectorInput.model_validate(
        {"job_id": "j1", "engine": engine, "cluster_endpoint": "db.example.internal",
         "port": 1, "database_name": "shop", "mode": "ddl",
         "ddl_config": {"s3_bucket": "b", "s3_key": "schema.sql"}}
    )  # fmt: skip
    with (
        patch("src.agents.collector.mysql_collector.fetch_ddl_from_s3", return_value=text),
        patch.object(module, "_collect_aws_raw", return_value={}),
    ):
        out = module._collect_ddl(inp, NoopCheckpointStore())
    assert [p.procedure_name for p in out.database_schema.procedures or []] == (
        ["log_order"]
        if engine == "postgresql"
        else ["place_order" if engine != "sqlserver" else "usp_place"]
    )
    assert len(out.database_schema.triggers or []) == 1
    assert out.metadata.routine_stats_source.status.value == "offline"
    assert "jane@example.com" not in json.dumps(out.model_dump(mode="json"))


def test_a_ddl_upload_that_cannot_be_read_costs_only_its_routines():
    inp = CollectorInput.model_validate(
        {"job_id": "j1", "engine": "mysql", "cluster_endpoint": "db.example.internal",
         "port": 3306, "database_name": "shop", "mode": "ddl",
         "ddl_config": {"s3_bucket": "b", "s3_key": "schema.sql"}}
    )  # fmt: skip
    with patch(
        "src.agents.collector.mysql_collector.fetch_ddl_from_s3", side_effect=RuntimeError("gone")
    ):
        raw = mysql_collector._parse_ddl_routines_raw(inp, "us-east-1")
    assert raw == {"procedures": [], "triggers": [], "routine_stats_status": None}
