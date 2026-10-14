"""Regression test: reality-check executive-summary output must not depend on
Python's per-process string hash seed (PYTHONHASHSEED).

``_generate_executive_summary`` used to build its ``engines_eliminated`` field
with ``list(set(eliminated_engines))``. Since ``set`` iteration order for
strings depends on PYTHONHASHSEED, two runs over identical input could produce
``engines_eliminated`` in a different order, which propagates into reports and
other downstream artifacts (see issue #186). Since #628 the prompt carries the
external request's ``executive_summary`` block instead (one payload for both
LLM modes), serialized with sorted keys.

This test runs the function twice, in separate subprocesses with different
PYTHONHASHSEED values, over a fixed input that eliminates three engines, and
asserts the two runs build byte-identical prompts.
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

    # --- Capture the prompt the agent is called with ---------------------------
    captured = {}

    class _CapturingAgent(_FakeAgent):
        def __call__(self, prompt):
            captured["prompt"] = prompt
            raise RuntimeError("no network access in test")

    fake_strands.Agent = _CapturingAgent
    rch.build_bedrock_model = lambda *a, **k: object()

    # --- Fixed input: three engines eliminated, one duplicated, plus a --------
    # --- partial consolidation that must NOT count -----------------------------
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
    det = {
        "collector_output": {"database_schema": {"tables": []}, "queries": {"query_patterns": []}},
        "triage": {"signals": []},
        "assignment": {"query_assignments": []},
        "revised_assignments": [],
        "before_distribution": {"dynamodb": 1, "redis": 2, "opensearch": 1, "documentdb": 1, "elasticache": 1},
        "after_distribution": {"dynamodb": 6, "elasticache": 1},
        "consolidations": consolidations,
        "unique_value_assessment": {},
        "architectural_patterns": [],
        "recommendations": [],
    }

    result = rch._generate_executive_summary(det)

    # The fake Agent always raises, so the function must have swallowed the
    # exception and returned None -- the interesting part is what got captured.
    assert result is None, result
    print(json.dumps(captured["prompt"]))
    """)


def _run_child(hashseed: str) -> str:
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
    result: str = json.loads(last_line)
    return result


class TestEliminatedEnginesOrderIsHashSeedIndependent:
    """The summary prompt must come back byte-identical regardless of
    PYTHONHASHSEED (issue #186)."""

    def test_three_eliminated_engines_order_matches_across_hash_seeds(self):
        prompt_seed_1 = _run_child("1")
        prompt_seed_2 = _run_child("2")

        assert prompt_seed_1 == prompt_seed_2
        # The block lists the consolidations as the records give them.
        positions = [prompt_seed_1.index(f'"from_engine":"{e}"') for e in ("redis", "opensearch")]
        assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# Issue #288: per-query placement and assignment resolution must not depend on
# set iteration order (and so on PYTHONHASHSEED).
# ---------------------------------------------------------------------------


