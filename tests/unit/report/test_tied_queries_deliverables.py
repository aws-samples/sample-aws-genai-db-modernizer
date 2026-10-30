# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report shows unresolved engine ties (#503) -- the same
per-query fact the approval gate already shows from
``QueryAssignment.tied_engines``.
"""

from __future__ import annotations

from typing import Any

from src.report.renderers import render_engineering_report_md


def _report(tied_queries: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-12T00:00:00Z",
        "ranking": [
            {
                "target": "dynamodb",
                "confidence_score": 70,
                "workload_percent": 100.0,
                "assigned_queries": 3,
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": tied_queries,
    }


_ONE_TIED = [
    {
        "query_id": "q1",
        "assigned_engine": "documentdb",
        "tied_engines": ["documentdb", "dynamodb"],
    }
]
_THREE_TIED = _ONE_TIED + [
    {
        "query_id": f"q{i}",
        "assigned_engine": "documentdb",
        "tied_engines": ["documentdb", "dynamodb"],
    }
    for i in (2, 3)
]


class TestEngineeringReportShowsTiedQueries:
    def test_lists_each_tied_query_with_display_names(self) -> None:
        md = render_engineering_report_md(_report(_ONE_TIED))
        assert "Unresolved engine ties" in md
        assert "`q1`" in md
        assert "DocumentDB" in md
        assert "DynamoDB" in md
        # No raw internal reference leaks into the deliverable.
        assert "ADR-031" not in md
        assert "tie_unresolved" not in md

    def test_heading_pluralizes_correctly(self) -> None:
        # #503 review round 3, finding 2: plural_noun defaults to "querys"
        # without an explicit plural form.
        one = render_engineering_report_md(_report(_ONE_TIED))
        three = render_engineering_report_md(_report(_THREE_TIED))
        assert "## Unresolved engine ties (1 query)" in one
        assert "## Unresolved engine ties (3 queries)" in three
        assert "querys" not in one
        assert "querys" not in three

    def test_omits_the_section_when_there_are_no_ties(self) -> None:
        md = render_engineering_report_md(_report([]))
        assert "Unresolved engine ties" not in md

    def test_omits_the_section_when_the_field_is_absent(self) -> None:
        # A report synthesized before this field existed (None, not []).
        md = render_engineering_report_md(_report(None))
        assert "Unresolved engine ties" not in md
