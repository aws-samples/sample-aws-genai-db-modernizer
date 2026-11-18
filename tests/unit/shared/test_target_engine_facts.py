# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The target engines, as one report fact (#2345, ADR-032).

The results page lists the engines that own queries, plus the cache layer. The
assessments list shows the same engines, so both read ``facts.target_engines``
rather than each deciding from the engines' roles.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, target_engine_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


class TestTargetEngineFacts:
    def test_the_engines_that_own_queries_in_the_final_order(self) -> None:
        counts = {"aurora_mysql": 40, "dynamodb": 20}
        assert target_engine_facts({}, counts) == ["aurora_mysql", "dynamodb"]

    def test_the_cache_layer_engine_is_added_once(self) -> None:
        report = {"cache_overlay": {"engine": "elasticache"}}
        assert target_engine_facts(report, {"dynamodb": 20}) == ["dynamodb", "elasticache"]
        assert target_engine_facts(report, {"dynamodb": 20, "elasticache": 3}) == [
            "dynamodb",
            "elasticache",
        ]

    def test_a_cache_engine_in_the_counts_is_dropped_when_there_is_an_overlay(self) -> None:
        report = {"cache_overlay": {"engine": "elasticache"}}
        counts = {"elasticache": 3, "dynamodb": 20}
        assert target_engine_facts(report, counts) == ["dynamodb", "elasticache"]

    def test_a_report_with_no_overlay_adds_no_cache_engine(self) -> None:
        assert target_engine_facts({"engines_in_use": ["elasticache"]}, {"dynamodb": 1}) == [
            "dynamodb"
        ]

    def test_no_query_and_no_overlay_is_no_engine(self) -> None:
        assert target_engine_facts({}, {}) == []

    def test_an_engine_with_a_failed_design_that_owns_queries_stays(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        for design in (report.get("schema_designs") or {}).values():
            if isinstance(design, dict):
                design["status"] = "failed"
        facts = compute_report_facts(report)
        assert set(facts["engine_query_counts"]) <= set(facts["target_engines"])


class TestInTheFactsBlock:
    def test_compute_report_facts_states_them_and_the_contract_accepts_them(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        facts = compute_report_facts(report)
        assert facts["target_engines"] == target_engine_facts(report, facts["engine_query_counts"])
        ReportFacts.model_validate(facts)

    def test_an_old_block_gains_them_and_keeps_the_rest(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        expected = stored.pop("target_engines")
        stored["engine_count"] = 99  # a stored figure is never recomputed
        report["facts"] = stored
        facts = report_facts(report)
        assert facts["target_engines"] == expected
        assert facts["engine_count"] == 99

    def test_a_block_that_stored_none_gains_them(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        expected = stored["target_engines"]
        stored["target_engines"] = None
        report["facts"] = stored
        assert report_facts(report)["target_engines"] == expected
