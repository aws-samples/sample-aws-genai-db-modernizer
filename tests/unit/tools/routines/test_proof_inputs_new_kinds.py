# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine proofs behave as before the generator modelled bytes and enums (#1830)."""

from __future__ import annotations

import pytest

from src.tools.routines import proof_inputs as pi
from src.tools.synthetic_data.generator import generate_rows


def _col(name, data_type, normalized=None, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "normalized_data_type": normalized,
            "nullable": nullable, **kw}  # fmt: skip


@pytest.mark.passes_on_main  # main's behaviour, kept (#2058 review)
def test_an_update_with_no_named_column_still_skips_byte_and_enum_columns():
    table = {"table_id": "app.files", "table_name": "files", "primary_key": ["id"],
             "columns": [_col("id", "int", "integer"), _col("body", "blob", "blob"),
                         _col("kind", "enum('a','b')"), _col("n", "int", "integer")]}  # fmt: skip
    spec = pi.TriggerSpec(table="app.files", timing="AFTER", events=("UPDATE",))
    assert pi._update_column(table, spec, {"id"}) == ("n", True)


@pytest.mark.passes_on_main  # main's behaviour, kept (#2058 review)
def test_loadable_rows_get_no_full_length_byte_edge():
    table = {"table_id": "s.t", "row_count": 3, "primary_key": [],
             "columns": [_col("b", "varbinary(4)", "binary", max_length=4),
                         _col("f", "binary(4)", "binary", max_length=4)]}  # fmt: skip
    t = generate_rows(table, dialect="mysql", seed=1, loadable=True)
    assert t.edge_rows == 0
    assert all(r["b"] == "ab" and r["f"] == "ab" for r in t.rows)
