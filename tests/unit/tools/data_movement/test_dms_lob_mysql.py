# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 never runs a MySQL source in full LOB mode (#2123).

On a WordPress rehearsal (MySQL 8.0 into Aurora MySQL 3), the full load stopped with
``Update LOB failed ... Column 'post_content_filtered' cannot be null``: in full LOB mode DMS
writes each row first, without its LOB values, and fills them in with a second statement, so
the target's ``NOT NULL`` LONGTEXT columns refused the first insert. The plan had full LOB mode
because every LONGTEXT declares 4 GB, above the catalog's 1 MB limit. A MySQL source now gets
what a PostgreSQL source got in #1918: limited LOB mode at the task level, sized from the
collection's measured values, a ``table-settings`` rule with ``lob-settings`` for a table that
needs more, and, when the values were not measured, the catalog's limited size with a finding.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.dms_sizing import (
    MEASURED,
    lob_plan,
    table_lob_limits,
    unmeasured_lob_tables,
)
from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    bundle_inputs,
    render_ddl,
    render_dms_template,
    table_mappings,
    task_settings,
)
from src.tools.data_movement.plan import build_step_plans

from .dms_step_fixtures import make_bundle, make_step1_inputs
from .test_plan import _job

DEFAULT = dms_sizing().lob.undeclared_max_kb
# How the plan tells the customer to measure a MySQL LOB column (the DMS User Guide's query).
MYSQL_MEASURE = "SELECT max(length(<column>)) FROM <table>"
LONGTEXT = 4_294_967_295  # information_schema's CHARACTER_MAXIMUM_LENGTH of a LONGTEXT


def _c(name, data_type, measured=None, max_length=LONGTEXT, nullable="NO"):
    out = {"column_name": name, "data_type": data_type, "max_length": max_length,
           "is_nullable": nullable}  # fmt: skip
    if measured is not None:
        out["max_value_bytes"] = measured
    return out


def _collector(*tables, engine="mysql"):
    return {
        "metadata": {"source_database": {"engine": engine, "version": "8.0.45", "database_size_gb": 0.3}},
        "database_schema": {"tables": [
            {"table_id": t, "table_name": t.split(".")[1], "primary_key": ["ID"],
             "columns": [{"column_name": "ID", "data_type": "bigint"}, *cols]}
            for t, cols in tables]},
    }  # fmt: skip


def _wp_posts(content=None, filtered=None):
    return ("wordpress.wp_posts", [_c("post_content", "longtext", content),
                                   _c("post_content_filtered", "longtext", filtered)])  # fmt: skip


def _wp_term_taxonomy(measured=None):
    return ("wordpress.wp_term_taxonomy", [_c("description", "longtext", measured)])


def _lob(target: dict) -> tuple[bool, bool, int]:
    t = target["TargetMetadata"]
    return t["FullLobMode"], t["LimitedSizeLobMode"], t["LobMaxSize"]


# --- the plan's LOB modes -------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_measured_not_null_longtext_columns_run_limited_at_their_measured_sizes(dialect):
    # 3,000,000 bytes at 2 bytes per byte, 1.25 times headroom: 7325 KB, so 7360 KB in 64 KB steps.
    collector = _collector(_wp_posts(3_000_000, 0), _wp_term_taxonomy(120), engine=dialect)
    size, tables = lob_plan(
        collector, ["wordpress.wp_posts", "wordpress.wp_term_taxonomy"], dialect
    )
    assert {t.table: (t.mode, t.max_size_kb) for t in tables} == {
        "wordpress.wp_posts": ("limited", 7360),
        "wordpress.wp_term_taxonomy": ("limited", DEFAULT),
    }
    assert all(t.reason.startswith(MEASURED) for t in tables)
    # The task stays at the catalog's size; only wp_posts gets a limit of its own.
    assert size == DEFAULT
    assert [t.table for t in table_lob_limits(tables, size)] == ["wordpress.wp_posts"]
    assert unmeasured_lob_tables(tables, dialect) == []


