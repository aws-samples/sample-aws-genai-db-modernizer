"""The executive summary is told the engine scope and the queries with no engine (#1574, item 5).

The skill had no rule for a scoped job and its input carried neither the scope nor how
many queries no engine in it can serve, so a model summary could not state them (the
deterministic summary already does). Both modes read the same request, so the input is
built once (``prepare_synthesis_llm_input``) and the rule is in the one skill file.
"""

from __future__ import annotations

from pathlib import Path

from src.agents.referee.synthesis_handler import _executive_brief_input

SKILL = Path(__file__).resolve().parents[4] / "src" / "skills" / "executive-summary.md"


def _facts(**extra: object) -> dict:
    return {
        "projected_monthly_cost_usd": 168.23,
        "engine_count": 2,
        "engines": [{"engine": "aurora_postgresql", "workload_percent": 56.7}],
        "executive_brief": {"projected_monthly_cost_whole_usd": 168},
        **extra,
    }


def test_the_brief_carries_the_scope_and_the_queries_with_no_engine() -> None:
    brief = _executive_brief_input(
        _facts(
            engine_scope=["aurora_postgresql", "dynamodb"],
            no_engine_in_scope_query_count=42,
            query_total_count=1696,
        )
    )
    assert brief["engine_scope"] == ["aurora_postgresql", "dynamodb"]
    assert brief["no_engine_in_scope_query_count"] == 42
    assert brief["query_total_count"] == 1696


def test_an_unscoped_brief_says_so() -> None:
    brief = _executive_brief_input(_facts(query_total_count=1654))
    assert brief["engine_scope"] is None
    assert brief["no_engine_in_scope_query_count"] == 0


def test_the_skill_states_the_scope_rule_and_the_unservable_rule() -> None:
    text = " ".join(SKILL.read_text().split())
    assert "`executive_brief.engine_scope`" in text
    assert "never name an engine outside it" in text
    assert "`executive_brief.no_engine_in_scope_query_count`" in text
    assert "`executive_brief.query_total_count`" in text
