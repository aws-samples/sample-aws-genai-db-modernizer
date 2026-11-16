# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each engine's final query count, as a report fact (#2358, ADR-032).

The results page read each engine's count from ``reality_check.after_distribution``,
the reality check's own result, so a later assignment version (an ``--auto`` pick, a
post-schema reroute) left it showing older counts than the reports. Synthesis now
counts them once, from the ranking, which follows the final version, into
``facts.engine_query_counts``; every renderer, the UI included, reads that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, engine_query_count_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _engine(engine: str, role: str, cached: int | None = None) -> dict[str, Any]:
    return {"engine": engine, "role": role, "cached_queries": cached}


class TestEngineQueryCountFacts:
    def test_counts_each_owner_engine_from_the_ranking(self) -> None:
        report = {
            "ranking": [
                {"target": "aurora_postgresql", "assigned_queries": 797},
                {"target": "dynamodb", "assigned_queries": 694},
                {"target": "documentdb", "assigned_queries": 163},
            ]
        }
        engines = [
            _engine("aurora_postgresql", "Retained"),
            _engine("dynamodb", "Migration target"),
            _engine("documentdb", "Migration target"),
        ]
        assert engine_query_count_facts(report, engines) == {
            "aurora_postgresql": 797,
            "dynamodb": 694,
            "documentdb": 163,
        }

    def test_the_final_ranking_wins_over_the_reality_check(self) -> None:
        report = {
            "reality_check": {"after_distribution": {"dynamodb": 707, "documentdb": 151}},
            "ranking": [
                {"target": "dynamodb", "assigned_queries": 694},
                {"target": "documentdb", "assigned_queries": 163},
            ],
        }
        engines = [_engine("dynamodb", "Migration target"), _engine("documentdb", "Assessed")]
        assert engine_query_count_facts(report, engines) == {"dynamodb": 694, "documentdb": 163}

    def test_the_cache_layer_and_an_engine_with_no_query_are_left_out(self) -> None:
        report = {
            "ranking": [
                {"target": "dynamodb", "assigned_queries": 30},
                {"target": "elasticache", "assigned_queries": 12},
                {"target": "opensearch", "assigned_queries": 0},
            ]
        }
        engines = [
            _engine("dynamodb", "Migration target"),
            _engine("elasticache", "Cache layer", cached=12),
            _engine("opensearch", "Search read model"),
        ]
        assert engine_query_count_facts(report, engines) == {"dynamodb": 30}

    def test_the_in_scope_count_wins_over_the_raw_assignment(self) -> None:
        report = {
            "ranking": [
                {"target": "dynamodb", "assigned_queries": 40, "assigned_queries_in_scope": 37}
            ]
        }
        assert engine_query_count_facts(report, [_engine("dynamodb", "Migration target")]) == {
            "dynamodb": 37
        }

    def test_empty_input(self) -> None:
        assert engine_query_count_facts({}, []) == {}


class TestInTheFactsBlock:
    def test_compute_report_facts_carries_them_and_the_contract_accepts_them(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        facts = compute_report_facts(report)
        assert facts["engine_query_counts"] == engine_query_count_facts(report, facts["engines"])
        assert sum(facts["engine_query_counts"].values()) == facts["in_scope_query_count"]
        ReportFacts.model_validate(facts)

    def test_a_report_whose_final_version_differs_from_the_reality_check(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        engines = {r["target"]: r["assigned_queries"] for r in report["ranking"]}
        report["reality_check"]["after_distribution"] = {e: n + 5 for e, n in engines.items()}
        facts = compute_report_facts(report)
        for engine, n in facts["engine_query_counts"].items():
            assert n == engines[engine]
            assert n != report["reality_check"]["after_distribution"][engine]

    def test_an_old_block_gains_them_and_keeps_the_rest(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        expected = stored.pop("engine_query_counts")
        stored["engine_count"] = 99  # a stored figure is never recomputed
        report["facts"] = stored
        facts = report_facts(report)
        assert facts["engine_query_counts"] == expected
        assert facts["engine_count"] == 99

    def test_a_saved_1_26_report_gains_them_when_read(self) -> None:
        report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        assert "engine_query_counts" not in report["facts"]
        counts = report_facts(report)["engine_query_counts"]
        assert sum(counts.values()) == 107
        ReportFacts.model_validate(report_facts(report))

    def test_a_stored_count_is_read_as_written(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        stored["engine_query_counts"] = {"dynamodb": 3}
        report["facts"] = stored
        assert report_facts(report)["engine_query_counts"] == {"dynamodb": 3}