@pytest.mark.parametrize("data_type", ["longtext", "mediumtext", "longblob", "json"])
def test_unmeasured_mysql_lobs_run_limited_never_full_and_stop_preflight(data_type):
    # The declared maximum of a LONGTEXT (4 GB) is the type's ceiling, not a bound on the data:
    # it neither sends the table to full LOB mode nor sizes its limit (that would be 100 MB).
    # And no silent size: the table is unmeasured, which stops preflight's lob_sizes check.
    collector = _collector(("wordpress.wp_options", [_c("option_value", data_type)]))
    size, [t] = lob_plan(collector, ["wordpress.wp_options"], "mysql")
    assert (t.mode, t.size_source) == ("limited", "unmeasured")
    assert "LOB size not measured" in t.reason and "option_value" in t.reason
    assert MYSQL_MEASURE in t.reason and "preflight's lob_sizes check stops" in t.reason
    assert "lob-sizes.sql" in t.reason and "run_dms_step.py lob-sizes" in t.reason
    assert unmeasured_lob_tables([t], "mysql") == ["wordpress.wp_options"]


def test_skipping_the_measurement_runs_unmeasured_mysql_lobs_at_one_mb():
    collector = _collector(_wp_posts(None, None), _wp_term_taxonomy(120))
    size, tables = lob_plan(collector, ["wordpress.wp_posts", "wordpress.wp_term_taxonomy"], "mysql",
                            skip_unmeasured=True)  # fmt: skip
    by = {t.table: (t.max_size_kb, t.size_source) for t in tables}
    assert by == {
        "wordpress.wp_posts": (1024, "skipped"),
        "wordpress.wp_term_taxonomy": (DEFAULT, "measured"),
    }
    assert size == DEFAULT  # the task stays small; wp_posts gets its own 1 MB rule
    assert unmeasured_lob_tables(tables, "mysql") == []


def test_a_partly_measured_mysql_table_falls_back_to_the_catalogs_size():
    collector = _collector(_wp_posts(3_000_000, None))
    size, [t] = lob_plan(collector, ["wordpress.wp_posts"], "mysql")
    assert (t.mode, t.max_size_kb, size) == ("limited", DEFAULT, DEFAULT)
    assert not t.reason.startswith(MEASURED) and "LOB size not measured" in t.reason


# --- the plan writer on a MySQL job ---------------------------------------------------------


def _mysql_job(measured: int | None):
    inputs = _job()
    collector = copy.deepcopy(dict(inputs.collector))
    collector["metadata"]["source_database"].update(version="8.0.45", database_size_gb=0.3)
    for t in collector["database_schema"]["tables"]:
        for c in t["columns"]:
            c.setdefault("data_type", "bigint")
        if t["table_id"] == "shop.orders":
            t["columns"].append(_c("body", "longtext", measured))
    return dataclasses.replace(inputs, collector=collector)


def test_a_mysql_plan_never_has_a_full_lob_table():
    for measured in (None, 3_000_000):
        rep = build_step_plans(_mysql_job(measured), prices=None)[0][0].replication
        assert rep is not None and rep.lob_tables
        assert {t.mode for t in rep.lob_tables} == {"limited"}


def test_the_mysql_plan_says_when_the_collection_did_not_measure():
    plan = build_step_plans(_mysql_job(None), prices=None)[0][0]
    [note] = [n for n in plan.notes if "LOB size not measured" in n]
    assert "shop.orders" in note and MYSQL_MEASURE in note and "validation" in note
    # The MySQL collection script does not record value lengths: the note never sends the
    # customer to collect again for a measurement it would not get.
    assert "collect-postgresql.sql" not in note and "collect again" not in note
    measured = build_step_plans(_mysql_job(3_000_000), prices=None)[0][0]
    assert not any("LOB size not measured" in n for n in measured.notes)


def test_a_measured_mysql_plan_sizes_the_instance_for_its_lob_limit():
    rep = build_step_plans(_mysql_job(3_000_000), prices=None)[0][0].replication
    assert rep is not None
    [orders] = [t for t in rep.lob_tables if t.table == "shop.orders"]
    # The only LOB table: the task runs at its measured 7360 KB, above dms.r6i.large's 5120 KB
    # task-wide limit, so the class is raised (as from a PostgreSQL source, #1918).
    assert (orders.max_size_kb, rep.lob_max_size_kb) == (7360, 7360)
    assert rep.sizing.instance_class == "dms.r6i.xlarge"
    assert "raised from dms.t3.medium" in rep.sizing.reason


