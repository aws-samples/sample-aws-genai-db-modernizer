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
