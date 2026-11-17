# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A transformation-contract finding that quotes a collected name holding a control,
format, surrogate or line separator character is recorded, never refused (#2381)."""

from __future__ import annotations

import pytest

from src.contracts.transformation_contract import Finding


@pytest.mark.parametrize("bad", ["\ud800", "\x00", "\u2028", "\u202e"])
def test_a_finding_about_an_unsafe_name_is_recorded_with_the_character_replaced(bad):
    finding = Finding(code="missing_target_column", severity="error",
                      message=f"no{bad}te has no column in the Aurora DDL",
                      table_id="public.orders", column=f"no{bad}te")  # fmt: skip

    assert finding.message == "no\ufffdte has no column in the Aurora DDL"
    assert finding.column == "no\ufffdte"
    assert finding.model_dump_json()  # a lone surrogate would not even serialize


@pytest.mark.passes_on_main  # printable text, accents and NBSP included, is kept as is
def test_a_printable_finding_is_unchanged():
    message = "caf\u00e9\u00a0x has no column"
    assert Finding(code="c", severity="warning", message=message).message == message


def test_the_contract_refuses_the_same_categories_as_the_quoting_rule():
    from src.contracts import transformation_contract
    from src.shared.sql_identifiers import UNPRINTABLE_CATEGORIES

    assert transformation_contract._UNPRINTABLE == UNPRINTABLE_CATEGORIES
