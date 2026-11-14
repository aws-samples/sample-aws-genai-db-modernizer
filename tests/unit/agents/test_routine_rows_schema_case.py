# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine call counts match by schema exactly (#2268): PostgreSQL schemas ``"Reporting"``
and ``reporting`` are two schemas, so their same-named functions do not share counts."""

from src.agents.collector.routine_rows import attach_routine_stats


def test_schemas_that_differ_only_by_case_do_not_share_counts():
    procs = [
        {"routine_name": "f", "schema_name": "Reporting"},
        {"routine_name": "f", "schema_name": "reporting"},
    ]
    stats = [
        {"routine_name": "f", "schema_name": "Reporting", "execution_count": 1},
        {"routine_name": "f", "schema_name": "reporting", "execution_count": 2},
    ]
    attach_routine_stats(procs, [], stats)
    assert [p["execution_count"] for p in procs] == [1, 2]
