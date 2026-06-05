"""k6 subprocess runner for DynamoDB load tests."""

import json
import os
import subprocess  # nosec B404 — intentional subprocess use for k6 CLI
from pathlib import Path

import structlog

from src.agents.load_test.base import BaseRunner
from src.agents.load_test.models import RunResult
from src.contracts.load_test_models import LatencyPercentiles

logger = structlog.get_logger()


class K6Runner(BaseRunner):
    """Executes k6 as a subprocess and parses results."""

    def dry_run(self, scripts_dir: str, env_vars: dict) -> bool:
        """Run k6 inspect to validate script parsing."""
        main_js = str(Path(scripts_dir) / "main.js")
        cmd = ["k6", "inspect", "--no-color", main_js]

        merged_env = os.environ.copy()
        merged_env.update(env_vars)

        logger.info("k6_dry_run", cmd=cmd)
        result = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            cmd, capture_output=True, text=True, timeout=120, env=merged_env
        )

        if result.returncode != 0:
            logger.error("k6_dry_run_failed", stderr=result.stderr[:500])
            return False

        logger.info("k6_dry_run_passed")
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
        result = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            cmd, capture_output=True, text=True, timeout=timeout_seconds, env=merged_env
        )

        logger.info(
            "k6_run_complete",
            returncode=result.returncode,
            stderr_tail=result.stderr[-500:] if result.stderr else "",
        )

        # Try handleSummary output first, fall back to --summary-export
        summary = None
        sp = Path(summary_path)
        sep = Path(summary_export_path)
        if sp.exists() and sp.stat().st_size > 0:
            summary = json.loads(sp.read_text())
            logger.info("k6_summary_read", source="handleSummary", size=sp.stat().st_size)
        elif sep.exists() and sep.stat().st_size > 0:
            raw = json.loads(sep.read_text())
            # Normalize --summary-export format to match handleSummary format
            # summary-export has metrics.X.avg/med/etc directly
            # handleSummary has metrics.X.values.avg/med/etc
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
        metrics = raw.get("metrics", {})
        normalized_metrics = {}
        for key, data in metrics.items():
            if isinstance(data, dict) and "values" not in data and ("med" in data or "avg" in data):
                normalized_metrics[key] = {"values": data}
            else:
                normalized_metrics[key] = data
        return {**raw, "metrics": normalized_metrics}

    @staticmethod
    def _get_metric_values(metric: dict) -> dict:
        """Extract values dict from a metric entry (handles both normalized and raw)."""
        values: dict = metric.get("values", metric)
        return values

    def extract_scenario_latency(self, summary: dict, query_id: str) -> LatencyPercentiles:
        """Extract latency percentiles for a specific scenario."""
        metrics = summary.get("metrics", {})
        custom_key = f"latency_{query_id}"

        if custom_key in metrics:
            values = self._get_metric_values(metrics[custom_key])
        else:
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

    def extract_scenario_iterations(self, summary: dict, query_id: str) -> int:
        """Extract iteration count for a specific scenario."""
        metrics = summary.get("metrics", {})
        # Use the requests counter (Counter metrics include 'count' in handleSummary)
        requests_key = f"requests_{query_id}"
        if requests_key in metrics:
            values = self._get_metric_values(metrics[requests_key])
            return int(values.get("count", 0))
        # Fallback to iteration_duration count
        values = self._get_metric_values(metrics.get("iteration_duration", {}))
        return int(values.get("count", 0))
