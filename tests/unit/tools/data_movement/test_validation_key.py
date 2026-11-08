# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One key rule for step 1's contract, the plan and the DMS gate (#2042): the primary key,
else the smallest non-partial, expression-free unique index on NOT NULL columns; DMS validates
on it only when no key column has a LOB, binary or unbounded type."""

from __future__ import annotations

from typing import Any

import pytest

from src.tools.data_movement.plan import _hub_key
from src.tools.data_movement.validation_key import (
    NO_KEY_MESSAGE,
    TableKey,
    key_type_problem,
    table_key,
    validation_problem,
)
from src.tools.transformation_contract.step1 import build_step1
from src.tools.transformation_contract.validate import validate_step1
from tests.unit.tools.transformation_contract.conftest import col, step1_inputs, table


def _ix(name: str, cols: list[str], **extra: Any) -> dict[str, Any]:
    return {"index_name": name, "columns": cols, "is_unique": True, "index_type": "btree", **extra}


def _t(name: str, columns: list[dict[str, Any]], pk=(), indexes=()) -> dict[str, Any]:
    t: dict[str, Any] = {**table("public", name, columns, pk=pk), "indexes": list(indexes)}
    return t


# The Discourse sample's shapes (a PostgreSQL step 1 rehearsal, DMS 3.6.1).
DISCOURSE_LIKE = [
    # A unique index on NOT NULL columns, one an unbounded varchar: a key DMS can't validate on.
    _t("ad_plugin_house_ads_routes",
       [col("ad_plugin_house_ad_id", "int8", 1), col("route_name", "varchar", 2)],
       indexes=[_ix("index_house_ads_pages", ["ad_plugin_house_ad_id", "route_name"])]),
    # The only unique index allows NULLs: no key.
    _t("inferred_concept_posts",
       [col("post_id", "int8", 1, nullable=True), col("inferred_concept_id", "int8", 2, nullable=True)],
       indexes=[_ix("index_inferred_concept_posts_uniqueness", ["post_id", "inferred_concept_id"])]),
    _t("topic_views",
       [col("user_id", "int4", 1, nullable=True), col("ip_address", "inet", 2, nullable=True),
        col("topic_id", "int4", 3)],
       indexes=[_ix("uniq_ip_or_user_id_topic_views", ["user_id", "ip_address", "topic_id"])]),
    # A unique index on NOT NULL bounded columns: DMS validates on it.
    _t("post_timings", [col("topic_id", "int4", 1), col("post_number", "int4", 2), col("user_id", "int4", 3)],
       indexes=[_ix("index_post_timings_unique", ["topic_id", "post_number", "user_id"])]),
    # A primary key on an unbounded varchar.
    _t("schema_migrations", [col("version", "varchar", 1)], pk=["version"]),
    # No index at all.
    _t("ad_plugin_house_ads_categories", [col("ad_plugin_house_ad_id", "int8", 1)]),
    # A NOT NULL unique index that is partial, and one that is on an expression: no key.
    _t("partial_only", [col("a", "int4", 1)], indexes=[_ix("p", ["a"], predicate="a > 0")]),
    _t("expression_only", [col("a", "varchar", 1, max_length=40)],
       indexes=[_ix("e", ["a"], has_expressions=True, index_expressions="lower(a)")]),
    _t("orders", [col("id", "int8", 1), col("ref", "varchar", 2, max_length=36)], pk=["id"],
       indexes=[_ix("ux_ref", ["ref"])]),
]  # fmt: skip
BY_NAME = {t["table_name"]: t for t in DISCOURSE_LIKE}


def test_a_unique_index_that_allows_nulls_is_no_key():
    assert table_key(BY_NAME["inferred_concept_posts"]) is None
    assert table_key(BY_NAME["topic_views"]) is None


def test_a_not_null_unique_index_on_bounded_types_is_the_key_and_dms_validates_on_it():
    t = BY_NAME["post_timings"]
    assert table_key(t) == TableKey(("topic_id", "post_number", "user_id"), "unique_index",
                                    "index_post_timings_unique")  # fmt: skip
    assert validation_problem(t, "postgresql") is None


def test_a_key_on_an_unbounded_varchar_is_a_key_dms_cannot_validate_on():
    t = BY_NAME["ad_plugin_house_ads_routes"]
    assert table_key(t) == TableKey(("ad_plugin_house_ad_id", "route_name"), "unique_index",
                                    "index_house_ads_pages")  # fmt: skip
    problem = validation_problem(t, "postgresql")
    assert problem is not None and problem.code == "key_not_validatable"
    assert problem.message == (
        "Its unique index index_house_ads_pages has a column type DMS cannot validate on: "
        "route_name is an unbounded varchar."
    )
    pk = validation_problem(BY_NAME["schema_migrations"], "postgresql")
    assert pk is not None and pk.code == "key_not_validatable" and "primary key" in pk.message


@pytest.mark.parametrize(
    "name", ["partial_only", "expression_only", "ad_plugin_house_ads_categories"]
)
def test_a_partial_or_expression_index_is_no_key(name):
    assert table_key(BY_NAME[name]) is None
    problem = validation_problem(BY_NAME[name], "postgresql")
    assert problem is not None and (problem.code, problem.message) == ("no_key", NO_KEY_MESSAGE)


def test_the_primary_key_wins_over_a_smaller_unique_index():
    assert table_key(BY_NAME["orders"]) == TableKey(("id",), "primary_key")


def test_the_smallest_eligible_unique_index_wins_then_its_name():
    t = _t("t", [col("a", "int4", 1), col("b", "int4", 2), col("c", "int4", 3, nullable=True)],
           indexes=[_ix("z_ab", ["a", "b"]), _ix("y_c", ["c"]), _ix("x_ab", ["b", "a"])])  # fmt: skip
    assert table_key(t) == TableKey(("b", "a"), "unique_index", "x_ab")


def test_the_nullable_reason_names_the_index_and_its_nullable_columns():
    problem = validation_problem(BY_NAME["topic_views"], "postgresql")
    assert problem is not None and problem.code == "no_key"
    assert problem.message == (
        "No primary key, and its unique index uniq_ip_or_user_id_topic_views allows NULLs in "
        "user_id, ip_address, which DMS cannot validate on."
    )


@pytest.mark.parametrize(("data_type", "max_length"), [
    ("varchar", None), ("character varying", None), ("varchar", 1024), ("VARCHAR(2048)", None),
    ("text", None), ("citext", None), ("json", None), ("jsonb", None), ("bytea", None),
    ("_varchar", None), ("varchar[]", None), ("INTEGER[]", None), ("xml", None), ("hstore", None),
    ("bpchar", None),
])  # fmt: skip
def test_types_dms_cannot_validate_on(data_type, max_length):
    assert key_type_problem(data_type, max_length, "postgresql")


@pytest.mark.parametrize(("data_type", "max_length"), [
    ("varchar", 255), ("varchar", 1023), ("character varying(100)", None), ("char", None),
    ("bpchar", 36), ("int8", None), ("numeric", None), ("date", None), ("timestamp", None),
    ("timestamptz", None), ("uuid", None), ("inet", None),
])  # fmt: skip
def test_types_dms_validates_on(data_type, max_length):
    assert key_type_problem(data_type, max_length, "postgresql") is None


def test_the_contract_and_the_plan_agree_on_every_discourse_like_table():
    inputs = step1_inputs(engine="postgresql", tables=DISCOURSE_LIKE)
    contract = validate_step1(build_step1(inputs), inputs)
    rules = {r.source_table: r for r in contract.rules}
    for t in DISCOURSE_LIKE:
        rule = rules[t["table_id"]]
        contract_keyless = any(f.code == "no_key" for f in rule.findings)
        assert contract_keyless == (_hub_key(t) is None), t["table_id"]
        hub = _hub_key(t)
        assert rule.key_columns == (hub[0] if hub else []), t["table_id"]


def test_the_contract_names_each_table_dms_cannot_validate_with_its_reason():
    inputs = step1_inputs(engine="postgresql", tables=DISCOURSE_LIKE)
    contract = validate_step1(build_step1(inputs), inputs)
    codes = {
        r.source_table.split(".")[1]: sorted(
            f.code for f in r.findings if f.code in ("no_key", "key_not_validatable")
        )
        for r in contract.rules
    }
    assert codes == {
        "ad_plugin_house_ads_routes": ["key_not_validatable"],
        "inferred_concept_posts": ["no_key"],
        "topic_views": ["no_key"],
        "post_timings": [],
        "schema_migrations": ["key_not_validatable"],
        "ad_plugin_house_ads_categories": ["no_key"],
        "partial_only": ["no_key"],
        "expression_only": ["no_key"],
        "orders": [],
    }
    assert all(f.severity == "warning" for r in contract.rules for f in r.findings)


def test_a_rule_written_before_key_columns_still_loads_and_a_keyless_rule_keeps_its_bytes():
    from src.contracts.transformation_contract import Rule

    old = {"input_schema": "collector", "source_table": "public.t", "target_table": "t",
           "table_class": None, "primary_key": ["id"], "fields": [], "status": "valid", "findings": []}  # fmt: skip
    rule = Rule.model_validate(old)
    assert rule.key_columns == [] and rule.model_dump(mode="json") == old
    keyed = rule.model_copy(update={"key_columns": ["id"]})
    assert keyed.model_dump(mode="json")["key_columns"] == ["id"]
