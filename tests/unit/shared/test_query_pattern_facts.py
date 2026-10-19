"""The queries and the distinct query patterns they make, as report facts (#1146, ADR-032).

Synthesis groups the in-scope queries into distinct query patterns in ``query_pattern_groups``
(each query kept with its id, frequency and engine) and the facts layer counts them once
into ``facts.query_pattern_counts``, over the same queries as ``in_scope_query_count``, so the
deliverables' "N queries in M distinct query patterns" agrees with every other query count. A report
written before #1146 has no grouping: it still loads, and its figures are None.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import render_executive_report_pdf
from src.report.facts import report_facts
from src.report.query_patterns import (
    QUERY_PATTERN_DEFINITION,
    query_pattern_count_text,
    query_patterns_md,
)
from src.shared.report_facts import query_move_facts, query_pattern_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _pdf_text(report: dict[str, Any]) -> str:
    pages = PdfReader(io.BytesIO(render_executive_report_pdf(report))).pages
    return " ".join(" ".join((p.extract_text() or "").split()) for p in pages)


def _engine(engine: str, role: str, cached: int | None = None) -> dict[str, Any]:
    return {"engine": engine, "role": role, "cached_queries": cached}


def _group(pid: str, *members: tuple[str, str]) -> dict[str, Any]:
    return {
        "pattern_id": pid,
        "operation": "read",
        "tables": ["t"],
        "returned_columns": [],
        "frequency_per_hour": 60.0 * len(members),
        "members": [{"query_id": q, "frequency_per_hour": 60.0, "engine": e} for q, e in members],
    }


ENGINES = [
    _engine("aurora_mysql", "Retained"),
    _engine("dynamodb", "Migration target"),
    _engine("elasticache", "Cache layer", cached=3),
]


class TestQueryPatternFacts:
    def test_counts_queries_patterns_and_repeated_ones(self) -> None:
        report = {
            "query_pattern_groups": [
                _group("PAT-1", ("a", "dynamodb"), ("b", "dynamodb"), ("c", "dynamodb")),
                _group("PAT-2", ("d", "aurora_mysql")),
                _group("PAT-3", ("e", "aurora_mysql"), ("f", "aurora_mysql")),
            ]
        }
        assert query_pattern_facts(report, ENGINES) == {
            "query_count": 6,
            "pattern_count": 3,
            "repeated_pattern_count": 2,
            "repeated_query_count": 5,
            "split_pattern_count": 0,
        }

    def test_a_pattern_split_across_engines_counts_once(self) -> None:
        report = {
            "query_pattern_groups": [_group("PAT-1", ("a", "dynamodb"), ("b", "aurora_mysql"))]
        }
        facts = query_pattern_facts(report, ENGINES)
        assert facts is not None
        assert (facts["pattern_count"], facts["split_pattern_count"]) == (1, 1)

    def test_only_the_owner_engines_queries_count(self) -> None:
        """The cache layer owns no query (#296), and an engine not in the plan has none
        either: the same queries as ``in_scope_query_count``."""
        report = {
            "query_pattern_groups": [
                _group("PAT-1", ("a", "elasticache"), ("b", "elasticache")),
                _group("PAT-2", ("c", "documentdb")),
                _group("PAT-3", ("d", "dynamodb"), ("e", "elasticache")),
            ]
        }
        assert query_pattern_facts(report, ENGINES) == {
            "query_count": 1,
            "pattern_count": 1,
            "repeated_pattern_count": 0,
            "repeated_query_count": 0,
            "split_pattern_count": 0,
        }

    def test_the_query_count_is_the_in_scope_query_count(self) -> None:
        report = {
            "ranking": [
                {"target": "aurora_mysql", "assigned_queries_in_scope": 2},
                {"target": "dynamodb", "assigned_queries_in_scope": 3},
                {"target": "elasticache", "assigned_queries_in_scope": 2},
            ],
            "query_pattern_groups": [
                _group("PAT-1", ("a", "dynamodb"), ("b", "dynamodb"), ("c", "dynamodb")),
                _group("PAT-2", ("d", "aurora_mysql"), ("e", "aurora_mysql")),
                _group("PAT-3", ("f", "elasticache"), ("g", "elasticache")),
            ],
        }
        facts = query_pattern_facts(report, ENGINES)
        assert facts is not None
        assert facts["query_count"] == query_move_facts(report, ENGINES)["in_scope_query_count"]

    def test_no_groups_is_none_and_empty_groups_count_zero(self) -> None:
        assert query_pattern_facts({}, ENGINES) is None
        assert query_pattern_facts({"query_pattern_groups": []}, ENGINES) == {
            "query_count": 0,
            "pattern_count": 0,
            "repeated_pattern_count": 0,
            "repeated_query_count": 0,
            "split_pattern_count": 0,
        }


class TestOldReports:
    def test_a_1_26_report_loads_and_has_no_access_pattern_figures(self) -> None:
        report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        assert "query_pattern_counts" not in report["facts"]
        assert report_facts(report)["query_pattern_counts"] is None
        assert query_pattern_count_text(report) is None
        assert query_patterns_md(report) == []
        assert not re.search(r"\d+ quer(y|ies) in \d+ distinct query pattern", _pdf_text(report))
        assert "distinct query patterns:" not in render_engineering_report_md(report).lower()

    def test_a_1_13_report_still_validates_and_renders(self) -> None:
        report = json.loads((FIXTURES / "discourse_merged_wave_report.json").read_text())
        loaded = SynthesisOutputContract.model_validate(report)
        assert loaded.query_pattern_groups is None and loaded.facts is None
        assert report_facts(report)["query_pattern_counts"] is None
        assert "distinct query patterns:" not in render_engineering_report_md(report).lower()

    def test_a_report_with_no_facts_block_computes_none(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        assert "facts" not in report
        assert report_facts(report)["query_pattern_counts"] is None

    def test_a_block_with_the_figures_is_read_as_stored(self) -> None:
        report = json.loads((FIXTURES / "wordpress_1_26_report.json").read_text())
        stored = {
            "query_count": 9,
            "pattern_count": 4,
            "repeated_pattern_count": 2,
            "repeated_query_count": 7,
            "split_pattern_count": 0,
        }
        report["facts"]["query_pattern_counts"] = stored
        assert report_facts(report)["query_pattern_counts"] == stored
        assert query_pattern_count_text(report) == "9 queries in 4 distinct query patterns"
        line, _ = query_patterns_md(report)
        assert line.startswith(
            "**Distinct query patterns:** 9 queries in 4 distinct query patterns. "
        )
        assert "2 of the patterns are made by more than one query (7 queries in all)." in line
        md = render_engineering_report_md(report)
        assert line in md
        # The definition is stated once, in the engineering report only.
        assert md.count(QUERY_PATTERN_DEFINITION) == 1
        assert QUERY_PATTERN_DEFINITION not in _pdf_text(report)
        assert "9 queries in 4 distinct query patterns" in _pdf_text(report)

    def test_one_query_one_pattern_agree_in_number(self) -> None:
        report = {
            "facts": {
                "engines": [],
                "access_pattern_totals": None,
                "moving_query_count": 0,
                "resolved_risks_by_severity": {},
                "query_pattern_counts": {
                    "query_count": 1,
                    "pattern_count": 1,
                    "repeated_pattern_count": 0,
                    "repeated_query_count": 0,
                    "split_pattern_count": 0,
                },
            }
        }
        assert query_pattern_count_text(report) == "1 query in 1 distinct query pattern"
        assert query_patterns_md(report)[0].endswith("Every query is a pattern of its own.")