# --- the rendered task ----------------------------------------------------------------------


def test_a_mysql_task_runs_limited_lob_mode_even_with_a_full_table_in_the_plan():
    # The fixture plan has one full LOB table, as every MySQL plan written before #2123 has:
    # re-rendering it must not bring full LOB mode back.
    inputs = make_step1_inputs("aurora_mysql")
    assert any(t.mode == "full" for t in inputs.plan.replication.lob_tables)
    assert _lob(task_settings(inputs)) == (False, True, DEFAULT)


def _with_lob_tables(engine: str, lob_tables: list[dict]):
    b = make_bundle(engine)
    replication = b.plan.replication.model_copy(update={
        "lob_max_size_kb": DEFAULT,
        "lob_tables": [type(b.plan.replication.lob_tables[0]).model_validate(t) for t in lob_tables]})  # fmt: skip
    plan = b.plan.model_copy(update={"replication": replication})
    return bundle_inputs(DmsStepBundle(plan=plan, contract=b.contract, ddl=b.ddl))


def test_a_mysql_table_with_a_larger_measured_limit_gets_its_own_lob_settings_rule():
    inputs = _with_lob_tables("aurora_mysql", [
        {"table": "shop.customers", "mode": "limited", "columns": ["bio"], "max_size_kb": 7360,
         "reason": f"{MEASURED} the longest LOB value is 3000000 bytes (bio)."},
        {"table": "shop.orders", "mode": "limited", "columns": ["note"], "max_size_kb": DEFAULT,
         "reason": f"{MEASURED} the longest LOB value is 10 bytes (note)."}])  # fmt: skip
    assert _lob(task_settings(inputs)) == (False, True, DEFAULT)
    lob_rules = [r for r in table_mappings(inputs)["rules"] if r["rule-type"] == "table-settings"]
    assert lob_rules == [{
        "rule-type": "table-settings", "rule-id": lob_rules[0]["rule-id"], "rule-name": lob_rules[0]["rule-id"],
        "object-locator": {"schema-name": "shop", "table-name": "customers"},
        "lob-settings": {"mode": "limited", "bulk-max-size": "7360"}}]  # fmt: skip


@pytest.mark.passes_on_main  # guards what MySQL must keep: no PostgreSQL-only endpoint setting or rule
def test_a_mysql_source_endpoint_has_no_lob_truncation_setting_and_no_array_rules():
    # FailTasksOnLobTruncation is a PostgreSQL source endpoint setting; MySqlSettings has none.
    props = render_dms_template(make_step1_inputs("aurora_mysql"))["Resources"]["SourceEndpoint"]
    assert "FailTasksOnLobTruncation" not in props["Properties"]["MySqlSettings"]
    rules = table_mappings(make_step1_inputs("aurora_mysql"))["rules"]
    assert not [r for r in rules if r["rule-type"] == "validation"]


def test_the_mysql_ddl_header_says_why_limited_lob_mode_and_how_truncation_is_caught():
    flat = " ".join(line.removeprefix("-- ")
                    for line in render_ddl(make_step1_inputs("aurora_mysql", measured=True)).splitlines())  # fmt: skip
    assert f"limited LOB mode at {DEFAULT} KB (LobMaxSize) on " in flat
    assert "NOT NULL" in flat and "PostgreSQL" not in flat
    # The MySQL source has no FailTasksOnLobTruncation: validation is what reports a truncation.
    assert "FailTasksOnLobTruncation" in flat and "no FailTasksOnLobTruncation" in flat
    assert "DMS validation" in flat
    assert "The tables: shop.customers, shop.orders." in flat
    # Unmeasured: no size at all, and no stack, until lob-sizes.sql is read (#2123).
    flat = " ".join(line.removeprefix("-- ")
                    for line in render_ddl(make_step1_inputs("aurora_mysql")).splitlines())  # fmt: skip
    assert "LobMaxSize" not in flat and "no DMS stack" in flat and "lob-sizes.sql" in flat


