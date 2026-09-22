"""Tests for the assessment-core graph build-and-publish boundary gate.

``run_assessment_core`` builds + publishes the context graph read-model at its
single-writer boundary via ``_maybe_build_graph``. This is gated by JOURNEY_MODE
and must never raise into the phase. These tests pin the gate without running the
full assessment pipeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.atx_orchestrator.core import _maybe_build_graph


class TestMaybeBuildGraphGate:
    def test_graph_mode_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNEY_MODE", "graph")
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph"
        ) as mock_build:
            _maybe_build_graph(store, "job-1", "mydb")
        mock_build.assert_called_once_with(store, "mydb", "job-1")

    def test_both_mode_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNEY_MODE", "both")
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph"
        ) as mock_build:
            _maybe_build_graph(store, "job-1", "mydb")
        mock_build.assert_called_once()

    def test_json_mode_skips_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Legacy journeys are the read-model in json mode; do not build the graph.
        monkeypatch.setenv("JOURNEY_MODE", "json")
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph"
        ) as mock_build:
            _maybe_build_graph(store, "job-1", "mydb")
        mock_build.assert_not_called()

    def test_default_unset_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNEY_MODE", raising=False)
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph"
        ) as mock_build:
            _maybe_build_graph(store, "job-1", "mydb")
        mock_build.assert_called_once()

    def test_build_error_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNEY_MODE", "graph")
        store = MagicMock()
        with patch(
            "src.atx_orchestrator.runtime.graph_transport.build_and_publish_graph",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise — the read-model build cannot fail the assessment.
            _maybe_build_graph(store, "job-1", "mydb")


class TestSynthesisBoundaryBuildsGraph:
    """run_synthesis_core must materialize the graph at its boundary (after the
    schema fan-out has joined) via the same _maybe_build_graph gate. Patch the
    synthesis internals so the test exercises only the boundary call, not the
    full synthesis pipeline."""

    def test_synthesis_core_calls_maybe_build_graph(self) -> None:
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
            patch("src.atx_orchestrator.core._maybe_build_graph") as mock_build,
        ):
            core.run_synthesis_core(
                job_id="job-1", database_name="mydb", assignment_version=1, store=store
            )

        mock_build.assert_called_once_with(store, "job-1", "mydb")
