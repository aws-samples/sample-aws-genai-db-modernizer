"""#478: generate_executive_summary's Bedrock context must not

- rank "Utility and session statements"/"Table not identified by the
  collector" as a top query group (neither is a real access pattern), or
- count a relational engine's queries as "patterns" (Aurora has none; #157
  adds real ones later) or pair its workload with a bare "access_patterns: 0"
  that reads as contradicting its own query count.

Same capture technique as test_synthesis_executive_summary_no_schema_design.py:
patch the ``strands`` ``Agent``/``BedrockModel`` the function imports
internally and inspect the JSON context embedded in the captured prompt.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.agents.referee import synthesis_report
from src.agents.referee.synthesis_report import (
    UNRESOLVED_TABLE_GROUP_LABEL,
    UTILITY_GROUP_LABEL,
)


class _CapturingAgent:
    last_prompt: str = ""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __call__(self, prompt: str):
        type(self).last_prompt = prompt
        return "A concise executive summary that is long enough to pass the length gate."


RANKING = [
    {
        "target": "aurora_mysql",
        "confidence_score": 70,
        "assigned_queries": 53,
        "workload_percent": 49.5,
        "schema_design_available": True,
        "target_tables": 21,
        "access_patterns": 0,
    },
    {
        "target": "dynamodb",
        "confidence_score": 90,
        "assigned_queries": 54,
        "workload_percent": 50.5,
        "schema_design_available": True,
        "target_tables": 17,
        "access_patterns": 54,
    },
]

QUERY_GROUPS = [
    {
        "group_name": UNRESOLVED_TABLE_GROUP_LABEL,
        "engines": ["aurora_mysql"],
        "access_patterns": [{"engine": "aurora_mysql", "query_ids": ["q1"]}],
        "total_design_rps": 999.0,
    },
    {
        "group_name": UTILITY_GROUP_LABEL,
        "engines": ["aurora_mysql"],
        "access_patterns": [{"engine": "aurora_mysql", "query_ids": ["q2"]}],
        "total_design_rps": 998.0,
    },
    {
        "group_name": "wordpress.wp_posts",
        "engines": ["aurora_mysql"],
        "access_patterns": [{"engine": "aurora_mysql", "query_ids": ["q3"]}],
        "total_design_rps": 4.5,
    },
    {
        "group_name": "Option reads",
        "engines": ["dynamodb"],
        "access_patterns": [
            {"engine": "dynamodb", "query_ids": ["q4"]},
            {"engine": "dynamodb", "query_ids": ["q5"]},
        ],
        "total_design_rps": 10.0,
    },
]


def _generate() -> dict:
    _CapturingAgent.last_prompt = ""
    with (
        patch("strands.Agent", _CapturingAgent),
        patch("strands.models.bedrock.BedrockModel", lambda *a, **k: object()),
    ):
        synthesis_report.generate_executive_summary(
            deterministic_summary="fallback",
            ranking=RANKING,
            query_groups=QUERY_GROUPS,
            tco={},
            risks={"overall_risk_level": "LOW", "risks": []},
            table_mappings=[],
            trade_offs=[],
        )
    prompt = _CapturingAgent.last_prompt
    assert prompt, "Agent was not invoked / prompt not captured"
    start = prompt.index("{")
    end = prompt.rindex("}") + 1
    result: dict = json.loads(prompt[start:end])
    return result


def test_blame_labels_never_appear_in_top_query_groups() -> None:
    context = _generate()
    names = [g["name"] for g in context["top_query_groups"]]
    assert UTILITY_GROUP_LABEL not in names
    assert UNRESOLVED_TABLE_GROUP_LABEL not in names
    assert "wordpress.wp_posts" in names


def test_relational_group_counts_as_queries_not_patterns() -> None:
    context = _generate()
    aurora_group = next(g for g in context["top_query_groups"] if g["name"] == "wordpress.wp_posts")
    assert "queries" in aurora_group
    assert "patterns" not in aurora_group

    dynamodb_group = next(g for g in context["top_query_groups"] if g["name"] == "Option reads")
    assert "patterns" in dynamodb_group
    assert "queries" not in dynamodb_group


def test_relational_engine_workload_has_no_contradicting_access_pattern_count() -> None:
    context = _generate()
    aurora_entry = next(e for e in context["engines"] if e["engine"] == "aurora_mysql")
    assert aurora_entry["queries"] == 53
    assert "access_patterns" not in aurora_entry

    dynamodb_entry = next(e for e in context["engines"] if e["engine"] == "dynamodb")
    assert dynamodb_entry["access_patterns"] == 54
