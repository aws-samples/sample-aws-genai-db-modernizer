"""OpenSearch k6 script generator for load testing.

Generates k6 scripts that execute OpenSearch queries (search, aggregate,
bulk_index, get_by_id) matching the access patterns from the schema design.
Uses k6's built-in http module to call the OpenSearch REST API.
"""

import json
import tempfile
from pathlib import Path

import structlog

from src.agents.load_test.base import BaseScriptGenerator
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

logger = structlog.get_logger()


class OpenSearchScriptGenerator(BaseScriptGenerator):
    """Generates k6 scripts for OpenSearch load testing via REST API."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def generate_scenario(
        self, access_pattern: dict, table_definition: dict, seed_info: dict
    ) -> str:
        """Generate a k6 scenario script for one access pattern."""
        pattern_id = access_pattern.get("pattern_id", "unknown")
        operation = access_pattern.get("operation", "search")
        index_or_stream = access_pattern.get("index_or_stream", "default")
        opensearch_dsl = access_pattern.get("opensearch_dsl", "{}")
        design_rps = access_pattern.get("design_rps", 1)
        docs_seeded = seed_info.get("docs_seeded", 1000)

        return self._build_scenario_js(
            pattern_id=pattern_id,
            operation=operation,
            index_or_stream=index_or_stream,
            opensearch_dsl=opensearch_dsl,
            design_rps=design_rps,
            max_doc_id=docs_seeded,
        )

    def generate_main(self, scenarios: list, duration_minutes: int, warmup_seconds: int) -> str:
        """Generate the k6 main entry point that orchestrates all scenarios."""
        imports = []
        scenario_configs = []

        for i, scenario in enumerate(scenarios):
            fn_name = f"scenario_{i}"
            imports.append(f'import {{ {fn_name} }} from "./scenario_{i}.js";')
            rps = scenario.get("design_rps", 1)
            scenario_configs.append(f"""    {fn_name}: {{
      executor: "constant-arrival-rate",
      rate: {rps},
      timeUnit: "1s",
      duration: "{duration_minutes}m",
      preAllocatedVUs: {max(10, rps * 2)},
      maxVUs: {max(50, rps * 5)},
      startTime: "{warmup_seconds}s",
      exec: "{fn_name}",
    }},""")

        threshold_lines = ['"http_req_duration": ["p(95)<500"]']
        for i in range(len(scenarios)):
            threshold_lines.append(f'"http_req_duration{{scenario:scenario_{i}}}": ["p(95)<500"]')
        thresholds_str = ",\n    ".join(threshold_lines)

        return f"""// Auto-generated k6 load test for OpenSearch
import {{ textSummary }} from "https://jslib.k6.io/k6-summary/0.0.1/index.js";

{chr(10).join(imports)}

export const options = {{
  scenarios: {{
{chr(10).join(scenario_configs)}
  }},
  thresholds: {{
    {thresholds_str}
  }},
}};

{chr(10).join(f'export {{ scenario_{i} }};' for i in range(len(scenarios)))}

export function handleSummary(data) {{
  const path = __ENV.K6_SUMMARY_PATH || "./k6_summary.json";
  return {{
    [path]: JSON.stringify(data),
    stdout: textSummary(data, {{ indent: " ", enableColors: true }}),
  }};
}}
"""

    def generate_all(
        self,
        access_patterns: list,
        schema_output: dict,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> str:
        """Generate all k6 scripts to a temp directory."""
        scripts_dir = tempfile.mkdtemp(prefix="loadtest_opensearch_")
        scripts_path = Path(scripts_dir)

        scenarios = []
        for i, ap in enumerate(access_patterns):
            index_or_stream = ap.get("index_or_stream", "default")
            seed_info = seed_manifest.resources.get(index_or_stream, {"docs_seeded": 1000})

            scenario_js = self._build_scenario_js(
                pattern_id=f"scenario_{i}",
                operation=ap.get("operation", "search"),
                index_or_stream=index_or_stream,
                opensearch_dsl=ap.get("opensearch_dsl", "{}"),
                design_rps=ap.get("design_rps", 1),
                max_doc_id=seed_info.get("docs_seeded", 1000),
            )

            (scripts_path / f"scenario_{i}.js").write_text(scenario_js)
            scenarios.append(ap)

        main_js = self.generate_main(
            scenarios, test_config.duration_minutes, test_config.warmup_seconds
        )
        (scripts_path / "main.js").write_text(main_js)

        logger.info(
            "scripts_generated",
            scripts_dir=scripts_dir,
            scenario_count=len(scenarios),
        )
        return scripts_dir

    def _build_scenario_js(
        self,
        pattern_id: str,
        operation: str,
        index_or_stream: str,
        opensearch_dsl: str,
        design_rps: float,
        max_doc_id: int,
    ) -> str:
        """Build a single k6 scenario script for an OpenSearch operation."""
        safe_id = pattern_id.replace("-", "_").replace(".", "_")

        # Parse DSL to inject randomization where needed
        dsl_for_js = self._prepare_dsl_for_k6(opensearch_dsl, max_doc_id)

        request_code = self._generate_request_code(operation, index_or_stream, dsl_for_js)

        return f"""// Scenario: {pattern_id} — {operation} on {index_or_stream}
