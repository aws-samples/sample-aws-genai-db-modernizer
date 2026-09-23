"""Tests for the assessment-core / synthesis graph build-and-publish boundary.

``run_assessment_core`` and ``run_synthesis_core`` each build + publish the
context graph read-model at their single-writer boundary via ``_build_graph``.
The build is unconditional (the per-query journey read-model it replaced is
gone) and must never raise into the phase. These tests pin that behavior without
running the full pipeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.atx_orchestrator.core import _build_graph


class TestBuildGraph:
    def test_builds_and_publishes(self) -> None:
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph"
        ) as mock_build:
            _build_graph(store, "job-1", "mydb")
        mock_build.assert_called_once_with(store, "mydb", "job-1")

    def test_build_error_never_propagates(self) -> None:
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise — the read-model build cannot fail the assessment.
            _build_graph(store, "job-1", "mydb")


class TestSynthesisBoundaryBuildsGraph:
    """run_synthesis_core must materialize the graph at its boundary (after the
    schema fan-out has joined) via ``_build_graph``. Patch the synthesis
    internals so the test exercises only the boundary call, not the full
    synthesis pipeline."""

    def test_synthesis_core_calls_build_graph(self) -> None:
        from src.atx_orchestrator import core

        store = MagicMock()
        store.exists.return_value = True
        # Minimal report so the post-synthesis guards pass without raising.
        store.read_json.return_value = {
            "ranking": [{"target": "dynamodb", "schema_design_available": True}],
            "recommended_architecture": {"databases": [{"service": "dynamodb"}]},
            "risk_assessment": {"overall_risk_level": "LOW"},
            "summary": "ok",
            "assignment_summary": {"x": 1},
        }

        with (
            patch("src.agents.referee.synthesis_handler.run_synthesis"),
            patch("src.atx_orchestrator.core._build_graph") as mock_build,
        ):
            core.run_synthesis_core(
                job_id="job-1", database_name="mydb", assignment_version=1, store=store
            )

        mock_build.assert_called_once_with(store, "job-1", "mydb")
