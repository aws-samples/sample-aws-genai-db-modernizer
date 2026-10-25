"""An architectural pattern's example names only the engines the plan runs (#1574, item 1).

The examples were static text naming DynamoDB, OpenSearch and ElastiCache whatever the
plan was, so a job scoped to DynamoDB and Aurora PostgreSQL showed OpenSearch and
ElastiCache in the gate block, the reality-check output and ``report.json``. Each
example is now written from the pattern's own ``applies_to`` engines.
"""

from __future__ import annotations

import pytest

from src.agents.referee.reality_check_patterns import patterns_for_assignment
from src.agents.referee.synthesis_grounding import engine_mentions
from src.shared.engine_names import display_engine


def _qas(**counts: int) -> list[dict]:
    return [
        {"query_id": f"{engine}-{i}", "assigned_engine": engine, "in_scope": True}
        for engine, n in counts.items()
        for i in range(n)
    ]


def _engines(applies_to: dict) -> set[str]:
    out: set[str] = set()
    for value in applies_to.values():
        out.update(value if isinstance(value, list) else [value])
    return out


PLANS = [
    # The scoped discourse run: Aurora PostgreSQL primary, DynamoDB second (event sync,
    # and CQRS with DynamoDB as the key-value read side).
    {"aurora_postgresql": 938, "dynamodb": 716},
    # A relational primary with a search read model (CQRS and a materialized view).
    {"aurora_mysql": 60, "opensearch": 10},
    # Three engines: polyglot persistence.
    {"dynamodb": 50, "aurora_mysql": 20, "documentdb": 5},
    # A relational primary with a cache read side.
    {"aurora_mysql": 40, "elasticache": 12},
]


@pytest.mark.parametrize("counts", PLANS, ids=lambda c: "+".join(c))
def test_every_example_names_only_engines_the_pattern_applies_to(counts: dict) -> None:
    patterns = patterns_for_assignment(_qas(**counts))
    assert patterns, counts
    for p in patterns:
        named = {e for e, _, _ in engine_mentions(p["example"])}
        assert named <= _engines(p["applies_to"]), (p["name"], p["example"])
        # And within the plan: never an engine with no query.
        assert named <= set(counts), (p["name"], p["example"])


def test_the_scoped_run_names_neither_opensearch_nor_elasticache() -> None:
    patterns = patterns_for_assignment(_qas(aurora_postgresql=938, dynamodb=716))
    text = " ".join(p["example"] for p in patterns)
    assert "OpenSearch" not in text and "ElastiCache" not in text
    assert display_engine("aurora_postgresql") in text


def test_each_example_names_the_engines_of_its_pattern() -> None:
    patterns = patterns_for_assignment(_qas(aurora_mysql=60, opensearch=10))
    view = next(p for p in patterns if p["name"].startswith("Materialized View"))
    assert display_engine("aurora_mysql") in view["example"]
    assert display_engine("opensearch") in view["example"]
