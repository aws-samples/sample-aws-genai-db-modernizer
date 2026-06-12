"""ElastiCache (Valkey/Redis) k6 script generator for load testing.

Generates k6/xk6-redis scripts that execute Redis commands matching
the access patterns from the schema design.
"""
import tempfile
from pathlib import Path

import structlog

from src.agents.load_test.base import BaseScriptGenerator
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

logger = structlog.get_logger()


class ElastiCacheScriptGenerator(BaseScriptGenerator):
    """Generates k6 scripts for Redis/Valkey load testing."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def generate_scenario(
        self, access_pattern: dict, table_definition: dict, seed_info: dict
    ) -> str:
        """Generate a k6 scenario script for one access pattern."""
        pattern_id = access_pattern.get("pattern_id", "unknown")
        operation = access_pattern.get("operation", "GET")
        key_pattern = access_pattern.get("key_pattern", "key:{id}")
        command_example = access_pattern.get("command_example", "")
        design_rps = access_pattern.get("design_rps", 1)

        items_seeded = seed_info.get("items_seeded", 1000)

        return self._build_scenario_js(
            pattern_id=pattern_id,
            operation=operation,
            key_pattern=key_pattern,
            command_example=command_example,
            design_rps=design_rps,
            max_key_id=items_seeded,
        )

    def generate_main(self, scenarios: list, duration_minutes: int, warmup_seconds: int) -> str:
        """Generate the k6 main entry point that orchestrates all scenarios."""
        imports = []
        scenario_configs = []

        for i, scenario in enumerate(scenarios):
            fn_name = f"scenario_{i}"
            imports.append(f'import {{ {fn_name} }} from "./scenario_{i}.js";')
            rps = scenario.get("design_rps", 1)
            scenario_configs.append(
                f"""
    {fn_name}: {{
      executor: "constant-arrival-rate",
      rate: {rps},
      timeUnit: "1s",
      duration: "{duration_minutes}m",
      preAllocatedVUs: {max(10, rps * 2)},
      maxVUs: {max(50, rps * 5)},
      startTime: "{warmup_seconds}s",
      exec: "{fn_name}",
    }},"""
            )

        # Build per-scenario thresholds to force k6 to split iteration_duration by scenario
        threshold_lines = ['"iteration_duration": ["p(95)<50"]']
        for i in range(len(scenarios)):
            threshold_lines.append(
                f'"iteration_duration{{scenario:scenario_{i}}}": ["p(95)<50"]'
            )
        thresholds_str = ",\n    ".join(threshold_lines)

        return f"""// Auto-generated k6 load test for ElastiCache/Valkey
import {{ textSummary }} from "https://jslib.k6.io/k6-summary/0.0.1/index.js";

{chr(10).join(imports)}

