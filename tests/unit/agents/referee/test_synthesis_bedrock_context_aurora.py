"""#478/#157: generate_executive_summary's Bedrock context must not

- rank "Utility and session statements"/"Table not identified by the
  collector" as a top query group (neither is a real access pattern), or
- call an Aurora engine's group "patterns" or pair its workload with an
  "access_patterns" count while its own schema design has not run yet (there
  is nothing designed to count) -- but once it HAS run (#157: real,
  script-derived access patterns), Aurora is counted exactly like any other
  designed engine, with no special-casing.

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


# Aurora's schema design HAS run (#157): its access_patterns are derived 1:1
# from its assigned queries, so the count is consistent with its workload --
# no special-casing needed against dynamodb's entry below.
RANKING = [
    {
        "target": "aurora_mysql",
        "confidence_score": 70,
        "assigned_queries": 53,
        "workload_percent": 49.5,
        "schema_design_available": True,
        "target_tables": 21,
        "access_patterns": 53,
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

# A second Aurora engine with no schema design at all yet -- its workload is
# routed, but there is nothing designed to count a pattern against.
NOT_YET_DESIGNED_RANKING = [
    {
        "target": "aurora_postgresql",
        "confidence_score": 60,
        "assigned_queries": 12,
        "workload_percent": 100.0,
        "schema_design_available": False,
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

NOT_YET_DESIGNED_GROUPS = [
    {
        "group_name": "wordpress.wp_users",
        "engines": ["aurora_postgresql"],
        "access_patterns": [{"engine": "aurora_postgresql", "query_ids": ["q6"]}],
        "total_design_rps": 2.0,
    },
]


def _generate(ranking=RANKING, query_groups=QUERY_GROUPS) -> dict:
    _CapturingAgent.last_prompt = ""
    with (
        patch("strands.Agent", _CapturingAgent),
        patch("strands.models.bedrock.BedrockModel", lambda *a, **k: object()),
    ):
        synthesis_report.generate_executive_summary(
            deterministic_summary="fallback",
            ranking=ranking,
            query_groups=query_groups,
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


def test_designed_aurora_group_counts_as_patterns_like_any_other_engine() -> None:
    """#157: once Aurora's schema design has run and derived real access
    patterns, its group is counted the same way dynamodb's is -- "patterns",
    not "queries"."""
    context = _generate()
    aurora_group = next(g for g in context["top_query_groups"] if g["name"] == "wordpress.wp_posts")
    assert "patterns" in aurora_group
    assert "queries" not in aurora_group

    dynamodb_group = next(g for g in context["top_query_groups"] if g["name"] == "Option reads")
    assert "patterns" in dynamodb_group
    assert "queries" not in dynamodb_group


def test_not_yet_designed_aurora_group_still_counts_as_queries() -> None:
    """An Aurora engine with no schema design at all yet has nothing designed
    to call a "pattern" -- its group still reads as plain queries."""
    context = _generate(NOT_YET_DESIGNED_RANKING, NOT_YET_DESIGNED_GROUPS)
    group = next(g for g in context["top_query_groups"] if g["name"] == "wordpress.wp_users")
    assert "queries" in group
    assert "patterns" not in group


def test_designed_aurora_engine_carries_its_own_access_pattern_count() -> None:
    """#157: a designed Aurora engine's access_patterns count is consistent
    with its own query count, so it is no longer withheld as a special case."""
    context = _generate()
    aurora_entry = next(e for e in context["engines"] if e["engine"] == "aurora_mysql")
    assert aurora_entry["queries"] == 53
    assert aurora_entry["access_patterns"] == 53

    dynamodb_entry = next(e for e in context["engines"] if e["engine"] == "dynamodb")
    assert dynamodb_entry["access_patterns"] == 54


def test_not_yet_designed_aurora_engine_has_no_access_pattern_count() -> None:
    """No schema design at all means no target_tables/access_patterns entries
    -- there is nothing designed to report yet."""
    context = _generate(NOT_YET_DESIGNED_RANKING, NOT_YET_DESIGNED_GROUPS)
    entry = next(e for e in context["engines"] if e["engine"] == "aurora_postgresql")
    assert entry["queries"] == 12
    assert "access_patterns" not in entry
    assert "target_tables" not in entry
