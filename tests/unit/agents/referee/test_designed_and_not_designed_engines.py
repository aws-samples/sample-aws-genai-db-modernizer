"""#370 recheck: ``designed_and_not_designed_engines`` must not require
``assigned_queries`` to exist at all.

An *unversioned* run (no assignment artifact) never adds ``assigned_queries``
to a ranking entry -- it only exists once assignment has run. Gating
classification on ``assigned_queries > 0`` (even via ``.get(..., 0)``) treated
a missing key the same as a key present and zero, so in an unversioned run
every ranking entry was skipped regardless of whether it has a design. Both
``designed`` and ``not_designed`` came back empty even when an engine clearly
had ``schema_design_available=True``, which then made
``build_architecture_recommendation`` append a false "Schema design was not
run, so no tables are allocated yet.", and made
``generate_executive_summary`` fall into the fully assertive "all designed"
branch with nothing to back it (``not designed and not not_designed`` is
vacuously true for two empty lists).

Fix: a ranking entry with no ``assigned_queries`` key at all is classified by
``schema_design_available`` alone, not gated on workload. A versioned entry
(``assigned_queries`` present) keeps the existing workload-or-cache-overlay
gate.
"""

from __future__ import annotations

from src.agents.referee.synthesis_report import designed_and_not_designed_engines


class TestUnversionedRun:
    """No assignment artifact at all: ranking entries never got
    ``assigned_queries`` added to them."""

    def test_classified_by_schema_design_available_alone(self) -> None:
        ranking = [
            {"target": "dynamodb", "confidence_score": 90, "schema_design_available": True},
            {"target": "opensearch", "confidence_score": 60, "schema_design_available": False},
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == ["dynamodb"]
        assert not_designed == ["opensearch"]

    def test_all_designed_in_an_unversioned_run(self) -> None:
        ranking = [
            {"target": "dynamodb", "confidence_score": 90, "schema_design_available": True},
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == ["dynamodb"]
        assert not_designed == []

    def test_none_designed_in_an_unversioned_run(self) -> None:
        """The exact bug: --llm-mode none, no assignment -- every engine has
        schema_design_available=False and no assigned_queries key."""
        ranking = [
            {"target": "dynamodb", "confidence_score": 90, "schema_design_available": False},
            {"target": "elasticache", "confidence_score": 48, "schema_design_available": False},
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == []
        assert not_designed == ["dynamodb", "elasticache"]


class TestVersionedRun:
    """An assignment ran: assigned_queries is present (possibly 0). The
    existing workload-or-cache-overlay gate is unchanged."""

    def test_zero_workload_and_no_cache_overlay_is_excluded(self) -> None:
        ranking = [
            {
                "target": "aurora_mysql",
                "confidence_score": 50,
                "assigned_queries": 0,
                "schema_design_available": True,  # stale/unused design artifact
            },
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == []
        assert not_designed == []

    def test_workload_with_a_design_is_designed(self) -> None:
        ranking = [
            {
                "target": "dynamodb",
                "confidence_score": 90,
                "assigned_queries": 1486,
                "schema_design_available": True,
            },
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == ["dynamodb"]
        assert not_designed == []

    def test_cache_overlay_with_no_design_is_not_designed(self) -> None:
        ranking = [
            {
                "target": "elasticache",
                "confidence_score": 48,
                "assigned_queries": 0,
                "cache_overlay_queries": 34,
                "schema_design_available": False,
            },
        ]

        designed, not_designed = designed_and_not_designed_engines(ranking)

        assert designed == []
        assert not_designed == ["elasticache"]


def test_empty_ranking_returns_empty_lists() -> None:
    assert designed_and_not_designed_engines([]) == ([], [])