import http from "k6/http";
import {{ check }} from "k6";
import {{ Counter, Trend }} from "k6/metrics";

const latency_{safe_id} = new Trend("latency_{safe_id}", true);
const requests_{safe_id} = new Counter("requests_{safe_id}");
const errors_{safe_id} = new Counter("errors_{safe_id}");

const ENDPOINT = __ENV.OPENSEARCH_ENDPOINT || "localhost";
const AUTH = __ENV.OPENSEARCH_AUTH || "loadtest_admin:password";
const BASE_URL = "https://" + ENDPOINT;
const MAX_DOC_ID = {max_doc_id};

const params = {{
  headers: {{
    "Content-Type": "application/json",
    "Authorization": "Basic " + __ENV.OPENSEARCH_AUTH_B64,
  }},
  timeout: "30s",
}};

export function {safe_id}() {{
  const docId = Math.floor(Math.random() * MAX_DOC_ID) + 1;
{request_code}
  latency_{safe_id}.add(res.timings.duration);
  requests_{safe_id}.add(1);
  const ok = check(res, {{ "status 2xx": (r) => r.status >= 200 && r.status < 300 }});
  if (!ok) {{
    errors_{safe_id}.add(1);
  }}
}}
"""

    def _generate_request_code(self, operation: str, index_or_stream: str, dsl: str) -> str:
        """Generate the HTTP request code for the given operation."""
        match operation.lower():
            case "search":
                return f"""  const body = {dsl};
  const res = http.post(BASE_URL + "/{index_or_stream}/_search", JSON.stringify(body), params);"""
            case "aggregate":
                return f"""  const body = {dsl};
  const res = http.post(BASE_URL + "/{index_or_stream}/_search?size=0", JSON.stringify(body), params);"""
            case "get_by_id":
                return f"""  const res = http.get(BASE_URL + "/{index_or_stream}/_doc/" + docId, params);"""
            case "bulk_index":
                return f"""  const doc = {dsl};
  const res = http.post(BASE_URL + "/{index_or_stream}/_doc", JSON.stringify(doc), params);"""
            case "msearch":
                return f"""  const body = {dsl};
  const res = http.post(BASE_URL + "/_msearch", body, params);"""
            case _:
                return f"""  const body = {dsl};
  const res = http.post(BASE_URL + "/{index_or_stream}/_search", JSON.stringify(body), params);"""

    def _prepare_dsl_for_k6(self, opensearch_dsl: str, max_doc_id: int) -> str:
        """Prepare OpenSearch DSL for use in k6 JavaScript.

        Attempts to parse JSON and return a valid JS object literal.
        Falls back to the raw string wrapped in JSON.parse() if invalid.
        """
        try:
            parsed = json.loads(opensearch_dsl)
            return json.dumps(parsed)
        except (json.JSONDecodeError, TypeError):
            if opensearch_dsl and opensearch_dsl.strip():
                escaped = opensearch_dsl.replace("\\", "\\\\").replace("`", "\\`")
                return f"JSON.parse(`{escaped}`)"
            return '{"query": {"match_all": {}}}'
