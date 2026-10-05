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
import subprocess  # nosec B404 -- runs a fixed, module-level -c script, never external input
import sys
import textwrap
from pathlib import Path

import pytest

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
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus a fixed, module-level -c script
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
    def _pick(committed_order: list[str], dynamodb_queries: int = 1) -> str:
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        analysis = {"table_recommendations": [{"table_id": "t1", "confidence_score": 80}]}
        # Extra dynamodb queries touch another table, so the overlap stays 1 each.
        extra = [{"query_id": f"x{i}"} for i in range(dynamodb_queries - 1)]
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
                "dynamodb": [{"query_id": "q3"}, *extra],
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

    def test_tie_goes_to_the_engine_already_serving_more_queries(self):
        forward = self._pick(["aurora_postgresql", "dynamodb", "documentdb"], dynamodb_queries=3)
        backward = self._pick(["documentdb", "dynamodb", "aurora_postgresql"], dynamodb_queries=3)
        assert forward == backward == "dynamodb"


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
            proc = subprocess.run(  # nosec B603 -- fixed interpreter plus a fixed, module-level -c script
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


class TestAuroraChoiceFollowsTheSource:
    """With both Aurora engines available, the source's dialect wins; otherwise
    the engine with more queries, then PostgreSQL (#288 review)."""

    BOTH = {"aurora_mysql", "aurora_postgresql", "dynamodb"}

    def test_pick_aurora_engine(self):
        from src.agents.referee.aurora_choice import pick_aurora_engine

        assert pick_aurora_engine(self.BOTH, "mysql") == "aurora_mysql"
        assert pick_aurora_engine(self.BOTH, "MariaDB") == "aurora_mysql"
        assert pick_aurora_engine(self.BOTH, "postgresql") == "aurora_postgresql"
        # The source does not decide: more queries, then PostgreSQL.
        assert pick_aurora_engine(self.BOTH, "oracle", {"aurora_mysql": 3}) == "aurora_mysql"
        assert pick_aurora_engine(self.BOTH, "") == "aurora_postgresql"
        # The source's dialect is not a candidate.
        assert pick_aurora_engine({"aurora_postgresql"}, "mysql") == "aurora_postgresql"
        assert pick_aurora_engine({"dynamodb"}, "mysql") is None

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql")],
    )
    def test_resolver_fallback_uses_the_source_dialect(self, source, expected):
        from src.agents.referee.assignment_resolver import AssignmentResolver

        assignment = AssignmentResolver().resolve(
            {"selected_agents": ["aurora_mysql", "aurora_postgresql"]},
            {},
            {
                "metadata": {"source_database": {"engine": source}},
                "queries": {"query_patterns": [{"query_id": "q1", "tables_accessed": ["t"]}]},
            },
        )
        assert [qa.assigned_engine for qa in assignment.query_assignments] == [expected]

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("", "aurora_mysql")],
    )
    def test_corrections_redirect_to_the_source_dialect(self, source, expected):
        from src.agents.referee.consolidation_validator import apply_corrections

        revised = [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "m2", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
        ]
        corrections = [
            {
                "query_id": "q1",
                "original_engine": "documentdb",
                "failed_target": "dynamodb",
                "reason": "needs joins",
            }
        ]
        updated, _ = apply_corrections(
            corrections, revised, [], surviving_engines=self.BOTH, source_engine=source
        )
        # Without a source the engine with more queries (MySQL, 2 vs 1) wins.
        assert updated[0]["assigned_engine"] == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("", "aurora_postgresql")],
    )
    def test_sanity_sweep_redirects_to_the_source_dialect(self, source, expected):
        from src.agents.referee.consolidation_validator import sanity_sweep

        revised = [{"query_id": f"d{i}", "assigned_engine": "dynamodb"} for i in range(10)]
        revised += [
            {"query_id": "o1", "assigned_engine": "opensearch"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
        ]
        updated, _ = sanity_sweep(revised, [], {}, source_engine=source)
        # Without a source the Aurora engines tie on queries, so PostgreSQL wins.
        assert next(qa for qa in updated if qa["query_id"] == "o1")["assigned_engine"] == expected


class TestAuroraAbsorptionFollowsTheSource:
    """Reality Check's absorption pass and its rerun use the same Aurora rule as
    the resolver and the validator passes (#288 review)."""

    @staticmethod
    def _setup():
        # aurora_mysql serves 2 queries, aurora_postgresql 1, documentdb 1 (absorbable).
        assignments = [
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "m2", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
            {"query_id": "d1", "assigned_engine": "documentdb"},
        ]
        recs = {"table_recommendations": [{"table_id": "t", "confidence_score": 80}]}
        analysis = {e: recs for e in ("aurora_mysql", "aurora_postgresql", "documentdb")}
        return assignments, analysis

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("", "aurora_mysql")],
    )
    def test_absorption_pass_picks_the_source_dialect(self, source, expected):
        from src.agents.referee.reality_check import _run_aurora_absorption_pass

        assignments, analysis = self._setup()
        engine_queries: dict[str, list[dict]] = {}
        for qa in assignments:
            engine_queries.setdefault(qa["assigned_engine"], []).append(qa)
        result = _run_aurora_absorption_pass(
            engine_queries=engine_queries,
            surviving_engines=set(engine_queries),
            mandatory_committed_engines=set(),
            query_signals={},
            query_map={qa["query_id"]: {"tables_accessed": ["t"]} for qa in assignments},
            analysis_outputs=analysis,
            query_capabilities={},
            source_engine=source,
        )
        # Without a source the engine with more queries (MySQL, 2 vs 1) wins.
        assert result.aurora_engine == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("", "aurora_mysql")],
    )
    def test_rerun_takes_the_source_from_the_collector(self, source, expected):
        from src.agents.referee.reality_check import rerun_aurora_absorption

        assignments, analysis = self._setup()
        collector = {
            "metadata": {"source_database": {"engine": source}},
            "queries": {
                "query_patterns": [
                    {"query_id": qa["query_id"], "tables_accessed": ["t"]} for qa in assignments
                ]
            },
        }
        revised, consolidations = rerun_aurora_absorption(assignments, {}, analysis, collector)
        assert [c["to_engine"] for c in consolidations] == [expected]
        assert next(qa for qa in revised if qa["query_id"] == "d1")["assigned_engine"] == expected
