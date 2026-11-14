# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine call counts match a schema by the source engine's own rule (#2268).

PostgreSQL schemas ``"Reporting"`` and ``reporting`` are two schemas, so their same-named
functions do not share counts. MySQL with ``lower_case_table_names=2`` lists a routine
under ``WordPress`` while its call counts say ``wordpress``: they are one schema.
"""

import pytest

from src.agents.collector.routine_rows import attach_routine_stats


def test_postgresql_schemas_that_differ_only_by_case_keep_their_own_counts():
    procs = [
        {"routine_name": "f", "schema_name": "Reporting"},
        {"routine_name": "f", "schema_name": "reporting"},
    ]
    stats = [
        {"routine_name": "f", "schema_name": "Reporting", "execution_count": 1},
        {"routine_name": "f", "schema_name": "reporting", "execution_count": 3},
    ]
    attach_routine_stats(procs, [], stats, source_engine="postgresql")
    assert [p["execution_count"] for p in procs] == [1, 3]


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "sqlserver"])
def test_a_schema_case_mismatch_still_attaches_the_counts_on_other_engines(engine):
    procs = [
        {"routine_name": "p", "schema_name": "WordPress"},
        {"routine_name": "f", "schema_name": "WordPress"},
    ]
    stats = [
        {"routine_name": "p", "schema_name": "wordpress", "execution_count": 2},
        {"routine_name": "f", "schema_name": "wordpress", "execution_count": 1},
    ]
    attach_routine_stats(procs, [], stats, source_engine=engine)
    assert [p["execution_count"] for p in procs] == [2, 1]