class TestAbsorberTieBreakIsOrderIndependent:
    """``_find_best_absorber_for_query`` iterated a set of committed engines
    and used to sort a fit tie by table overlap, then how many queries an
    engine already had, then its name (#288). ADR-031 Decision 2 has no
    overlap or query-count rule for a non-Aurora tie (#503): it now shares
    ``break_tie`` with the resolver's ``break_owner_tie`` and this module's
    own ``_pick_winner``, so a tie is either source-compatible or
    deterministically ``tie_unresolved`` -- never decided by how many
    queries an engine already has, and (being deterministic) still never
    by set iteration order."""

    @staticmethod
    def _pick(
        committed_order: list[str],
        dynamodb_queries: int = 1,
        source_compatible_engine: str | None = None,
        second_engine: str = "aurora_postgresql",
        settled_counts: dict[str, int] | None = None,
    ) -> str:
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        # Extra dynamodb queries touch another table, so the overlap stays 1 each.
        extra = [{"query_id": f"x{i}"} for i in range(dynamodb_queries - 1)]
        # Both candidates tied on resolver score too (#502 review round 5,
        # finding N3 swapped the scorer here) -- the tie this test checks is
        # the ADR-031 Decision 2 tie-break itself (#503).
        ctx = ScoringContext(
            analysis_outputs={
                second_engine: {
                    "table_recommendations": [{"table_id": "t1", "confidence_score": 80}]
                },
                "dynamodb": {"table_recommendations": [{"table_id": "t1", "confidence_score": 80}]},
            }
        )
        absorber = _find_best_absorber_for_query(
            {"query_id": "q1", "assigned_engine": "documentdb"},
            committed_order,  # type: ignore[arg-type]  # a list pins the iteration order
            "documentdb",
            {},
            {
                "q1": {"query_id": "q1", "tables_accessed": ["t1"]},
                "q2": {"query_id": "q2", "tables_accessed": ["t1"]},
                "q3": {"query_id": "q3", "tables_accessed": ["t1"]},
            },
            {
                second_engine: [{"query_id": "q2"}],
                "dynamodb": [{"query_id": "q3"}, *extra],
            },
            set(),
            "dynamodb",
            ctx,
            source_compatible_engine=source_compatible_engine,
            settled_counts=settled_counts,
        )
        assert absorber is not None
        return str(absorber["target_engine"])

    def test_tie_goes_to_the_same_engine_whatever_the_iteration_order(self):
        forward = self._pick(["aurora_postgresql", "dynamodb", "documentdb"])
        backward = self._pick(["documentdb", "dynamodb", "aurora_postgresql"])
        assert forward == backward
        assert forward in {"aurora_postgresql", "dynamodb"}

    def test_the_tie_does_not_depend_on_which_engine_already_has_more_queries(self):
        """#503: this used to be the "whichever has more existing queries
        wins" rule -- giving dynamodb 3 queries against aurora_postgresql's
        1 used to flip the pick from aurora_postgresql to dynamodb. The
        ``dynamodb_queries`` knob here only grows this function's own
        ``engine_queries`` argument (table-overlap bookkeeping); it is not
        the maintainer-decided placeholder's ``settled_counts`` (#503
        review item 2 follow-up), a separate, explicit argument this test
        leaves at its default -- so growing it must not move the pick,
        unlike the pre-#503 rule it replaced.
        """
        forward = self._pick(["aurora_postgresql", "dynamodb", "documentdb"], dynamodb_queries=1)
        with_more_queries = self._pick(
            ["aurora_postgresql", "dynamodb", "documentdb"], dynamodb_queries=3
        )
        assert forward == with_more_queries

    def test_settled_counts_decides_the_tie_when_it_favors_one_engine(self):
        """The maintainer-decided placeholder's primary rule (ADR-031
        Decision 12): the tied engine with more SETTLED (non-tied) workload
        wins. Passed explicitly here, independent of ``engine_queries``/
        ``dynamodb_queries`` above."""
        winner = self._pick(
            ["aurora_postgresql", "dynamodb", "documentdb"],
            settled_counts={"aurora_postgresql": 1, "dynamodb": 9},
        )
        assert winner == "dynamodb"

    def test_the_source_compatible_engine_wins_the_absorber_tie_too(self):
        """The issue (#503) reported that this absorber had "no
        source-compatible rule at all". ADR-031's source-compatible rule
        only ever applies to the Aurora engine matching the source
        dialect, so the tie here is between dynamodb and aurora_mysql (not
        aurora_postgresql, which could never be the source-compatible
        engine in the same tie as a tie between it and dynamodb for a
        MySQL-sourced database) -- marking aurora_mysql as the
        source-compatible engine must flip the pick to it, the same
        ``break_tie`` the resolver and ``_pick_winner`` use.
        """
        winner = self._pick(
            ["aurora_mysql", "dynamodb", "documentdb"],
            second_engine="aurora_mysql",
            source_compatible_engine="aurora_mysql",
        )
        assert winner == "aurora_mysql"


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
        "fallback": _resolve_aurora_fallback({"aurora_mysql", "aurora_postgresql", "dynamodb"})[0],
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
    the engine with more of the recorded SETTLED workload, then the shared
    ADR-031 tie-break's deterministic hash -- never a bare name preference
    (#288 review, #550)."""

    BOTH = {"aurora_mysql", "aurora_postgresql", "dynamodb"}

    def test_pick_aurora_engine(self):
        from src.agents.referee.aurora_choice import pick_aurora_engine

        assert pick_aurora_engine(self.BOTH, "mysql") == "aurora_mysql"
        assert pick_aurora_engine(self.BOTH, "MariaDB") == "aurora_mysql"
        assert pick_aurora_engine(self.BOTH, "postgresql") == "aurora_postgresql"
        # The source does not decide: the engine with more settled workload wins.
        assert pick_aurora_engine(self.BOTH, "oracle", {"aurora_mysql": 3}) == "aurora_mysql"
        # No settled workload at all (0 vs 0) and no tie_key: the shared ADR-031
        # tie-break's placeholder hash decides -- for this call's default empty
        # tie_key, that happens to land on aurora_postgresql, not because the name
        # is preferred (see test_aurora_choice.py's tie_key tests for a key where
        # it does not).
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
    def test_flags_tie_to_the_source_dialect(self, source, expected):
        # #647: a flagged query goes to its best remaining engine by score; an
        # empty ScoringContext ties both Aurora engines at 0, so the shared
        # break_tie decides -- the source-compatible engine, else the settled
        # workload (MySQL, 2 vs 1).
        from src.agents.referee.model_flags import route_flagged_unit
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.tie_break import settled_counts

        revised = [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "m2", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
        ]
        routing = route_flagged_unit(
            ["q1"],
            "dynamodb",
            self.BOTH | {"dynamodb"},
            ScoringContext().with_flag_gaps({"q1": ["dynamodb"]}),
            {"q1": {"query_id": "q1"}},
            source_engine=source,
            settled=settled_counts(revised),
        )
        assert routing.destination == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql")],
    )
    def test_sanity_sweep_ties_between_committed_aurora_engines_go_to_the_source_dialect(
        self, source, expected
    ):
        # #579: sanity_sweep no longer picks one special-cased "aurora
        # target" (``pick_aurora_engine``) for every orphan -- it scores
        # opensearch's one query against every OTHER committed engine
        # (here, just the two Aurora engines) and only reaches a tie-break
        # because an empty ``ScoringContext`` ties everyone at 0. Decision
        # 2's source-compatible rule still decides it, now by going through
        # the exact same shared ``tie_break.break_tie`` every other tie in
        # the pipeline uses, rather than a sanity_sweep-local Aurora pick.
        from src.agents.referee.consolidation_validator import sanity_sweep
        from src.agents.referee.query_scoring import ScoringContext

        revised = [
            {"query_id": "o1", "assigned_engine": "opensearch"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
        ]
        query_map = {qa["query_id"]: {"query_id": qa["query_id"]} for qa in revised}

        updated, _, _ = sanity_sweep(revised, [], ScoringContext(), query_map, source_engine=source)

        assert next(qa for qa in updated if qa["query_id"] == "o1")["assigned_engine"] == expected

    def test_sanity_sweep_tie_with_no_source_dialect_matches_the_shared_tie_break(self):
        # With no source at all, the tie between the two committed Aurora
        # engines falls to the same placeholder every other unresolved tie
        # in the pipeline uses (ADR-031 Decision 12) -- computed here
        # independently via ``break_tie`` itself, never hardcoded: #579
        # keys this tie by the query's own id, not the whole sweep's query
        # ids joined together the way the retired ``pick_aurora_engine``
        # call used to.
        from src.agents.referee.consolidation_validator import sanity_sweep
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.tie_break import break_tie, settled_counts

        revised = [
            {"query_id": "o1", "assigned_engine": "opensearch"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
            {"query_id": "p1", "assigned_engine": "aurora_postgresql"},
        ]
        query_map = {qa["query_id"]: {"query_id": qa["query_id"]} for qa in revised}
        workload = settled_counts(revised)
        expected_winner, _ = break_tie(
            ["aurora_mysql", "aurora_postgresql"],
            set(),
            None,
            settled_counts=workload,
            query_id="o1",
        )

        updated, _, _ = sanity_sweep(revised, [], ScoringContext(), query_map, source_engine="")

        assert (
            next(qa for qa in updated if qa["query_id"] == "o1")["assigned_engine"]
            == expected_winner
        )


# #588: ``TestAuroraAbsorptionFollowsTheSource`` tested which Aurora engine the
# deleted Aurora absorption pass absorbed into. The post-correction re-settle
# that replaces it places each query by the shared per-query scores and the
# ADR-031 tie-break instead; its source-dialect case is in
# ``test_reality_check_resettle.py``.
