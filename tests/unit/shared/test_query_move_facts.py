# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How many queries move to a new engine, as report facts (#1020, ADR-032).

The executive report's "Queries moved" tile is a total, so it is a fact:
synthesis counts it once, from the ranking's in-scope query counts and each
engine's role, into ``facts.in_scope_query_count`` and
``facts.moving_query_count``. A query on a migration target or a search read
model leaves the source engine; one on the retained core stays; the cache layer
owns none.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, query_move_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _engine(engine: str, role: str, cached: int | None = None) -> dict[str, Any]:
    return {"engine": engine, "role": role, "cached_queries": cached}


def _ranking(*rows: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"target": e, "assigned_queries_in_scope": n, "assigned_queries": n} for e, n in rows]


class TestQueryMoveFacts:
    def test_a_retained_core_keeps_its_queries(self) -> None:
        report = {"ranking": _ranking(("aurora_mysql", 70), ("dynamodb", 37))}
        engines = [_engine("aurora_mysql", "Retained"), _engine("dynamodb", "Migration target")]
        assert query_move_facts(report, engines) == {
            "in_scope_query_count": 107,
            "moving_query_count": 37,
        }

    def test_the_cache_layer_owns_no_query(self) -> None:
        report = {"ranking": _ranking(("aurora_mysql", 70), ("elasticache", 20))}
        engines = [
            _engine("aurora_mysql", "Retained"),
            _engine("elasticache", "Cache layer", cached=20),
        ]
        assert query_move_facts(report, engines) == {
            "in_scope_query_count": 70,
            "moving_query_count": 0,
        }

    def test_a_search_read_model_and_every_migration_target_move(self) -> None:
        report = {
            "ranking": _ranking(
                ("aurora_postgresql", 44), ("dynamodb", 51), ("opensearch", 5), ("documentdb", 9)
            )
        }
        engines = [
            # A heterogeneous source: the Aurora engine picked is a migration target too.
            _engine("aurora_postgresql", "Migration target"),
            _engine("dynamodb", "Migration target"),
            _engine("opensearch", "Search read model"),
            _engine("documentdb", "Assessed"),
        ]
        facts = query_move_facts(report, engines)
        assert facts == {"in_scope_query_count": 109, "moving_query_count": 100}

    def test_an_evaluated_engine_and_a_missing_count_add_nothing(self) -> None:
        report = {"ranking": [{"target": "dynamodb"}, {"target": "documentdb", "x": 1}]}
        engines = [_engine("dynamodb", "Migration target"), _engine("documentdb", "Evaluated")]
        assert query_move_facts(report, engines) == {
            "in_scope_query_count": 0,
            "moving_query_count": 0,
        }

    def test_the_in_scope_count_wins_over_the_raw_assignment(self) -> None:
        report = {
            "ranking": [
                {"target": "dynamodb", "assigned_queries": 40, "assigned_queries_in_scope": 37}
            ]
        }
        engines = [_engine("dynamodb", "Migration target")]
        assert query_move_facts(report, engines)["moving_query_count"] == 37

    def test_empty_input(self) -> None:
        assert query_move_facts({}, []) == {"in_scope_query_count": 0, "moving_query_count": 0}


class TestInTheFactsBlock:
    def test_compute_report_facts_carries_them_and_the_contract_accepts_them(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        facts = compute_report_facts(report)
        engines = facts["engines"]
        assert {k: facts[k] for k in ("in_scope_query_count", "moving_query_count")} == (
            query_move_facts(report, engines)
        )
        assert facts["moving_query_count"] > 0
        assert facts["moving_query_count"] <= facts["in_scope_query_count"]
        ReportFacts.model_validate(facts)

    def test_a_block_written_before_1_28_gains_them_and_keeps_the_rest(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        expected = {k: stored.pop(k) for k in ("in_scope_query_count", "moving_query_count")}
        stored["engine_count"] = 99  # a stored figure is never recomputed
        report["facts"] = stored
        facts = report_facts(report)
        assert {k: facts[k] for k in expected} == expected
        assert facts["engine_count"] == 99

    def test_a_stored_count_is_read_as_written(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        stored = compute_report_facts(report)
        stored["moving_query_count"] = 3
        report["facts"] = stored
        assert report_facts(report)["moving_query_count"] == 3


def test_both_reports_print_the_stored_counts() -> None:
    """The engineering report states the executive report's "Queries moved" tile,
    from the same stored fact."""
    from tests.executive_pdf import engineering_text, executive_text

    report = json.loads((FIXTURES / "wordpress_report.json").read_text())
    report["facts"] = compute_report_facts(report)
    report["facts"].update(in_scope_query_count=99, moving_query_count=42)
    assert "Queries moved: 42 of 99 queries move to a new engine" in engineering_text(report)
    assert "42 of 99 queries move to a new engine" in executive_text(report)


def test_a_saved_1_26_report_gains_the_counts_when_read() -> None:
    """A report.json saved at contract 1.26 (facts without the move counts), loaded
    from disk: both readers fill the counts from its ranking, and it renders."""
    from src.report.executive_report import render_executive_report_pdf
    from tests.executive_pdf import body_text

    report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
    assert report["contract_version"] == "1.26"
    assert "moving_query_count" not in report["facts"]
    facts = report_facts(report)
    assert (facts["in_scope_query_count"], facts["moving_query_count"]) == (107, 37)
    assert facts["engine_count"] == report["facts"]["engine_count"]
    text = body_text(render_executive_report_pdf(report), report["database_name"])
    assert "37 of 107 queries move to a new engine" in text
