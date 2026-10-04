"""Regression test: reality-check executive-summary output must not depend on
Python's per-process string hash seed (PYTHONHASHSEED).

``_generate_executive_summary`` used to build its ``engines_eliminated`` field
with ``list(set(eliminated_engines))``. Since ``set`` iteration order for
strings depends on PYTHONHASHSEED, two runs over identical input could produce
``engines_eliminated`` in a different order, which propagates into reports and
other downstream artifacts (see issue #186). The fix uses
``sorted(set(eliminated_engines))`` instead.

This test runs the function twice, in separate subprocesses with different
PYTHONHASHSEED values, over a fixed input that eliminates three engines, and
asserts the two runs produce byte-identical output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

# Child process script: fakes out `strands` so no real Bedrock/network call is
# made, spies on `json.dumps` inside reality_check_handler to capture the
# `context` dict passed to the LLM prompt (which is where the previously
# nondeterministic `engines_eliminated` list lived), calls
# `_generate_executive_summary` with a fixed input that eliminates three
# engines, and prints the captured list as the last line of stdout.
_CHILD_SCRIPT = textwrap.dedent("""
    import json
    import sys
    import types

    # --- Fake out strands so no real LLM call happens ------------------------
    fake_strands = types.ModuleType("strands")
    fake_strands_models = types.ModuleType("strands.models")
    fake_strands_bedrock = types.ModuleType("strands.models.bedrock")

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

        def __call__(self, prompt):
            # Short-circuit before any network call. By this point the
            # `context` dict has already been built and serialized.
            raise RuntimeError("no network access in test")

    class _FakeBedrockModel:
        def __init__(self, *a, **k):
            pass

    fake_strands.Agent = _FakeAgent
    fake_strands_bedrock.BedrockModel = _FakeBedrockModel
    sys.modules["strands"] = fake_strands
    sys.modules["strands.models"] = fake_strands_models
    sys.modules["strands.models.bedrock"] = fake_strands_bedrock

    from src.agents.referee import reality_check_handler as rch

    # --- Spy on json.dumps to capture the `context` dict ----------------------
    captured = {}
    _real_dumps = rch.json.dumps

    def _spy_dumps(obj, *a, **k):
        if isinstance(obj, dict) and "engines_eliminated" in obj:
            captured["context"] = obj
        return _real_dumps(obj, *a, **k)

    rch.json.dumps = _spy_dumps

    # --- Fixed input: three engines eliminated, one duplicated to exercise ---
    # --- the dedupe path, plus a partial consolidation that must NOT count ---
    consolidations = [
        {"from_engine": "redis", "to_engine": "dynamodb", "query_count": 4,
         "saved_cost_estimate": 10, "action": "full", "reason": "", "queries_retained": []},
        {"from_engine": "opensearch", "to_engine": "dynamodb", "query_count": 2,
         "saved_cost_estimate": 5, "action": "full", "reason": "", "queries_retained": []},
        {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 1,
         "saved_cost_estimate": 2, "action": "full", "reason": "", "queries_retained": []},
        {"from_engine": "redis", "to_engine": "dynamodb", "query_count": 1,
         "saved_cost_estimate": 1, "action": "full", "reason": "", "queries_retained": []},
        {"from_engine": "elasticache", "to_engine": "dynamodb", "query_count": 1,
         "saved_cost_estimate": 1, "action": "partial", "reason": "", "queries_retained": ["q1"]},
    ]

    result = rch._generate_executive_summary(
        database_name="testdb",
        collector_output={"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
        before_distribution={"dynamodb": 1, "redis": 2, "opensearch": 1, "documentdb": 1, "elasticache": 1},
        after_distribution={"dynamodb": 6, "elasticache": 1},
        consolidations=consolidations,
        unique_value_assessment={},
        architectural_patterns=[],
        recommendations=[],
        analysis_outputs={},
    )

    # The fake Agent always raises, so the function must have swallowed the
    # exception and returned None -- the interesting part is what got captured.
    assert result is None, result
    print(json.dumps(captured["context"]["engines_eliminated"]))
    """)


def _run_child(hashseed: str) -> list[str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"child process failed (PYTHONHASHSEED={hashseed}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    last_line = proc.stdout.strip().splitlines()[-1]
    result: list[str] = json.loads(last_line)
    return result


class TestEliminatedEnginesOrderIsHashSeedIndependent:
    """engines_eliminated must come back in the same order regardless of
    PYTHONHASHSEED (issue #186)."""

    def test_three_eliminated_engines_order_matches_across_hash_seeds(self):
        result_seed_1 = _run_child("1")
        result_seed_2 = _run_child("2")

        assert result_seed_1 == result_seed_2
        # Also pin the exact expected (sorted, deduped) order so a future
        # regression to list(set(...)) is caught even if, by coincidence, both
        # seeds happened to agree with each other but not with the sorted order.
        assert result_seed_1 == ["documentdb", "opensearch", "redis"]


# ---------------------------------------------------------------------------
# Issue #288: per-query placement and assignment resolution must not depend on
# set iteration order (and so on PYTHONHASHSEED).
# ---------------------------------------------------------------------------


class TestAbsorberTieBreakIsOrderIndependent:
    """``_find_best_absorber_for_query`` iterated a set of committed engines and
    sorted on a partial key, so a fit/overlap tie went to whichever engine the
    set yielded first (#288)."""

    @staticmethod
    def _pick(committed_order: list[str]) -> str:
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        analysis = {"table_recommendations": [{"table_id": "t1", "confidence_score": 80}]}
        absorber = _find_best_absorber_for_query(
            {"query_id": "q1", "assigned_engine": "documentdb"},
            committed_order,  # type: ignore[arg-type]  # a list pins the iteration order
            "documentdb",
            {},
            {
                "q1": {"tables_accessed": ["t1"]},
                "q2": {"tables_accessed": ["t1"]},
                "q3": {"tables_accessed": ["t1"]},
            },
            {"aurora_postgresql": analysis, "dynamodb": analysis},
            {
                "aurora_postgresql": [{"query_id": "q2"}],
                "dynamodb": [{"query_id": "q3"}],
            },
            set(),
            "dynamodb",
        )
        assert absorber is not None
        return str(absorber["target_engine"])

    def test_tie_goes_to_the_same_engine_whatever_the_iteration_order(self):
        forward = self._pick(["aurora_postgresql", "dynamodb", "documentdb"])
        backward = self._pick(["documentdb", "dynamodb", "aurora_postgresql"])
        assert forward == backward == "aurora_postgresql"


_ASSIGNMENT_CHILD = textwrap.dedent("""
    import json

    from src.agents.referee.assignment_resolver import (
        _resolve_aurora_fallback,
        build_co_dependency_groups,
    )

    queries = [
        {"query_id": f"q{i}", "join_count": 2, "has_joins": True,
         "tables_accessed": ["orders", f"t{i % 3}"]}
        for i in range(12)
    ]
    print(json.dumps({
        "groups": build_co_dependency_groups(queries, []),
        "fallback": _resolve_aurora_fallback({"aurora_mysql", "aurora_postgresql", "dynamodb"}),
    }))
    """)


class TestAssignmentResolutionIsHashSeedIndependent:
    """Co-dependency groups were built from sets and the Aurora fallback took the
    first element of a set, so both varied with PYTHONHASHSEED (#288)."""

    def test_co_dependency_groups_and_fallback_match_across_hash_seeds(self):
        outputs = set()
        for seed in ("0", "1", "2", "3", "4", "5"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", _ASSIGNMENT_CHILD],
                cwd=REPO,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert proc.returncode == 0, proc.stderr
            outputs.add(proc.stdout.strip().splitlines()[-1])
        assert len(outputs) == 1, outputs
        result = json.loads(outputs.pop())
        assert result["groups"] == [[f"q{i}" for i in range(12)]]
        assert result["fallback"] == "aurora_postgresql"
