# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest

from src.tools.schema.aurora_common.source_family import (
    classify_source_family,
    classify_type_family,
    migration_strategy,
)


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("postgresql", "postgresql"),
        ("Postgres", "postgresql"),
        ("aurora_postgresql", "postgresql"),
        ("mysql", "mysql"),
        ("MariaDB", "mysql"),
        ("oracle", "other"),
        ("sqlserver", "other"),
        ("", "other"),
    ],
)
def test_classify_source_family(engine, expected):
    assert classify_source_family(engine) == expected


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("postgresql", "postgresql"),
        ("MariaDB", "mysql"),
        ("Oracle", "oracle"),
        ("sqlserver", "sqlserver"),
        ("mssql", "sqlserver"),
        ("db2", "other"),
        ("", "other"),
    ],
)
def test_classify_type_family_names_oracle_and_sqlserver(engine, expected):
    """The draft DDL's own family (#2219); the strategy keeps the three-way one above."""
    assert classify_type_family(engine) == expected


@pytest.mark.parametrize(
    "engine,target,expected",
    [
        ("postgresql", "aurora_postgresql", "carry_over"),
        ("oracle", "aurora_postgresql", "translate"),
        ("sqlserver", "aurora_postgresql", "translate"),
        ("mysql", "aurora_mysql", "carry_over"),
        ("oracle", "aurora_mysql", "translate"),
        # Known-but-mismatched families must translate, not carry over.
        ("mysql", "aurora_postgresql", "translate"),
        ("postgresql", "aurora_mysql", "translate"),
    ],
)
def test_migration_strategy(engine, target, expected):
    assert migration_strategy(engine, target) == expected
