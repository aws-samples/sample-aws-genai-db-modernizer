"""DocumentDB k6 script generator using per-operation Jinja templates.

Mirrors :class:`DynamoDBScriptGenerator` structure but emits xk6-mongo-based
scripts targeting DocumentDB. The generated test scripts ARE the customer
deliverable per ADR-020 — they're working JavaScript that produces the same
results the load test reports.

Generated layout (under a tempdir):

  scenarios/<query_id>.js   one per access pattern; imports helpers
  helpers/client.js         xk6-mongo client + operation dispatcher
  helpers/key-generator.js  random key generation matching seeder format
  helpers/metrics.js        per-query Trend/Counter
  main.js                   entry point with constant-arrival-rate executors
"""

import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog
from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment

from src.agents.load_test.base import BaseScriptGenerator
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

logger = structlog.get_logger()

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Operations supported by DocumentDB schema design — must have a matching
# template at templates/operations/<operation>.js.j2
SUPPORTED_OPERATIONS = {
    "findOne",
    "find",
    "aggregate",
    "insertOne",
    "insertMany",
    "updateOne",
    "updateMany",
    "deleteOne",
    "deleteMany",
    "bulkWrite",
}

# Helper files copied verbatim into scripts_dir/helpers/
HELPER_FILES = ("client.js", "key-generator.js", "metrics.js")

# Safety cap on total VUs (matches DynamoDB script generator)
MAX_TOTAL_VUS = 10_000


class DocumentDBScriptGenerator(BaseScriptGenerator):
    """Generates k6 + xk6-mongo scripts from DocumentDB schema design output."""

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region
        self.env = SandboxedEnvironment(  # nosemgrep: python.lang.security.audit.autoescape-disabled
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            keep_trailing_newline=True,
            # Generating JS code, not HTML — autoescaping would corrupt output
            autoescape=False,
        )

    # =========================================================================
    # BaseScriptGenerator API
    # =========================================================================

    def generate_scenario(
        self,
        access_pattern: dict[str, Any],
        collection_def: dict[str, Any],
        seed_info: dict[str, Any],
    ) -> str:
        """Generate one k6 scenario script for one access pattern."""
        operation = access_pattern.get("operation")
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError(
                f"DocumentDB script generator does not support operation '{operation}'. "
                f"Supported: {sorted(SUPPORTED_OPERATIONS)}"
            )

        template = self.env.get_template(f"operations/{operation}.js.j2")

        context = {
            "query_id": self._extract_query_id(access_pattern),
            "description": access_pattern.get("description", ""),
            "collection_name": seed_info["collection_name"],
            "primary_key_field": seed_info.get("primary_key_field", "primary_id"),
            "primary_key_count": seed_info["primary_key_count"],
        }
        return str(template.render(**context))  # nosemgrep: direct-use-of-jinja2

    def generate_main(
        self, scenarios: list[dict[str, Any]], duration_minutes: int, warmup_seconds: int
    ) -> str:
        """Generate the k6 main.js entry point."""
        template = self.env.get_template("main.js.j2")

        # Compute pre-allocated and max VUs per scenario, scaling down if total
        # would exceed MAX_TOTAL_VUS to fit in 16 GB Fargate task memory.
        raw_total = sum(
            max(10, math.ceil(max(1, math.ceil(s.get("design_rps", 1))) * 2)) for s in scenarios
        )
        scale = min(1.0, MAX_TOTAL_VUS / raw_total) if raw_total > 0 else 1.0

        enriched: list[dict[str, Any]] = []
        for i, s in enumerate(scenarios):
            rps = max(1, math.ceil(s.get("design_rps", 1)))
            qid = s.get("query_id", f"unknown_{i}")
            safe_id = f"q{qid[:8]}_{i}"  # JS-identifier-safe + unique
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
        access_patterns: list[dict[str, Any]],
        schema_output: Any,
        seed_manifest: SeedManifest,
        test_config: TestConfig,
    ) -> str:
        """Generate all scripts to a temp directory. Returns scripts_dir path."""
        scripts_dir = Path(tempfile.mkdtemp(prefix="k6_documentdb_load_test_"))

        # Index collections by primary source table for fast lookup
        collections_by_source: dict[str, dict[str, Any]] = {}
        for collection_def in schema_output.get("collections", []):
            sources = collection_def.get("source_tables") or []
            if sources:
                collections_by_source[sources[0]] = collection_def

        scenarios_meta: list[dict[str, Any]] = []
        seen_query_ids: set[str] = set()

        for ap in access_patterns:
            source_table = self._extract_source_table(ap)
            if source_table is None:
                logger.warning("documentdb_no_source_table_for_pattern", access_pattern=ap)
                continue

            collection_def = collections_by_source.get(source_table)
            if not collection_def:
                logger.warning(
                    "documentdb_no_collection_def_for_source",
                    source_table=source_table,
                )
                continue

            seed_info_raw = seed_manifest.resources.get(source_table)
            if not seed_info_raw:
                logger.warning(
                    "documentdb_no_seed_info_for_source",
                    source_table=source_table,
                )
                continue

            # SeedManifest.resources values may be dict or Pydantic models
            seed_info: dict[str, Any] = (
                seed_info_raw.model_dump()
                if hasattr(seed_info_raw, "model_dump")
                else seed_info_raw
            )

            qid = self._extract_query_id(ap)
            if qid in seen_query_ids:
                continue
            seen_query_ids.add(qid)

            try:
                script = self.generate_scenario(ap, collection_def, seed_info)
            except Exception as exc:
                logger.warning("documentdb_script_gen_failed", query_id=qid, error=str(exc))
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

        # Copy helpers verbatim
        helpers_dir = scripts_dir / "helpers"
        helpers_dir.mkdir(parents=True, exist_ok=True)
        for helper in HELPER_FILES:
            src = TEMPLATES_DIR / "helpers" / helper
            if src.exists():
                shutil.copyfile(src, helpers_dir / helper)
            else:
                logger.warning("documentdb_helper_missing", helper=helper)

        # Write main.js
        main = self.generate_main(
            scenarios_meta, test_config.duration_minutes, test_config.warmup_seconds
        )
        (scripts_dir / "main.js").write_text(main)

        logger.info(
            "documentdb_scripts_generated",
            scripts_dir=str(scripts_dir),
            scenario_count=len(scenarios_meta),
        )
        return str(scripts_dir)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _extract_query_id(self, access_pattern: dict[str, Any]) -> str:
        """Extract a stable query_id from an access pattern.

        DocumentDB schema_design's AccessPattern uses ``pattern_id``; older
        contracts use ``query_ids[0]``. Try both for forward/back compat.
        """
        if "pattern_id" in access_pattern:
            return str(access_pattern["pattern_id"])
        query_ids = access_pattern.get("query_ids")
        if query_ids:
            return str(query_ids[0])
        return "unknown"

    def _extract_source_table(self, access_pattern: dict[str, Any]) -> str | None:
        """Extract the primary source table targeted by an access pattern.

        DocumentDB AccessPattern doesn't directly carry a source_table — the
        coordinator resolves it via the collection_def. We accept several keys
        for forward compat:

          - ``source_table`` (explicit)
          - ``collections[0]`` (collection name → not source table; needs reverse lookup)
          - ``table_name`` (DynamoDB convention)
        """
        source_table = access_pattern.get("source_table") or access_pattern.get("table_name")
        if source_table:
            return str(source_table)
        return None
