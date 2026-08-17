"""k6 subprocess runner for OpenSearch load tests.

Uses k6's built-in http module to call the OpenSearch REST API.
No custom extensions required (unlike ElastiCache/DynamoDB).
"""

import json
import os
import subprocess  # nosec B404 — intentional subprocess use for k6 CLI
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import structlog

from src.agents.load_test.base import BaseRunner
from src.agents.load_test.models import RunResult
from src.contracts.load_test_models import LatencyPercentiles

logger = structlog.get_logger()


class OpenSearchRunner(BaseRunner):
    """Executes k6 load tests against OpenSearch and parses results."""

    # Operations that carry no query DSL to validate against _search.
    _NON_QUERY_OPS = {"get_by_id", "bulk_index"}

    def dry_run(self, scripts_dir: str, env_vars: dict) -> bool:
        """Validate k6 scripts without executing the full run.

        Two gates:
          1. 'k6 inspect' — parses/validates script syntax (k6 v1 and v2).
          2. Query replay — POSTs each query once to the provisioned domain so
             DSL that OpenSearch rejects (e.g. a top-level ``inner_hits`` → HTTP
             400) fails fast here with the real error, instead of running a full
             k6 pass where every request errors.
        """
        main_js = str(Path(scripts_dir) / "main.js")
        cmd = ["k6", "inspect", "--no-color", main_js]

        merged_env = os.environ.copy()
        merged_env.update(env_vars)

        try:
            result = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit
                cmd, capture_output=True, text=True, timeout=30, env=merged_env
            )
            if result.returncode != 0:
                logger.error("k6_dry_run_failed", stderr=result.stderr[-500:])
                return False
        except subprocess.TimeoutExpired:
            logger.error("k6_dry_run_timeout")
            return False

        failures = self.validate_queries(scripts_dir, env_vars)
        if failures:
            for f in failures:
                logger.error(
                    "dry_run_query_invalid",
                    scenario=f.get("scenario"),
                    query_id=f.get("query_id"),
                    status=f.get("status"),
                    error=f.get("error"),
                )
            return False
        return True

    def validate_queries(
        self,
        scripts_dir: str,
        env_vars: dict,
        http_post: Callable[[str, dict, str], tuple[int, str]] | None = None,
    ) -> list[dict]:
        """Replay each generated query once against the domain to catch invalid DSL.

        Reads ``query_manifest.json`` (written by the script generator) and POSTs
        each query-bearing scenario to ``_search``. Returns a list of failures —
        one per scenario whose query OpenSearch did not accept (non-2xx) or whose
        DSL isn't valid JSON. Returns ``[]`` when there's nothing to validate
        (no manifest, no endpoint, or all queries accepted).

        ``http_post`` is injectable for testing; it defaults to a urllib POST.
        """
        manifest_path = Path(scripts_dir) / "query_manifest.json"
        if not manifest_path.exists():
            return []

        endpoint = env_vars.get("OPENSEARCH_ENDPOINT")
        if not endpoint:
            return []

        if http_post is None:
            http_post = self._default_http_post

        headers = {
            "Content-Type": "application/json",
            "Authorization": "Basic " + env_vars.get("OPENSEARCH_AUTH_B64", ""),
        }

        entries = json.loads(manifest_path.read_text())
        failures: list[dict] = []
        for entry in entries:
            operation = (entry.get("operation") or "search").lower()
            if operation in self._NON_QUERY_OPS:
                continue

            index = entry.get("index", "default")
            dsl = entry.get("dsl", "{}")
            # aggregate mirrors the generator's "_search?size=0"; everything else
            # (search / unknown) posts the body to _search.
            suffix = "?size=0" if operation == "aggregate" else ""
            url = f"https://{endpoint}/{index}/_search{suffix}"

            try:
                json.loads(dsl)
            except (json.JSONDecodeError, TypeError):
                failures.append(
                    {
                        "scenario": entry.get("scenario"),
                        "query_id": entry.get("query_id"),
                        "status": 0,
                        "error": f"opensearch_dsl is not valid JSON: {dsl[:200]}",
                    }
                )
                continue

            status, text = http_post(url, headers, dsl)
            if not 200 <= status < 300:
                failures.append(
                    {
                        "scenario": entry.get("scenario"),
                        "query_id": entry.get("query_id"),
                        "status": status,
                        "error": self._extract_error_reason(text) or text[:300],
                    }
                )
        return failures

    @staticmethod
    def _default_http_post(url: str, headers: dict, body: str) -> tuple[int, str]:
        """POST a query to the domain; returns (status_code, response_text)."""
        # Defense-in-depth: only ever open our provisioned https OpenSearch
        # endpoint (urllib would otherwise honor file://, etc.).
        if not url.startswith("https://"):
            return (0, f"refusing non-https url: {url[:80]}")
        req = urllib.request.Request(  # nosec B310 — https-only url built from our domain
            url, data=body.encode(), headers=headers, method="POST"
        )
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                return (resp.status, resp.read().decode(errors="replace"))
        except urllib.error.HTTPError as exc:
            return (exc.code, exc.read().decode(errors="replace"))
        except urllib.error.URLError as exc:
            return (0, str(exc))

    @staticmethod
    def _extract_error_reason(text: str) -> str | None:
        """Pull OpenSearch's error reason out of a JSON error response."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            if err.get("reason"):
                return str(err["reason"])
            root = err.get("root_cause")
            if isinstance(root, list) and root and isinstance(root[0], dict):
                reason = root[0].get("reason")
                return str(reason) if reason is not None else None
        if isinstance(err, str):
            return err
        return None

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

        The handler passes a query_id. Resolution order:
          1. custom per-query Trend "latency_{query_id}" (preferred — this is
             what the scripts emit and what maps 1:1 to an access pattern)
          2. per-scenario tag "http_req_duration{scenario:...}"
          3. global "http_req_duration" (fallback)
        All values are milliseconds.
        """
        metrics = summary.get("metrics", {})

        custom_key = f"latency_{scenario_name}"
        scenario_key = f"http_req_duration{{scenario:{scenario_name}}}"
        if custom_key in metrics:
            values = self._get_metric_values(metrics[custom_key])
        elif scenario_key in metrics:
            values = self._get_metric_values(metrics[scenario_key])
        else:
            values = {}
            for key in metrics:
                if "http_req_duration" in key and scenario_name in key:
                    values = self._get_metric_values(metrics[key])
                    break
            if not values:
                values = self._get_metric_values(metrics.get("http_req_duration", {}))

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
        """Extract request count for a scenario.

        Prefers the custom "requests_{query_id}" Counter (emitted by the
        scripts), then the per-scenario http_req_duration tag, then the global.
        """
        metrics = summary.get("metrics", {})

        requests_key = f"requests_{scenario_name}"
        if requests_key in metrics:
            values = self._get_metric_values(metrics[requests_key])
            return int(values.get("count", 0))

        scenario_key = f"http_req_duration{{scenario:{scenario_name}}}"
        if scenario_key in metrics:
            values = self._get_metric_values(metrics[scenario_key])
            return int(values.get("count", 0))

        values = self._get_metric_values(metrics.get("http_req_duration", {}))
        return int(values.get("count", 0))
