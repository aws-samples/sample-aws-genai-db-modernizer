"""Tests for OpenSearch k6 runner.

Verifies:
  - dry_run validates scripts with duration=0s
  - run executes k6 and reads summary from handleSummary output
  - run falls back to --summary-export format
  - extract_scenario_latency reads per-scenario http_req_duration
  - extract_scenario_iterations reads per-scenario request counts
  - _normalize_summary_export wraps raw metric values
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.opensearch.runner import OpenSearchRunner


@pytest.fixture
def runner() -> OpenSearchRunner:
    return OpenSearchRunner()


@pytest.fixture
def k6_summary() -> dict:
    """Simulate k6 handleSummary JSON output."""
    return {
        "metrics": {
            "http_req_duration": {
                "values": {
                    "med": 5.2,
                    "p(90)": 8.1,
                    "p(95)": 12.3,
                    "p(99)": 25.0,
                    "p(99.9)": 50.0,
                    "min": 1.0,
                    "max": 100.0,
                    "count": 10000,
                }
            },
            "http_req_duration{scenario:scenario_0}": {
                "values": {
                    "med": 3.5,
                    "p(90)": 6.0,
                    "p(95)": 9.0,
                    "p(99)": 18.0,
                    "p(99.9)": 35.0,
                    "min": 0.8,
                    "max": 80.0,
                    "count": 5000,
                }
            },
            "http_req_duration{scenario:scenario_1}": {
                "values": {
                    "med": 7.0,
                    "p(90)": 12.0,
                    "p(95)": 15.0,
                    "p(99)": 30.0,
                    "p(99.9)": 60.0,
                    "min": 2.0,
                    "max": 120.0,
                    "count": 5000,
                }
            },
            "requests_scenario_0": {"values": {"count": 5000}},
            "requests_scenario_1": {"values": {"count": 5000}},
            # Per-query custom metrics (keyed by query_id) — the naming the
            # engine-agnostic handler actually looks up.
            "latency_q1": {
                "values": {
                    "med": 3.5,
                    "p(90)": 6.0,
                    "p(95)": 9.0,
                    "p(99)": 18.0,
                    "p(99.9)": 35.0,
                    "min": 0.8,
                    "max": 80.0,
                    "count": 5000,
                }
            },
            "requests_q1": {"values": {"count": 5000}},
        }
    }


class TestDryRun:
    @patch("subprocess.run")
    def test_dry_run_returns_true_on_success(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = runner.dry_run("/tmp/scripts", {"AWS_REGION": "us-east-1"})
        assert result is True

    @patch("subprocess.run")
    def test_dry_run_returns_false_on_failure(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = runner.dry_run("/tmp/scripts", {})
        assert result is False

    @patch("subprocess.run")
    def test_dry_run_uses_zero_duration(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        runner.dry_run("/tmp/scripts", {})
        cmd = mock_run.call_args[0][0]
        assert "inspect" in cmd

    @patch("subprocess.run")
    def test_dry_run_returns_false_on_timeout(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="k6", timeout=30)
        result = runner.dry_run("/tmp/scripts", {})
        assert result is False


class TestRun:
    @patch("subprocess.run")
    def test_run_reads_handle_summary_json(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
        k6_summary: dict,
    ) -> None:
        scripts_dir = tempfile.mkdtemp()
        summary_path = Path(scripts_dir) / "k6_summary.json"
        summary_path.write_text(json.dumps(k6_summary))
        (Path(scripts_dir) / "main.js").write_text("// placeholder")

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = runner.run(scripts_dir, duration_minutes=5, env_vars={})

        assert result.returncode == 0
        assert result.summary is not None
        assert "http_req_duration" in result.summary["metrics"]

    @patch("subprocess.run")
    def test_run_falls_back_to_summary_export(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        scripts_dir = tempfile.mkdtemp()
        export_path = Path(scripts_dir) / "k6_summary_export.json"
        raw_export = {
            "metrics": {
                "http_req_duration": {
                    "med": 5.0,
                    "p(90)": 8.0,
                    "avg": 6.0,
                }
            }
        }
        export_path.write_text(json.dumps(raw_export))
        (Path(scripts_dir) / "main.js").write_text("// placeholder")

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = runner.run(scripts_dir, duration_minutes=5, env_vars={})

        assert result.summary is not None
        # Should be normalized (wrapped in "values")
        assert "values" in result.summary["metrics"]["http_req_duration"]

    @patch("subprocess.run")
    def test_run_handles_missing_summary(
        self,
        mock_run: MagicMock,
        runner: OpenSearchRunner,
    ) -> None:
        scripts_dir = tempfile.mkdtemp()
        (Path(scripts_dir) / "main.js").write_text("// placeholder")

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = runner.run(scripts_dir, duration_minutes=5, env_vars={})

        assert result.returncode == 1
        assert result.summary is None


class TestExtractScenarioLatency:
    def test_extracts_per_scenario_latency(
        self, runner: OpenSearchRunner, k6_summary: dict
    ) -> None:
        latency = runner.extract_scenario_latency(k6_summary, "scenario_0")

        assert latency.p50 == 3.5
        assert latency.p90 == 6.0
        assert latency.p95 == 9.0
        assert latency.p99 == 18.0
        assert latency.min == 0.8
        assert latency.max == 80.0

    def test_extracts_latency_by_query_id_custom_metric(
        self, runner: OpenSearchRunner, k6_summary: dict
    ) -> None:
        """The handler passes query_id; latency_{query_id} must resolve to the
        per-query Trend, not the global http_req_duration fallback."""
        latency = runner.extract_scenario_latency(k6_summary, "q1")

        assert latency.p50 == 3.5
        assert latency.p99 == 18.0
        assert latency.p999 == 35.0

    def test_falls_back_to_global_when_scenario_not_found(
        self, runner: OpenSearchRunner, k6_summary: dict
    ) -> None:
        latency = runner.extract_scenario_latency(k6_summary, "scenario_99")

        assert latency.p50 == 5.2
        assert latency.p90 == 8.1


class TestExtractScenarioIterations:
    def test_extracts_per_scenario_count(self, runner: OpenSearchRunner, k6_summary: dict) -> None:
        count = runner.extract_scenario_iterations(k6_summary, "scenario_0")
        assert count == 5000

    def test_extracts_count_by_query_id_custom_metric(
        self, runner: OpenSearchRunner, k6_summary: dict
    ) -> None:
        """requests_{query_id} counter must resolve for the handler's qid lookup."""
        count = runner.extract_scenario_iterations(k6_summary, "q1")
        assert count == 5000

    def test_falls_back_to_http_req_duration_count(
        self, runner: OpenSearchRunner, k6_summary: dict
    ) -> None:
        count = runner.extract_scenario_iterations(k6_summary, "scenario_99")
        assert count == 10000


class TestNormalizeSummaryExport:
    def test_wraps_raw_values(self, runner: OpenSearchRunner) -> None:
        raw = {
            "metrics": {
                "http_req_duration": {
                    "med": 5.0,
                    "p(90)": 8.0,
                    "avg": 6.0,
                }
            }
        }
        normalized = runner._normalize_summary_export(raw)
        assert "values" in normalized["metrics"]["http_req_duration"]
        assert normalized["metrics"]["http_req_duration"]["values"]["med"] == 5.0

    def test_preserves_already_wrapped(self, runner: OpenSearchRunner) -> None:
        raw = {"metrics": {"http_req_duration": {"values": {"med": 5.0, "p(90)": 8.0}}}}
        normalized = runner._normalize_summary_export(raw)
        assert normalized["metrics"]["http_req_duration"]["values"]["med"] == 5.0
