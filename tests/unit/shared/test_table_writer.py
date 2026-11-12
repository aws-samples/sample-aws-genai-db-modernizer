# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table's writer (ADR-031 Amendment 9): its source of truth, else the relational step."""

from __future__ import annotations

from src.shared.engine_names import SOURCE_DATABASE
from src.shared.table_writer import table_writer


def test_a_written_table_is_owned_by_its_source_of_truth() -> None:
    assert table_writer("app.carts", {"app.carts": "dynamodb"}, "aurora_mysql") == "dynamodb"


def test_a_table_nobody_writes_stays_with_the_relational_step() -> None:
    # Never its readers' engine: a later step reads a copy of it.
    assert table_writer("app.products", {"app.carts": "dynamodb"}, "aurora_mysql") == (
        "aurora_mysql"
    )


def test_with_no_relational_step_the_source_database_keeps_it() -> None:
    assert table_writer("app.products", {}, None) == SOURCE_DATABASE
