# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from functools import partial

from src.tools.transformation_contract.shape_check import shape_check
from src.tools.transformation_contract.step1 import build_step1
from src.tools.transformation_contract.validate import validate_step1

from .conftest import step1_inputs

_inputs = partial(step1_inputs, design=True)


def _with_column(inputs, name, data_type, aurora_type, *, pk=False):
    t = inputs.collector["database_schema"]["tables"][0]
    t["columns"].append(
        {"column_name": name, "data_type": data_type, "ordinal_position": 9, "nullable": False}
    )
    inputs.schema_designs["aurora_mysql"][1]["table_definitions"][0]["columns"].append(
        {"name": name, "aurora_type": aurora_type}
    )
    if pk:
        t["primary_key"] = ["ID", name]
    return inputs


def _run(inputs, **kw):
    return shape_check(validate_step1(build_step1(inputs), inputs), inputs, **kw)


def _posts(c):
    return next(r for r in c.rules if r.source_table == "wp.posts")


def test_a_zero_date_cast_blocks_the_rule():
    c = _run(_with_column(_inputs(), "post_date", "datetime", "DATE"))
    posts = _posts(c)
    assert posts.status == "blocked"
    assert c.status == "has_blocked_rules" and c.blocked_rule_count == 1
    assert any(f.code == "cast_failure" and "0000-00-00" in f.message for f in posts.findings)


def test_an_unknown_target_type_is_a_warning():
    c = _run(_with_column(_inputs(), "shape", "geometry", "GEOMETRY"))
    posts = _posts(c)
    assert posts.status == "valid" and c.status == "valid"
    unknown = [f for f in posts.findings if f.code == "unknown_target_type"]
    assert unknown and unknown[0].severity == "warning" and "shape" in unknown[0].message


def test_copies_are_checked_too_and_the_clean_sample_has_no_cast_failure():
    c = _run(_inputs())
    assert not [f for r in c.rules for f in r.findings if f.code == "cast_failure"]


def test_a_mysql_boolean_target_accepts_the_tinyint_edge_value():
    posts = _posts(_run(_inputs()))
    assert not [f for f in posts.findings if f.code == "cast_failure" and f.column == "flag"]


def test_a_key_that_collapses_after_conversion_blocks_the_rule():
    # decimal(3,2) -> DECIMAL(3,0) rounds without failing: 50 rows land on <= 10 values
    inputs = _with_column(_inputs(), "grade", "decimal(3,2)", "DECIMAL(3,0)")
    table = inputs.collector["database_schema"]["tables"][0]
    table["primary_key"] = ["grade"]
    table["row_count"] = 50
    posts = _posts(_run(inputs))
    assert posts.status == "blocked" and any(f.code == "key_not_unique" for f in posts.findings)
    assert not any(f.code == "cast_failure" for f in posts.findings)


def test_a_rule_the_validator_blocked_is_not_run():
    inputs = _with_column(_inputs(), "post_date", "datetime", "DATE")
    base = build_step1(inputs)
    inputs.schema_designs["aurora_mysql"][1]["table_definitions"][0]["columns"].append(
        {"name": "extra", "aurora_type": "INT"}
    )
    blocked = validate_step1(base, inputs)
    assert _posts(blocked).status == "blocked"
    assert _posts(shape_check(blocked, inputs)).findings == _posts(blocked).findings


def test_checking_twice_gives_the_same_findings():
    inputs = _with_column(_inputs(), "shape", "geometry", "GEOMETRY")
    once = _run(inputs)
    assert shape_check(once, inputs) == once


def test_the_check_is_deterministic():
    inputs = _with_column(_inputs(), "post_date", "datetime", "DATE")
    assert _run(inputs, seed=3) == _run(inputs, seed=3)


def test_a_missing_row_count_adds_one_warning_per_rule():
    inputs = _inputs()
    for t in inputs.collector["database_schema"]["tables"]:
        t["row_count"] = None
    c = _run(inputs)
    for r in c.rules:
        assert [f.code for f in r.findings].count("default_row_count") == 1


def test_a_zero_date_copy_is_a_warning_not_a_block():
    c = _run(_with_column(_inputs(), "post_date", "datetime", "DATETIME"))
    posts = _posts(c)
    assert posts.status == "valid" and c.status == "valid"
    assert not [f for f in posts.findings if f.code == "cast_failure"]
    (w,) = [f for f in posts.findings if f.code == "copy_edge_value"]
    assert w.severity == "warning" and w.column == "post_date"
    assert "0000-00-00" in w.message and "NO_ZERO_DATE" in w.message


def test_a_transform_to_date_still_blocks():
    posts = _posts(_run(_with_column(_inputs(), "post_date", "datetime", "DATE")))
    assert posts.status == "blocked"


def test_decimal_from_data_type_alone_has_no_findings():
    inputs = _with_column(_inputs(), "avg", "decimal(3,2)", "DECIMAL(3,2)")
    posts = _posts(_run(inputs))
    assert not [f for f in posts.findings if f.column == "avg"]


def test_a_failed_key_cast_is_not_also_reported_as_a_collapsed_key():
    posts = _posts(_run(_with_column(_inputs(), "code", "varchar(10)", "DATE", pk=True)))
    codes = [f.code for f in posts.findings]
    assert "cast_failure" in codes and "key_not_unique" not in codes


def test_an_unknown_source_engine_is_not_run_as_mysql():
    inputs = _with_column(_inputs(), "post_date", "datetime", "DATE")
    inputs.collector["metadata"]["source_database"]["engine"] = "sqlserver"
    c = _run(inputs)
    posts = _posts(c)
    assert posts.status == "valid"
    assert [f.code for f in posts.findings].count("unknown_source_dialect") == 1
    assert not [f for f in posts.findings if f.code == "cast_failure"]


def test_a_4x_postgresql_zero_row_count_is_unknown_so_the_check_warns():
    from .conftest import col, table

    t = table("public", "events", [col("id", "integer", 1)], pk=["id"])
    t["row_count"] = 0
    inputs = step1_inputs(engine="postgresql", tables=[t], version="4.0")
    c = _run(inputs)
    rule = next(r for r in c.rules if r.source_table == "public.events")
    assert [f.code for f in rule.findings if f.code == "default_row_count"] == ["default_row_count"]
