# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 validator: an error blocks its own rule, a warning never does (#1764)."""

from __future__ import annotations

import copy
from functools import partial

from src.tools.transformation_contract import validate as validate_module
from src.tools.transformation_contract.step1 import build_step1, target_tables
from src.tools.transformation_contract.validate import validate_step1

from .conftest import step1_inputs

_inputs = partial(step1_inputs, design=True)
NO_KEY = "No primary or unique key: DMS cannot validate this table, and Debezium needs declared key columns."


def _codes(c, table):
    return [f.code for r in c.rules if r.source_table == table for f in r.findings]


def _rule(c, table):
    return next(r for r in c.rules if r.source_table == table)


def _design_cols(inputs, i):
    return inputs.schema_designs["aurora_mysql"][1]["table_definitions"][i]["columns"]


def test_a_valid_step_has_only_the_no_key_warning():
    c = validate_step1(build_step1(_inputs()), _inputs())
    assert c.status == "valid" and c.blocked_rule_count == 0
    assert [f.code for r in c.rules for f in r.findings] == ["no_key"]  # wp.log has no key


def test_no_key_is_a_warning_not_an_error():
    c = validate_step1(build_step1(_inputs()), _inputs())
    log = _rule(c, "wp.log")
    assert log.status == "valid" and log.findings[0].severity == "warning"
    assert log.findings[0].message == NO_KEY
    assert log.findings[0].table_id == "wp.log"


def test_a_unique_index_counts_as_a_key_but_a_partial_one_does_not():
    def with_index(**extra):
        inputs = _inputs()
        tables = inputs.collector["database_schema"]["tables"]
        tables[1]["indexes"] = [
            {
                "index_name": "u",
                "columns": ["msg"],
                "is_unique": True,
                "index_type": "btree",
                **extra,
            }
        ]
        return inputs

    inputs = with_index()
    assert "no_key" not in _codes(validate_step1(build_step1(inputs), inputs), "wp.log")
    inputs = with_index(predicate="msg is not null")
    assert "no_key" in _codes(validate_step1(build_step1(inputs), inputs), "wp.log")


def test_unknown_column_and_table():
    inputs = _inputs()
    contract = build_step1(inputs)
    tables = inputs.collector["database_schema"]["tables"]
    tables[0]["columns"] = [c for c in tables[0]["columns"] if c["column_name"] != "flag"]
    tables.pop()  # wp.log leaves the collector
    c = validate_step1(contract, inputs)
    assert "unknown_column" in _codes(c, "wp.posts") and "unknown_table" in _codes(c, "wp.log")
    assert _rule(c, "wp.posts").status == "blocked" and _rule(c, "wp.log").status == "blocked"


def test_extra_target_column_and_type_mismatch():
    inputs = _inputs()
    contract = build_step1(inputs)
    cols = _design_cols(inputs, 0)
    cols.append({"name": "added", "aurora_type": "INT"})
    next(c for c in cols if c["name"] == "title")["aurora_type"] = "VARCHAR(40)"
    c = validate_step1(contract, inputs)
    assert {"extra_target_column", "type_mismatch"} <= set(_codes(c, "wp.posts"))
    assert _rule(c, "wp.posts").status == "blocked" and _rule(c, "wp.log").status == "valid"


def test_a_column_missing_from_the_merged_targets_blocks_only_its_rule(monkeypatch):
    inputs = _inputs()
    contract = build_step1(inputs)
    real = target_tables

    def without_flag(*args, **kwargs):
        tables = real(*args, **kwargs)
        posts = tables["posts"]
        tables["posts"] = type(posts)(
            posts.name, tuple(c for c in posts.columns if c.name != "flag")
        )
        return tables

    monkeypatch.setattr(validate_module, "target_tables", without_flag)
    c = validate_step1(contract, inputs)
    posts, log = _rule(c, "wp.posts"), _rule(c, "wp.log")
    assert posts.status == "blocked" and "missing_target_column" in _codes(c, "wp.posts")
    assert log.status == "valid" and c.status == "has_blocked_rules" and c.blocked_rule_count == 1


