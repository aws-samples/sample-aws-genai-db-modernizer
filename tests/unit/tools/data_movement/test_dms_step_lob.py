# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's LOB mode per source (#1918): a PostgreSQL source never runs full LOB mode.

From a PostgreSQL source into Aurora PostgreSQL, full LOB mode wrote a NULL LOB as an empty
value in a live run: a unique index over a nullable text column refused the second row, and an
array column refused the empty value. MySQL rehearsals passed in full LOB mode, so it keeps it.
"""

from __future__ import annotations

import pytest

from src.contracts.transformation_contract import Finding
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    bundle_inputs,
    render_ddl,
    render_dms_template,
    task_settings,
)

from .dms_step_fixtures import make_bundle, make_step1_inputs


def _lob(target: dict) -> tuple[bool, bool, int]:
    t = target["TargetMetadata"]
    return t["FullLobMode"], t["LimitedSizeLobMode"], t["LobMaxSize"]


def _source_settings(engine: str) -> dict:
    props = render_dms_template(make_step1_inputs(engine))["Resources"]["SourceEndpoint"][
        "Properties"
    ]
    settings: dict = props[
        "PostgreSqlSettings" if engine == "aurora_postgresql" else "MySqlSettings"
    ]
    return settings


def test_a_postgresql_source_runs_limited_lob_mode_even_with_a_full_table_in_the_plan():
    # The fixture plan has one full LOB table, as every plan written before #1918 has for a
    # PostgreSQL text column: re-rendering it must not bring full LOB mode back.
    inputs = make_step1_inputs("aurora_postgresql")
    assert any(t.mode == "full" for t in inputs.plan.replication.lob_tables)
    assert _lob(task_settings(inputs)) == (False, True, dms_sizing().lob.undeclared_max_kb)


def test_a_postgresql_source_keeps_a_larger_plan_size():
    b = make_bundle("aurora_postgresql")
    big = dms_sizing().lob.undeclared_max_kb * 2
    plan = b.plan.model_copy(update={"replication": b.plan.replication.model_copy(
        update={"lob_max_size_kb": big})})  # fmt: skip
    inputs = bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))
    assert _lob(task_settings(inputs)) == (False, True, big)


@pytest.mark.passes_on_main  # MySQL keeps full LOB mode: its rehearsals passed with it
def test_a_mysql_source_keeps_full_lob_mode_and_no_truncation_setting():
    assert _lob(task_settings(make_step1_inputs("aurora_mysql"))) == (True, False, 64)
    assert "FailTasksOnLobTruncation" not in _source_settings("aurora_mysql")


def test_a_postgresql_source_endpoint_sets_fail_tasks_on_lob_truncation():
    assert _source_settings("aurora_postgresql")["FailTasksOnLobTruncation"] is True
    target = render_dms_template(make_step1_inputs("aurora_postgresql"))["Resources"][
        "TargetEndpoint"
    ]
    assert "FailTasksOnLobTruncation" not in target["Properties"]["PostgreSqlSettings"]


def test_the_postgresql_ddl_header_says_the_lob_size_is_not_measured_and_how_to_check_it():
    ddl = " ".join(
        line.removeprefix("-- ")
        for line in render_ddl(make_step1_inputs("aurora_postgresql")).splitlines()
    )
    size = dms_sizing().lob.undeclared_max_kb
    assert f"limited LOB mode at {size} KB (LobMaxSize) on " in ddl
    # A live full load truncated a larger value despite FailTasksOnLobTruncation (#1918): the
    # header says so and points at DMS validation, never that the value is not truncated.
    assert "not a measured one" in ddl and "A larger value is truncated" in ddl
    assert "FailTasksOnLobTruncation" in ddl and "never truncated" not in ddl
    assert "SELECT max(octet_length(<column>::text)) FROM <table>" in ddl
    assert "The tables: public.customers, public.orders." in ddl


@pytest.mark.passes_on_main  # the MySQL DDL is unchanged
def test_the_mysql_ddl_header_has_no_lob_paragraph():
    assert "LOB columns:" not in render_ddl(make_step1_inputs("aurora_mysql"))


def test_a_blocked_lob_table_is_not_listed_in_the_ddl_header():
    b = make_bundle("aurora_postgresql")
    error = Finding(
        code="unknown_table", severity="error", message="customers is not in the collection"
    )
    rules = [r.model_copy(update={"status": "blocked", "findings": [error]}) if r.target_table == "customers"
             else r for r in b.contract.rules]  # fmt: skip
    contract = b.contract.model_copy(
        update={"rules": rules, "blocked_rule_count": 1, "status": "has_blocked_rules"}
    )
    plan = b.plan.model_copy(update={"contract": None})
    inputs = bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl=b.ddl))
    ddl = render_ddl(inputs)
    flat = " ".join(line.removeprefix("-- ") for line in ddl.splitlines())
    assert "LOB columns: 1 moved table have" in flat and "The tables: public.orders." in flat
    # The blocked full table no longer raises the size: the orders table's own 64 KB applies.
    assert _lob(task_settings(inputs)) == (False, True, 64)


def test_a_long_lob_table_list_names_ten_and_counts_the_rest():
    from src.tools.data_movement.dms_step import _first_tables

    names = [f"public.t{i:02d}" for i in range(12)]
    text = _first_tables(names)
    assert text.startswith("public.t00, public.t01") and "public.t09" in text
    assert "public.t10" not in text and text.endswith(
        "and 2 more (the plan's replication.lob_tables)"
    )
    assert _first_tables(names[:10]) == ", ".join(names[:10])