# --- PostgreSQL is unchanged (#1918) --------------------------------------------------------


@pytest.mark.passes_on_main  # guards #1918's PostgreSQL behaviour against this change
def test_postgresql_keeps_its_1918_sizing_wording_and_truncation_setting():
    collector = {
        "metadata": {"source_database": {"engine": "postgresql", "version": "16.4"}},
        "database_schema": {"tables": [{"table_id": "public.posts", "table_name": "posts", "primary_key": ["id"],
                                        "columns": [{"column_name": "id", "data_type": "int4"},
                                                    {"column_name": "raw", "data_type": "text"}]}]},
    }  # fmt: skip
    size, [t] = lob_plan(collector, ["public.posts"], "postgresql")
    assert (t.mode, t.max_size_kb, size) == ("limited", DEFAULT, DEFAULT)
    assert "it writes a NULL LOB as an empty value, #1918" in t.reason
    assert "SELECT max(octet_length(<column>::text)) FROM <table>" in t.reason
    inputs = make_step1_inputs("aurora_postgresql")
    assert _lob(task_settings(inputs)) == (False, True, DEFAULT)
    source = render_dms_template(inputs)["Resources"]["SourceEndpoint"]["Properties"]
    assert source["PostgreSqlSettings"]["FailTasksOnLobTruncation"] is True
    flat = " ".join(line.removeprefix("-- ") for line in render_ddl(inputs).splitlines())
    assert "from a PostgreSQL source, full LOB mode writes a NULL LOB as an empty value" in flat
    assert "the source endpoint sets FailTasksOnLobTruncation" in flat


# --- the instance holds every table's LOB buffer, not only the largest (#2123 review) -------


def _sized(tables, skip=False):
    from src.tools.data_movement.dms_sizing import size_instance
    from src.tools.data_movement.plan import _lob_sized

    collector = _collector(*tables)
    size, lob = lob_plan(collector, [t for t, _ in tables], "mysql", skip_unmeasured=skip)
    return size, lob, _lob_sized(size_instance(0.3, 1.0), size, lob, "mysql")


def test_one_measured_and_sixty_skipped_tables_size_the_class_for_all_sixty():
    tables = [("wordpress.a_small", [_c("body", "longtext", 1000)])]
    tables += [(f"wordpress.t{i:02d}", [_c("body", "longtext")]) for i in range(60)]
    size, lob, sizing = _sized(tables, skip=True)
    # The task stays at 64 KB and the 60 skipped tables get their own 1 MB rule each: 60 MB
    # of LOB buffers in total, which dms.t3.medium cannot hold.
    assert size == DEFAULT and sum(1 for t in lob if t.size_source == "skipped") == 60
    assert sizing.instance_class == "dms.r6i.large"
    assert "61 LOB columns at 61504 KB in total" in sizing.reason


def test_sixty_measured_large_tables_size_the_class_for_their_total():
    # 100,000 bytes each: 245 KB, so 256 KB in 64 KB steps (not 1 MB). With one small table the
    # task stays at 64 KB and the 60 get their own 256 KB rule: 15424 KB of buffers in total,
    # within dms.t3.medium's 16384 KB; at 200,000 bytes each (512 KB), 30784 KB: dms.r6i.large.
    small = [("wordpress.a_small", [_c("body", "longtext", 1000)])]
    tables = small + [(f"wordpress.t{i:02d}", [_c("body", "longtext", 100_000)]) for i in range(60)]
    size, lob, sizing = _sized(tables)
    assert size == DEFAULT and {t.max_size_kb for t in lob if t.table != "wordpress.a_small"} == {
        256
    }
    assert sizing.instance_class == "dms.t3.medium"
    tables = small + [(f"wordpress.t{i:02d}", [_c("body", "longtext", 200_000)]) for i in range(60)]
    _, lob, sizing = _sized(tables)
    assert {t.max_size_kb for t in lob if t.table != "wordpress.a_small"} == {512}
    assert sizing.instance_class == "dms.r6i.large" and "30784 KB in total" in sizing.reason