export const options = {{
  scenarios: {{{chr(10).join(scenario_configs)}
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
        scripts_dir = tempfile.mkdtemp(prefix="loadtest_elasticache_")
        scripts_path = Path(scripts_dir)

        scenarios = []
        for i, ap in enumerate(access_patterns):
            key_pattern = ap.get("key_pattern", "key:{id}")
            seed_info = seed_manifest.resources.get(key_pattern, {"items_seeded": 1000})

            scenario_js = self._build_scenario_js(
                pattern_id=f"scenario_{i}",
                operation=ap.get("operation", "GET"),
                key_pattern=key_pattern,
                command_example=ap.get("command_example", ""),
                design_rps=ap.get("design_rps", 1),
                max_key_id=seed_info.get("items_seeded", 1000),
            )

            (scripts_path / f"scenario_{i}.js").write_text(scenario_js)
            scenarios.append(ap)

        # Generate main.js
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
        key_pattern: str,
        command_example: str,
        design_rps: float,
        max_key_id: int,
    ) -> str:
        """Build a single k6 scenario script for a Redis operation."""
        # Determine the Redis command category
        read_ops = {
            "GET",
            "MGET",
            "HGET",
            "HMGET",
            "HGETALL",
            "LRANGE",
            "SMEMBERS",
            "SISMEMBER",
            "ZRANGE",
            "ZREVRANGE",
            "ZRANGEBYSCORE",
            "ZRANK",
            "ZSCORE",
            "XRANGE",
            "XREVRANGE",
            "GEOSEARCH",
            "PFCOUNT",
            "JSON.GET",
            "JSON.MGET",
            "EXISTS",
            "TTL",
            "SCARD",
            "ZCARD",
            "LLEN",
        }

        is_read = operation.upper() in read_ops
        op_type = "read" if is_read else "write"

        # Sanitize pattern_id for use as JS identifier
        safe_id = pattern_id.replace("-", "_").replace(".", "_")

        return f"""// Scenario: {pattern_id} — {operation} on {key_pattern}
// Command: {command_example}
import redis from "k6/x/redis";
import {{ Counter }} from "k6/metrics";

const requests_{safe_id} = new Counter("requests_{safe_id}");
const errors_{safe_id} = new Counter("errors_{safe_id}");

const ENDPOINT = __ENV.ELASTICACHE_ENDPOINT || "localhost";
const PORT = __ENV.ELASTICACHE_PORT || "6379";
const MAX_KEY_ID = {max_key_id};

const client = new redis.Client("rediss://" + ENDPOINT + ":" + PORT);

export async function {safe_id}() {{
  const keyId = Math.floor(Math.random() * MAX_KEY_ID) + 1;
  const key = "{key_pattern}".replace(/\\{{[^}}]+\\}}/g, keyId.toString());

  try {{
    await {self._generate_command_js(operation, op_type)}
    requests_{safe_id}.add(1);
  }} catch (e) {{
    errors_{safe_id}.add(1);
  }}
}}
"""

    def _generate_command_js(self, operation: str, op_type: str) -> str:
        """Generate the JS Redis command call."""
        op = operation.upper()
        match op:
            case "GET":
                return "client.get(key);"
            case "SET":
                return 'client.set(key, "value_" + keyId);'
            case "HGET":
                return 'client.hget(key, "field1");'
            case "HSET":
                return 'client.hset(key, "field1", "value_" + keyId);'
            case "HGETALL":
                return "client.hgetall(key);"
            case "HMGET":
                return 'client.hmget(key, "field1", "field2", "field3");'
            case "LPUSH":
                return 'client.lpush(key, "item_" + keyId);'
            case "RPUSH":
                return 'client.rpush(key, "item_" + keyId);'
            case "LRANGE":
                return "client.lrange(key, 0, -1);"
            case "SADD":
                return 'client.sadd(key, "member_" + keyId);'
            case "SMEMBERS":
                return "client.smembers(key);"
            case "SISMEMBER":
                return 'client.sismember(key, "member_1");'
            case "ZADD":
                return 'client.zadd(key, keyId, "member_" + keyId);'
            case "ZRANGE":
                return "client.zrange(key, 0, 9);"
            case "ZREVRANGE":
                return "client.zrevrange(key, 0, 9);"
            case "ZRANGEBYSCORE":
                return "client.zrangebyscore(key, 0, keyId);"
            case "ZRANK":
                return 'client.zrank(key, "member_1");'
            case "ZSCORE":
                return 'client.zscore(key, "member_1");'
            case "XADD":
                return 'client.xadd(key, "*", "data", "event_" + keyId);'
            case "XRANGE":
                return 'client.xrange(key, "-", "+", 10);'
            case "GEOADD":
                return 'client.geoadd(key, -74.0 + Math.random(), 40.0 + Math.random(), "loc_" + keyId);'
            case "GEOSEARCH":
                return 'client.geosearch(key, "FROMMEMBER", "loc_1", "BYRADIUS", 10, "km");'
            case "PFADD":
                return 'client.pfadd(key, "element_" + keyId);'
            case "PFCOUNT":
                return "client.pfcount(key);"
            case "DEL":
                return "client.del(key);"
            case "INCR" | "INCRBY":
                return "client.incr(key);"
            case "JSON.GET":
                return 'client.sendCommand("JSON.GET", key, "$");'
            case "JSON.SET":
                return 'client.sendCommand("JSON.SET", key, "$", JSON.stringify({field1: "val_" + keyId}));'
            case _:
                return f'client.sendCommand("{op}", key);'
