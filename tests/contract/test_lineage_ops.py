# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The lineage operations are a closed, discriminated union (#1762, ADR-033 Decision 4)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from src.contracts.lineage_ops import (
    ColumnRef,
    CopyOp,
    LineageOp,
    TransformOp,
    UnsupportedOp,
    reads_own_row,
)

OP = TypeAdapter(LineageOp)


def test_each_op_parses_to_its_model():
    assert isinstance(
        OP.validate_python({"op": "copy", "column": {"table_id": "wp.posts", "column": "ID"}}),
        CopyOp,
    )
    t = OP.validate_python({"op": "transform", "column": {"table_id": "wp.posts", "column": "flag"},
                            "function": "cast", "from_type": "tinyint(1)", "to_type": "BOOLEAN"})  # fmt: skip
    assert isinstance(t, TransformOp) and t.to_type == "BOOLEAN"
    assert isinstance(
        OP.validate_python({"op": "unsupported", "description": "PHP-serialized array"}),
        UnsupportedOp,
    )


def test_an_unknown_op_is_refused():
    with pytest.raises(ValidationError, match="op"):
        OP.validate_python({"op": "sql", "text": "SELECT 1"})


def test_transform_functions_are_a_closed_list():
    with pytest.raises(ValidationError):
        OP.validate_python({"op": "transform", "column": {"table_id": "a.b", "column": "c"},
                            "function": "eval", "from_type": "x", "to_type": "y"})  # fmt: skip


def test_unsupported_needs_a_description():
    with pytest.raises(ValidationError):
        UnsupportedOp(op="unsupported", description="")


def test_a_column_ref_names_a_schema_qualified_table():
    with pytest.raises(ValidationError):
        ColumnRef(table_id="posts", column="ID")


def test_copy_and_transform_read_the_rules_own_table():
    own = CopyOp(op="copy", column=ColumnRef(table_id="wp.posts", column="ID"))
    other = CopyOp(op="copy", column=ColumnRef(table_id="wp.users", column="ID"))
    assert reads_own_row(own, "wp.posts") and not reads_own_row(other, "wp.posts")
    assert reads_own_row(UnsupportedOp(op="unsupported", description="d"), "wp.posts")


def test_a_table_id_needs_a_schema_but_a_name_may_hold_spaces():
    assert ColumnRef(table_id="shop.Order Items", column="id").table_id == "shop.Order Items"
    with pytest.raises(ValidationError):
        ColumnRef(table_id="posts", column="id")
