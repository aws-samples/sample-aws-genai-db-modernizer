# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The backend-neutral plan of a contract rule (#1763)."""

from src.contracts.transformation_contract import Rule
from src.tools.transformation_contract.plan_ir import OutputColumn, plan_rule


def test_a_rule_plans_to_output_columns():
    rule = Rule.model_validate({"input_schema": "collector", "source_table": "wp.posts", "target_table": "posts",
        "fields": [
            {"source_column": "ID", "target_column": "ID", "target_type": "BIGINT",
             "op": {"op": "copy", "column": {"table_id": "wp.posts", "column": "ID"}}},
            {"source_column": "flag", "target_column": "flag", "target_type": "BOOLEAN",
             "op": {"op": "transform", "column": {"table_id": "wp.posts", "column": "flag"},
                    "function": "cast", "from_type": "tinyint(1)", "to_type": "BOOLEAN"}},
            {"source_column": "meta", "target_column": "meta", "target_type": "TEXT",
             "op": {"op": "unsupported", "description": "PHP-serialized array"}}]})  # fmt: skip
    p = plan_rule(rule)
    assert p.columns == (
        OutputColumn("ID", "ID", None, "BIGINT"),
        OutputColumn("flag", "flag", "BOOLEAN", "BOOLEAN"),
    )
    assert p.unsupported == ("meta",)


def test_the_plan_carries_the_primary_key():
    rule = Rule.model_validate({"input_schema": "collector", "source_table": "wp.posts",
        "target_table": "posts", "primary_key": ["ID", "rev"], "fields": []})  # fmt: skip
    assert plan_rule(rule).primary_key == ("ID", "rev")
