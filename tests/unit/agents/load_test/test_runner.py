"""Tests for DynamoDB K6 runner."""
import json
from unittest.mock import MagicMock, patch

from src.agents.load_test.dynamodb.runner import K6Runner
from src.agents.load_test.models import RunResult


class TestK6Runner:
    def test_dry_run_calls_k6_inspect(self, tmp_path):
        (tmp_path / "main.js").write_text("export default function() {}")
        runner = K6Runner()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.dry_run(str(tmp_path), env_vars={"AWS_REGION": "us-east-1"})

        assert result is True
        cmd = mock_run.call_args[0][0]
        assert "k6" in cmd
        assert "inspect" in cmd

    def test_dry_run_returns_false_on_failure(self, tmp_path):
        (tmp_path / "main.js").write_text("invalid")
        runner = K6Runner()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="parse error")
            result = runner.dry_run(str(tmp_path), env_vars={})

        assert result is False

    def test_run_returns_run_result_with_summary(self, tmp_path):
        (tmp_path / "main.js").write_text("export default function() {}")
        summary_data = {"metrics": {"latency_q1": {"values": {"med": 5.0, "p(90)": 8.0}}}}
        summary_path = tmp_path / "k6_summary.json"
        summary_path.write_text(json.dumps(summary_data))

        runner = K6Runner()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            # Need to tell runner where to find the summary
            with patch.dict("os.environ", {"K6_SUMMARY_PATH": str(summary_path)}):
                result = runner.run(
                    str(tmp_path),
                    duration_minutes=1,
                    env_vars={"K6_SUMMARY_PATH": str(summary_path)},
                )

        assert isinstance(result, RunResult)
        assert result.returncode == 0
        assert result.summary == summary_data

    def test_extract_scenario_latency_from_custom_metric(self):
        runner = K6Runner()
        summary = {
            "metrics": {
                "latency_q1": {
                    "values": {
                        "med": 5.0,
                        "p(90)": 8.0,
                        "p(95)": 10.0,
                        "p(99)": 15.0,
                        "p(99.9)": 20.0,
                        "min": 2.0,
                        "max": 50.0,
                    }
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "q1")
        assert latency.p50 == 5.0
        assert latency.p90 == 8.0
        assert latency.p99 == 15.0

    def test_extract_scenario_latency_fallback_to_iteration_duration(self):
        runner = K6Runner()
        summary = {
            "metrics": {
                "iteration_duration": {
                    "values": {
                        "med": 3.0,
                        "p(90)": 6.0,
                        "p(95)": 7.0,
                        "p(99)": 9.0,
                        "p(99.9)": 12.0,
                        "min": 1.0,
                        "max": 30.0,
                    }
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "q_missing")
        assert latency.p50 == 3.0

    def test_extract_scenario_iterations(self):
        runner = K6Runner()
        summary = {"metrics": {"requests_q1": {"values": {"count": 5000, "rate": 333.0}}}}
        count = runner.extract_scenario_iterations(summary, "q1")
        assert count == 5000
