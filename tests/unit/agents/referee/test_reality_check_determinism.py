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
