"""k6 subprocess runner for ElastiCache/Valkey load tests.

Uses xk6-redis extension for native Redis protocol support in k6.
"""

import json
import os
import subprocess  # nosec B404 — intentional subprocess use for k6 CLI
from pathlib import Path

import structlog

from src.agents.load_test.base import BaseRunner
from src.agents.load_test.models import RunResult
from src.contracts.load_test_models import LatencyPercentiles

logger = structlog.get_logger()


class ValkeyRunner(BaseRunner):
    """Executes k6 with xk6-redis extension and parses results."""

    def dry_run(self, scripts_dir: str, env_vars: dict) -> bool:
        """Skip dry-run for ElastiCache — xk6-redis connects at module load time.

        The Redis client is initialized at script import, so k6 cannot validate
        the script without a live cluster connection. The full run will catch
        any script errors immediately.
        """
        logger.info("k6_dry_run_skipped", reason="xk6-redis connects at module load")
        return True

    def run(self, scripts_dir: str, duration_minutes: int, env_vars: dict) -> RunResult:
        """Run the full k6 load test."""
        main_js = str(Path(scripts_dir) / "main.js")
        summary_path = str(Path(scripts_dir) / "k6_summary.json")
        summary_export_path = str(Path(scripts_dir) / "k6_summary_export.json")

        cmd = [
            "k6",
            "run",
            "--no-color",
            "--summary-export",
            summary_export_path,
            main_js,
        ]
        timeout_seconds = (duration_minutes + 5) * 60

        merged_env = os.environ.copy()
        merged_env["K6_SUMMARY_PATH"] = summary_path
        merged_env.update(env_vars)

        logger.info("k6_run_start", duration_minutes=duration_minutes)
        result = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit
            cmd, capture_output=True, text=True, timeout=timeout_seconds, env=merged_env
        )

        logger.info(
            "k6_run_complete",
            returncode=result.returncode,
            stderr_tail=result.stderr[-500:] if result.stderr else "",
        )

        summary = None
        sp = Path(summary_path)
        sep = Path(summary_export_path)
        if sp.exists() and sp.stat().st_size > 0:
            summary = json.loads(sp.read_text())
            logger.info("k6_summary_read", source="handleSummary", size=sp.stat().st_size)
        elif sep.exists() and sep.stat().st_size > 0:
            raw = json.loads(sep.read_text())
            summary = self._normalize_summary_export(raw)
            logger.info("k6_summary_read", source="summary-export", size=sep.stat().st_size)
        else:
            logger.warning("k6_summary_not_found", summary_path=summary_path)

        return RunResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            summary=summary,
        )

    def _normalize_summary_export(self, raw: dict) -> dict:
        """Convert --summary-export format to handleSummary format (wrap values)."""
        metrics: dict = raw.get("metrics", {})
        normalized_metrics: dict = {}
        for key, data in metrics.items():
            if isinstance(data, dict) and "values" not in data and ("med" in data or "avg" in data):
                normalized_metrics[key] = {"values": data}
            else:
                normalized_metrics[key] = data
        return {**raw, "metrics": normalized_metrics}

    @staticmethod
    def _get_metric_values(metric: dict) -> dict:
        """Extract values dict from a metric entry."""
        values = metric.get("values")
        if isinstance(values, dict):
            return values
        return metric

    def extract_scenario_latency(self, summary: dict, scenario_name: str) -> LatencyPercentiles:
        """Extract latency percentiles for a specific scenario.

        k6 tracks iteration_duration per scenario at nanosecond precision (Go runtime).
        In handleSummary JSON, per-scenario metrics appear as:
          "iteration_duration{scenario:scenario_0}" (with curly braces in the key)
        """
        metrics = summary.get("metrics", {})

        # Look for per-scenario iteration_duration (sub-ms precision from Go)
        # Key format: "iteration_duration{scenario:scenario_0}"
        scenario_key = f"iteration_duration{{scenario:{scenario_name}}}"
        if scenario_key in metrics:
            values = self._get_metric_values(metrics[scenario_key])
        else:
            # Try partial match in case k6 version uses different format
            values = {}
            for key in metrics:
                if "iteration_duration" in key and scenario_name in key:
                    values = self._get_metric_values(metrics[key])
                    break
            if not values:
                values = self._get_metric_values(metrics.get("iteration_duration", {}))

        return LatencyPercentiles(
            p50=values.get("med", values.get("p(50)", 0.0)),
            p90=values.get("p(90)", 0.0),
            p95=values.get("p(95)", 0.0),
            p99=values.get("p(99)", 0.0),
            p999=values.get("p(99.9)", 0.0),
            min=values.get("min", 0.0),
            max=values.get("max", 0.0),
        )

    def extract_scenario_iterations(self, summary: dict, scenario_name: str) -> int:
        """Extract iteration count for a specific scenario."""
        metrics = summary.get("metrics", {})

        # Per-scenario requests counter
        requests_key = f"requests_{scenario_name}"
        if requests_key in metrics:
            values = self._get_metric_values(metrics[requests_key])
            return int(values.get("count", 0))

        # Per-scenario iteration_duration count
        scenario_key = f"iteration_duration{{scenario:{scenario_name}}}"
        if scenario_key in metrics:
            values = self._get_metric_values(metrics[scenario_key])
            return int(values.get("count", 0))

        # Fallback to global
        values = self._get_metric_values(metrics.get("iteration_duration", {}))
        return int(values.get("count", 0))
