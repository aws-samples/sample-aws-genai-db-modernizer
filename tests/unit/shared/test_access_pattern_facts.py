# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Access-pattern counts as report facts (#792, ADR-032).

The explorer's totals are deliverable facts: synthesis
computes them once from ``query_groups`` into ``facts.access_pattern_totals`` and
the UI reads them. The explorer's live filter counts stay presentation.
"""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import AccessPatternFacts, ReportFacts
from src.shared.report_facts import access_pattern_facts, compute_report_facts


def _ap(engine: str, pid: str | None, op: str | None = "GetItem") -> dict[str, Any]:
    entry: dict[str, Any] = {"engine": engine, "operation": op}
    if pid is not None:
        entry["pattern_id"] = pid
    return entry


def _report(*groups: list[dict[str, Any]], overlay: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "query_groups": [
            {"group_name": f"g{i}", "access_patterns": g} for i, g in enumerate(groups)
        ]
    }
    if overlay:
        report["cache_overlay"] = {"engine": overlay, "query_count": 3}
    return report


class TestAccessPatternFacts:
    def test_none_for_a_report_with_no_query_groups(self) -> None:
        assert access_pattern_facts({}) is None

    def test_counts_owned_patterns_across_engines(self) -> None:
        report = _report(
            [_ap("dynamodb", "DDB-AP-1"), _ap("dynamodb", "DDB-AP-2", "PutItem")],
            [_ap("opensearch", "OS-AP-1", "search"), _ap("dynamodb", "DDB-AP-3", "DeleteItem")],
        )
        assert access_pattern_facts(report) == {
            "owned_pattern_count": 4,
            "cache_layer_pattern_count": 0,
            "listed_pattern_count": 4,
        }

    def test_a_pattern_listed_in_two_groups_counts_once(self) -> None:
        report = _report([_ap("dynamodb", "DDB-AP-1")], [_ap("dynamodb", "DDB-AP-1")])
        assert access_pattern_facts(report)["owned_pattern_count"] == 1  # type: ignore[index]

    def test_the_same_id_on_two_engines_counts_twice(self) -> None:
        report = _report([_ap("dynamodb", "AP-1"), _ap("documentdb", "AP-1")])
        assert access_pattern_facts(report)["owned_pattern_count"] == 2  # type: ignore[index]

    def test_patterns_without_an_id_each_count(self) -> None:
        report = _report([_ap("dynamodb", None), _ap("dynamodb", None)])
        assert access_pattern_facts(report)["owned_pattern_count"] == 2  # type: ignore[index]

    def test_the_cache_layer_is_counted_apart_never_as_an_owner(self) -> None:
        report = _report(
            [_ap("dynamodb", "DDB-AP-1"), _ap("elasticache", "EC-AP-1", "GET")],
            [_ap("elasticache", "EC-AP-2", "SET")],
            overlay="elasticache",
        )
        facts = access_pattern_facts(report)
        assert facts["owned_pattern_count"] == 1  # type: ignore[index]
        assert facts["cache_layer_pattern_count"] == 2  # type: ignore[index]
        assert facts["listed_pattern_count"] == 3  # type: ignore[index]

    def test_without_an_overlay_elasticache_is_an_owner(self) -> None:
        report = _report([_ap("elasticache", "EC-AP-1", "GET")])
        facts = access_pattern_facts(report)
        assert facts["owned_pattern_count"] == 1  # type: ignore[index]
        assert facts["cache_layer_pattern_count"] == 0  # type: ignore[index]

    def test_a_relational_query_awaiting_its_design_is_not_an_access_pattern(self) -> None:
        report = _report(
            [
                _ap("aurora_mysql", "relational-aurora_mysql-q1", "SELECT"),
                _ap("aurora_mysql", "relational-aurora_mysql-q2", "SELECT"),
                _ap("dynamodb", "DDB-AP-1"),
            ]
        )
        facts = access_pattern_facts(report)
        assert facts["owned_pattern_count"] == 1  # type: ignore[index]

    def test_a_designed_aurora_pattern_is_counted(self) -> None:
        report = _report([_ap("aurora_mysql", "AUR-AP-1", "SELECT")])
        assert access_pattern_facts(report)["owned_pattern_count"] == 1  # type: ignore[index]

    def test_out_of_scope_patterns_are_counted_like_the_explorer_does(self) -> None:
        out = {**_ap("dynamodb", "DDB-AP-2"), "in_scope": False}
        report = _report([_ap("dynamodb", "DDB-AP-1"), out])
        assert access_pattern_facts(report)["owned_pattern_count"] == 2  # type: ignore[index]

    def test_ignores_malformed_entries(self) -> None:
        report = {
            "query_groups": ["x", {"access_patterns": "no"}, {"access_patterns": [None, 3, {}]}]
        }
        facts = access_pattern_facts(report)
        assert facts is not None
        assert facts["owned_pattern_count"] == 0


class TestInTheFactsBlock:
    def test_compute_report_facts_carries_the_totals_and_validates(self) -> None:
        report = _report([_ap("dynamodb", "DDB-AP-1")])
        facts = compute_report_facts(report, waves=[])
        assert facts["access_pattern_totals"]["owned_pattern_count"] == 1
        parsed = ReportFacts.model_validate(facts)
        assert isinstance(parsed.access_pattern_totals, AccessPatternFacts)

    def test_a_report_with_no_query_groups_has_none(self) -> None:
        assert compute_report_facts({}, waves=[])["access_pattern_totals"] is None

    def test_a_facts_block_written_before_1_26_still_loads(self) -> None:
        old = compute_report_facts(_report([_ap("dynamodb", "DDB-AP-1")]), waves=[])
        del old["access_pattern_totals"]
        assert ReportFacts.model_validate(old).access_pattern_totals is None