def test_a_design_missing_a_column_does_not_block_the_generator_fills_it():
    inputs = _inputs()
    _design_cols(inputs, 0).pop()  # "flag" leaves the design, stays in the collector
    c = validate_step1(build_step1(inputs), inputs)
    assert c.status == "valid" and "missing_target_column" not in _codes(c, "wp.posts")
    assert "flag" in [f.target_column for f in _rule(c, "wp.posts").fields]


def test_unsupported_fields_are_warnings():
    inputs = _inputs()
    _design_cols(inputs, 1)[0]["aurora_type"] = "NOT A TYPE(("
    c = validate_step1(build_step1(inputs), inputs)
    log = _rule(c, "wp.log")
    assert "unsupported_field" in _codes(c, "wp.log") and log.status == "valid"
    assert c.unsupported_count == 1


def test_a_wave_table_with_no_rule_is_missing():
    inputs = _inputs()
    contract = build_step1(inputs)
    contract = contract.model_copy(
        update={"rules": [r for r in contract.rules if r.source_table != "wp.log"]}
    )
    c = validate_step1(contract, inputs)
    assert "missing_table" in _codes(c, "wp.log") and c.status == "has_blocked_rules"
    assert c.blocked_rule_count == 1


def test_a_view_in_the_wave_is_not_a_missing_table():
    views = [{"view_id": "wp.v", "view_name": "v", "definition": "x"}]
    inputs = step1_inputs(design=True, views=views)
    c = validate_step1(build_step1(inputs), inputs)
    assert c.status == "valid" and "wp.v" in c.views and "missing_table" not in _codes(c, "wp.v")


def test_ambiguous_target_findings_are_kept():
    tables = [
        {"table_id": f"{s}.users", "table_name": "users", "schema_name": s, "row_count": 1,
         "primary_key": ["id"], "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]}
        for s in ("a", "b")
    ]  # fmt: skip
    inputs = step1_inputs(tables=tables)
    c = validate_step1(build_step1(inputs), inputs)
    assert c.blocked_rule_count == 2
    assert _codes(c, "a.users") == ["ambiguous_target"]


def test_no_rules_steps_are_returned_unchanged():
    inputs = step1_inputs("cross_engine")
    c = build_step1(inputs)
    assert validate_step1(c, inputs) == c


def test_validating_twice_gives_the_same_contract_and_leaves_inputs_alone():
    inputs = _inputs()
    before = copy.deepcopy(inputs.collector)
    once = validate_step1(build_step1(inputs), inputs)
    assert validate_step1(once, inputs) == once and inputs.collector == before


def test_a_kept_rule_still_gets_the_unknown_table_check_without_duplicates():
    tables = [
        {"table_id": f"{s}.users", "table_name": "users", "schema_name": s, "row_count": 1,
         "primary_key": ["id"], "columns": [{"column_name": "id", "data_type": "int", "nullable": False}]}
        for s in ("a", "b")
    ]  # fmt: skip
    inputs = step1_inputs(tables=tables)
    contract = build_step1(inputs)
    inputs.collector["database_schema"]["tables"].pop()  # b.users leaves the collector
    c = validate_step1(contract, inputs)
    assert sorted(_codes(c, "b.users")) == ["ambiguous_target", "unknown_table"]
    assert _rule(c, "b.users").status == "blocked"
    assert _codes(validate_step1(c, inputs), "b.users") == _codes(c, "b.users")


def test_a_case_only_type_difference_is_not_a_mismatch():
    inputs = _inputs()
    contract = build_step1(inputs)
    next(c for c in _design_cols(inputs, 0) if c["name"] == "title")["aurora_type"] = "varchar(20)"
    c = validate_step1(contract, inputs)
    assert "type_mismatch" not in _codes(c, "wp.posts")


def test_a_wave_table_the_collector_lacks_is_blocked_once():
    inputs = _inputs()
    inputs.report["migration_waves"][0]["tables"].append("wp.ghost")
    c = validate_step1(build_step1(inputs), inputs)
    assert _codes(c, "wp.ghost") == ["unknown_table"] and "wp.ghost" not in c.views
    assert c.status == "has_blocked_rules" and c.blocked_rule_count == 1
