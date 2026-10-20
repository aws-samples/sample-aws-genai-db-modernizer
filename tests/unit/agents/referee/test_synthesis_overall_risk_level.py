"""``overall_risk_level`` is the highest severity among the open risks (#248).

The old rule was a HIGH-count threshold: three HIGH risks for HIGH, one or two for
MEDIUM, and LOW otherwise, so a register of only MEDIUM risks rated LOW. The rule is now
any CRITICAL -> CRITICAL, any HIGH -> HIGH, any MEDIUM -> MEDIUM, else LOW; resolved
risks (``resolved_risks``) do not count.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment, overall_risk_level


def _risks(*severities: str) -> list[dict]:
    return [{"risk_id": f"RISK-{i:03d}", "severity": s} for i, s in enumerate(severities, 1)]


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        ((), "LOW"),
        (("LOW",), "LOW"),
        (("MEDIUM",), "MEDIUM"),
        (("MEDIUM",) * 8, "MEDIUM"),
        (("LOW", "MEDIUM"), "MEDIUM"),
        (("HIGH",), "HIGH"),
        (("MEDIUM", "HIGH"), "HIGH"),
        (("HIGH", "HIGH"), "HIGH"),
        (("HIGH", "CRITICAL", "MEDIUM"), "CRITICAL"),
        (("medium",), "MEDIUM"),
    ],
)
def test_highest_open_severity(severities: tuple[str, ...], expected: str) -> None:
    assert overall_risk_level(_risks(*severities)) == expected


def _data(unsupported: int, high_anti_patterns: int = 0) -> SynthesisData:
    schema = {
        "unsupported_patterns": [
            {"query_ids": [f"u{i}"], "pattern_type": "aggregation", "recommendation": "Count."}
            for i in range(unsupported)
        ],
        # Covers every anti-pattern query, so those HIGH risks are resolved.
        "access_patterns": [{"query_ids": [f"a{i}"]} for i in range(high_anti_patterns)],
    }
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "hot-partition",
                    "description": f"Hot key {i}.",
                    "query_ids": [f"a{i}"],
                    "severity_weight": 0.9,
                }
                for i in range(high_anti_patterns)
            ]
        }
    }
    data = SynthesisData(job_id="j", database_name="db")
    data.engines["dynamodb"] = EngineArtifacts("dynamodb", analysis=analysis, schema_design=schema)
    return data


@pytest.mark.parametrize(("unsupported", "expected"), [(0, "LOW"), (1, "MEDIUM"), (8, "MEDIUM")])
def test_risk_assessment_rates_medium_risks_medium(unsupported: int, expected: str) -> None:
    out = build_risk_assessment(_data(unsupported))
    assert len(out["risks"]) == unsupported
    assert out["overall_risk_level"] == expected


def test_resolved_high_risks_do_not_count() -> None:
    out = build_risk_assessment(_data(unsupported=2, high_anti_patterns=4))
    assert [r["severity"] for r in out["resolved_risks"]] == ["HIGH"] * 4
    assert out["overall_risk_level"] == "MEDIUM"


def _documentdb_data(schema: dict) -> SynthesisData:
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "hot-partition",
                    "description": "Hot key.",
                    "query_ids": ["q1"],
                    "severity_weight": 0.9,
                }
            ]
        }
    }
    data = SynthesisData(job_id="j", database_name="db")
    data.engines["documentdb"] = EngineArtifacts(
        "documentdb", analysis=analysis, schema_design=schema
    )
    return data


def test_coverage_gap_reads_a_documentdb_index_as_the_scope_check_does() -> None:
    # #1213: one coverage rule (scope.covered_query_ids): a DocumentDB query named only by
    # collections[].indexes[].source_query_ids is served, so its risk is resolved, and
    # the same query unnamed (or only on an out-of-scope pattern) stays open.
    served = {
        "collections": [
            {"collection_name": "c", "indexes": [{"index_name": "i", "source_query_ids": ["q1"]}]}
        ]
    }
    out = build_risk_assessment(_documentdb_data(served))
    assert [r["severity"] for r in out["resolved_risks"]] == ["HIGH"]
    assert out["risks"] == []

    unserved = {
        "access_patterns": [{"pattern_id": "p", "source_query_ids": ["q1"], "in_scope": False}]
    }
    out = build_risk_assessment(_documentdb_data(unserved))
    assert out["resolved_risks"] == []
    assert [r["severity"] for r in out["risks"]] == ["HIGH"]
