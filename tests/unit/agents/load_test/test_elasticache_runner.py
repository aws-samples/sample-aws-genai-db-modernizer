"""Tests for ValkeyRunner (k6 subprocess runner for ElastiCache).

Verifies:
  - dry_run always returns True (skipped for xk6-redis)
  - run invokes k6 with correct arguments
  - run reads handleSummary JSON when k6_summary.json exists
  - run falls back to --summary-export JSON when handleSummary file is absent
  - run returns empty summary when neither file exists
  - _normalize_summary_export wraps flat metric dicts under "values"
  - extract_scenario_latency reads per-scenario iteration_duration key
  - extract_scenario_latency falls back to partial key match
  - extract_scenario_latency falls back to global iteration_duration
  - extract_scenario_latency returns zeros when no metrics found
  - extract_scenario_iterations reads requests_{scenario_name} counter
  - extract_scenario_iterations falls back to per-scenario iteration count
  - extract_scenario_iterations falls back to global iteration_duration count
  - run merges env_vars with os.environ
  - run timeout is (duration_minutes + 5) * 60
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.elasticache.runner import ValkeyRunner
from src.agents.load_test.models import RunResult
from src.contracts.load_test_models import LatencyPercentiles

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def runner() -> ValkeyRunner:
    return ValkeyRunner()


# =============================================================================
# dry_run
# =============================================================================


class TestDryRun:
    def test_always_returns_true(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        """xk6-redis connects at module load, so validation is skipped."""
        result = runner.dry_run(str(tmp_path), env_vars={"AWS_REGION": "us-east-1"})
        assert result is True

    def test_never_calls_subprocess(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            runner.dry_run(str(tmp_path), env_vars={})
        mock_run.assert_not_called()


# =============================================================================
# run — subprocess invocation
# =============================================================================


class TestRun:
    def test_run_calls_k6_with_main_js(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        (tmp_path / "main.js").write_text("export default function() {}")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner.run(str(tmp_path), duration_minutes=5, env_vars={})

        cmd = mock_run.call_args[0][0]
        assert "k6" in cmd
        assert "run" in cmd
        assert str(tmp_path / "main.js") in cmd

    def test_run_includes_summary_export_flag(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner.run(str(tmp_path), duration_minutes=5, env_vars={})

        cmd = mock_run.call_args[0][0]
        assert "--summary-export" in cmd

    def test_run_timeout_is_duration_plus_5_minutes(
        self, runner: ValkeyRunner, tmp_path: Path
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner.run(str(tmp_path), duration_minutes=10, env_vars={})

        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 15 * 60  # (10 + 5) * 60

    def test_run_returns_run_result(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="stdout_content", stderr="")
            result = runner.run(str(tmp_path), duration_minutes=1, env_vars={})

        assert isinstance(result, RunResult)
        assert result.returncode == 0
        assert result.stdout == "stdout_content"

    def test_run_merges_env_vars(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        env_overrides = {"ELASTICACHE_ENDPOINT": "cache.example.com", "CUSTOM_VAR": "hello"}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner.run(str(tmp_path), duration_minutes=1, env_vars=env_overrides)

        merged_env = mock_run.call_args[1]["env"]
        assert merged_env["ELASTICACHE_ENDPOINT"] == "cache.example.com"
        assert merged_env["CUSTOM_VAR"] == "hello"

    def test_run_sets_k6_summary_path_env(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner.run(str(tmp_path), duration_minutes=1, env_vars={})

        merged_env = mock_run.call_args[1]["env"]
        assert "K6_SUMMARY_PATH" in merged_env


# =============================================================================
# run — summary loading
# =============================================================================


class TestRunSummaryLoading:
    def test_reads_handle_summary_json_when_present(
        self, runner: ValkeyRunner, tmp_path: Path
    ) -> None:
        summary_data = {"metrics": {"iteration_duration": {"values": {"med": 1.5, "p(90)": 3.0}}}}
        summary_path = tmp_path / "k6_summary.json"
        summary_path.write_text(json.dumps(summary_data))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch.dict("os.environ", {"K6_SUMMARY_PATH": str(summary_path)}):
                result = runner.run(
                    str(tmp_path),
                    duration_minutes=1,
                    env_vars={"K6_SUMMARY_PATH": str(summary_path)},
                )

        assert result.summary == summary_data

    def test_falls_back_to_summary_export_when_handle_summary_absent(
        self, runner: ValkeyRunner, tmp_path: Path
    ) -> None:
        raw_export = {
            "metrics": {
                "iteration_duration": {"med": 2.0, "p(90)": 4.0, "p(95)": 5.0, "count": 1000}
            }
        }
        export_path = tmp_path / "k6_summary_export.json"
        export_path.write_text(json.dumps(raw_export))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.run(str(tmp_path), duration_minutes=1, env_vars={})

        # Should have read and normalized the export
        assert result.summary is not None
        values = result.summary["metrics"]["iteration_duration"].get("values")
        assert values is not None
        assert values["med"] == 2.0

    def test_summary_is_none_when_no_files_written(
        self, runner: ValkeyRunner, tmp_path: Path
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.run(str(tmp_path), duration_minutes=1, env_vars={})

        assert result.summary is None

    def test_returncode_preserved_on_failure(self, runner: ValkeyRunner, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error output")
            result = runner.run(str(tmp_path), duration_minutes=1, env_vars={})

        assert result.returncode == 1
        assert result.stderr == "error output"


# =============================================================================
# _normalize_summary_export
# =============================================================================


class TestNormalizeSummaryExport:
    def test_wraps_flat_metric_in_values(self, runner: ValkeyRunner) -> None:
        raw = {"metrics": {"iteration_duration": {"med": 2.0, "avg": 2.5}}}
        normalized = runner._normalize_summary_export(raw)
        assert "values" in normalized["metrics"]["iteration_duration"]
        assert normalized["metrics"]["iteration_duration"]["values"]["med"] == 2.0

    def test_does_not_double_wrap_already_nested_metric(self, runner: ValkeyRunner) -> None:
        raw = {"metrics": {"iteration_duration": {"values": {"med": 3.0, "p(90)": 5.0}}}}
        normalized = runner._normalize_summary_export(raw)
        # Should remain unchanged (already has "values")
        assert normalized["metrics"]["iteration_duration"]["values"]["med"] == 3.0

    def test_preserves_non_metric_keys(self, runner: ValkeyRunner) -> None:
        raw = {"root_group": {}, "metrics": {"m1": {"med": 1.0}}}
        normalized = runner._normalize_summary_export(raw)
        assert "root_group" in normalized


# =============================================================================
# extract_scenario_latency
# =============================================================================


class TestExtractScenarioLatency:
    def test_reads_per_scenario_iteration_duration_key(self, runner: ValkeyRunner) -> None:
        summary = {
            "metrics": {
                "iteration_duration{scenario:scenario_0}": {
                    "values": {
                        "med": 2.5,
                        "p(90)": 4.0,
                        "p(95)": 5.0,
                        "p(99)": 8.0,
                        "p(99.9)": 15.0,
                        "min": 0.5,
                        "max": 30.0,
                    }
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "scenario_0")
        assert latency.p50 == 2.5
        assert latency.p90 == 4.0
        assert latency.p99 == 8.0
        assert latency.min == 0.5
        assert latency.max == 30.0

    def test_partial_key_match_fallback(self, runner: ValkeyRunner) -> None:
        """If the exact key isn't found, match any key containing both strings."""
        summary = {
            "metrics": {
                "iteration_duration{sc=scenario_1}": {
                    "values": {"med": 1.0, "p(90)": 2.0, "p(95)": 3.0, "p(99)": 4.0, "p(99.9)": 5.0}
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "scenario_1")
        assert latency.p50 == 1.0

    def test_falls_back_to_global_iteration_duration(self, runner: ValkeyRunner) -> None:
        summary = {
            "metrics": {
                "iteration_duration": {
                    "values": {"med": 3.0, "p(90)": 6.0, "p(95)": 7.0, "p(99)": 9.0, "p(99.9)": 12.0}
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "scenario_missing")
        assert latency.p50 == 3.0

    def test_returns_zeros_when_no_metrics(self, runner: ValkeyRunner) -> None:
        latency = runner.extract_scenario_latency({"metrics": {}}, "scenario_0")
        assert latency.p50 == 0.0
        assert latency.p99 == 0.0

    def test_handles_normalized_export_format(self, runner: ValkeyRunner) -> None:
        """Values may be directly on the metric dict (after normalization)."""
        summary = {
            "metrics": {
                "iteration_duration{scenario:scenario_0}": {
                    "values": {"med": 5.0, "p(90)": 9.0, "p(95)": 10.0, "p(99)": 12.0, "p(99.9)": 15.0}
                }
            }
        }
        latency = runner.extract_scenario_latency(summary, "scenario_0")
        assert latency.p50 == 5.0
        assert latency.p95 == 10.0


# =============================================================================
# extract_scenario_iterations
# =============================================================================


class TestExtractScenarioIterations:
    def test_reads_requests_counter(self, runner: ValkeyRunner) -> None:
        summary = {
            "metrics": {
                "requests_scenario_0": {"values": {"count": 12_500, "rate": 208.3}}
            }
        }
        count = runner.extract_scenario_iterations(summary, "scenario_0")
        assert count == 12_500

    def test_falls_back_to_per_scenario_iteration_count(self, runner: ValkeyRunner) -> None:
        summary = {
            "metrics": {
                "iteration_duration{scenario:scenario_1}": {
                    "values": {"count": 8_000, "med": 2.0}
                }
            }
        }
        count = runner.extract_scenario_iterations(summary, "scenario_1")
        assert count == 8_000

    def test_falls_back_to_global_iteration_duration_count(
        self, runner: ValkeyRunner
    ) -> None:
        summary = {
            "metrics": {"iteration_duration": {"values": {"count": 3_000, "med": 1.5}}}
        }
        count = runner.extract_scenario_iterations(summary, "scenario_missing")
        assert count == 3_000

    def test_returns_zero_when_no_metrics(self, runner: ValkeyRunner) -> None:
        count = runner.extract_scenario_iterations({"metrics": {}}, "scenario_0")
        assert count == 0

    def test_returns_integer(self, runner: ValkeyRunner) -> None:
        summary = {
            "metrics": {
                "requests_scenario_0": {"values": {"count": 500.9}}
            }
        }
        count = runner.extract_scenario_iterations(summary, "scenario_0")
        assert isinstance(count, int)
        assert count == 500
