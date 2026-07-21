"""DynamoDB k6 script generator using per-operation Jinja templates."""

import math
import re
import tempfile
from pathlib import Path
from typing import Any

import structlog
from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment

from src.agents.load_test.base import BaseScriptGenerator
from src.agents.load_test.dynamodb.key_condition_parser import parse_key_condition
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

logger = structlog.get_logger()

TEMPLATES_DIR = Path(__file__).parent / "templates"


class DynamoDBScriptGenerator(BaseScriptGenerator):
    """Generates k6 scripts from DynamoDB schema design contract."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.env = (
            SandboxedEnvironment(  # nosemgrep: python.lang.security.audit.autoescape-disabled
                loader=FileSystemLoader(str(TEMPLATES_DIR)),
                keep_trailing_newline=True,
                autoescape=False,  # Generating JS code, not HTML — autoescaping would break output
            )
        )

    def generate_scenario(self, access_pattern: dict, table_def: dict, seed_info: dict) -> str:
        """Generate one k6 scenario script for one access pattern."""
        operation = access_pattern["operation"]
        template_name = f"operations/{operation}.js.j2"
        template = self.env.get_template(template_name)

        parsed_kc = parse_key_condition(access_pattern.get("key_condition", ""))

        pk = table_def["partition_key"]
        sk = table_def.get("sort_key")

        context = {
            "query_id": access_pattern.get("query_ids", ["unknown"])[0],
            "description": access_pattern.get("description", ""),
            "table_name": seed_info["table_name"],
            "pk_attr": pk["attribute_name"],
            "pk_type": pk["attribute_type"],
            "pk_count": seed_info["pk_count"],
            "pk_pad_width": seed_info.get("pk_pad_width") or 4,
            "sk_attr": sk["attribute_name"] if sk else None,
            "sk_type": sk["attribute_type"] if sk else None,
            "sk_count": seed_info.get("sk_count"),
            "sk_pad_width": seed_info.get("sk_pad_width") or 4,
            "sk_operator": parsed_kc.sk_operator if parsed_kc else None,
            "sk_literal": parsed_kc.sk_literal if parsed_kc else None,
            "gsi_name": access_pattern.get("gsi_name"),
        }

        return str(template.render(**context))  # nosemgrep: direct-use-of-jinja2

    def generate_main(self, scenarios: list, duration_minutes: int, warmup_seconds: int) -> str:
        """Generate the k6 main.js entry point."""
        template = self.env.get_template("main.js.j2")

        # Safety cap: 10K total VUs max (~10GB) to prevent OOM in 16GB containers
        max_total_vus = 10_000

        enriched = []
        raw_vus_total = sum(
            max(10, math.ceil(max(1, math.ceil(s.get("design_rps", 1))) * 2)) for s in scenarios
        )
        scale = min(1.0, max_total_vus / raw_vus_total) if raw_vus_total > 0 else 1.0

        for i, s in enumerate(scenarios):
            rps = max(1, math.ceil(s.get("design_rps", 1)))
            # Create a safe JS identifier: prefix with 'q', use first 8 chars + index,
            # and replace any non-identifier character (e.g. the '-' in negative source
            # query ids like -7551067248247426933) so the generated JS stays valid.
            qid = s.get("query_id", f"unknown_{i}")
            safe_qid = re.sub(r"\W", "_", qid[:8])
            safe_id = f"q{safe_qid}_{i}"
            max_vus = max(10, math.ceil(rps * 2 * scale))
            enriched.append(
                {
                    **s,
                    "safe_id": safe_id,
                    "design_rps": rps,
                    "pre_allocated_vus": max(2, math.ceil(max_vus * 0.1)),
                    "max_vus": max_vus,
                }
            )

        return str(
            template.render(  # nosemgrep: direct-use-of-jinja2
                scenarios=enriched,
                duration=f"{duration_minutes}m",
                warmup_seconds=warmup_seconds,
            )
        )

    def generate_all(
        self,
        access_patterns: list,
        schema_output: Any,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> str:
        """Generate all scripts to a temp directory. Returns scripts_dir path."""
        scripts_dir = Path(tempfile.mkdtemp(prefix="k6_load_test_"))
        table_defs = {td["table_name"]: td for td in schema_output.get("table_definitions", [])}

        scenarios_meta = []
        seen_query_ids: set[str] = set()

        for ap in access_patterns:
            table_name = ap["table_name"]
            table_def = table_defs.get(table_name)
            if not table_def:
                logger.warning("table_def_not_found", table_name=table_name)
                continue
            seed_info = seed_manifest.resources.get(table_name)
            if not seed_info:
                logger.warning("seed_info_not_found", table_name=table_name)
                continue

            # seed_info may be a dict or a Pydantic model — normalise to dict
            if hasattr(seed_info, "model_dump"):
                seed_info = seed_info.model_dump()

            for qid in ap.get("query_ids", []):
                if qid in seen_query_ids:
                    continue
                seen_query_ids.add(qid)

                try:
                    script = self.generate_scenario(ap, table_def, seed_info)
                except Exception as e:
                    logger.warning("script_gen_failed", query_id=qid, error=str(e))
                    continue

                script_path = scripts_dir / "scenarios" / f"{qid}.js"
                script_path.parent.mkdir(parents=True, exist_ok=True)
                script_path.write_text(script)

                scenarios_meta.append(
                    {
                        "query_id": qid,
                        "script_path": f"scenarios/{qid}.js",
                        "design_rps": ap.get("design_rps", 1),
                    }
                )

        # Copy helpers (at root level so scenarios can import '../helpers/...')
        helpers_dir = scripts_dir / "helpers"
        helpers_dir.mkdir(parents=True, exist_ok=True)
        for helper in ["aws-client.js", "metrics-collector.js"]:
            src = TEMPLATES_DIR / "helpers" / helper
            if src.exists():
                (helpers_dir / helper).write_text(src.read_text())

        # Write main.js
        main = self.generate_main(
            scenarios_meta,
            test_config.duration_minutes,
            test_config.warmup_seconds,
        )
        (scripts_dir / "main.js").write_text(main)

        logger.info(
            "scripts_generated",
            scripts_dir=str(scripts_dir),
            scenario_count=len(scenarios_meta),
        )
        return str(scripts_dir)
