"""A deterministic job whose results page shows the schema-design panels (#987).

The deterministic pipeline never runs schema design (it needs a model), so its
results page never renders the access-pattern explorer, the design trade-offs
or the per-engine design detail. A headless run
does, and the results page's accessibility check found problems only there.

This builds the same state without a model: the deterministic wordpress run,
then a design for each engine the plan keeps -- DynamoDB (one target table per
source table its queries read, reusing the committed design's definitions in
``docs/examples/wordpress/load-test-results`` where it has them, and one access
pattern per query the assignment routes to it), ElastiCache (one key
design and access pattern per cached read) and Aurora MySQL (tables, DDL,
optimizations), the last two built through their contracts -- then synthesis
and the render step again, as ``/modernize`` does after schema design.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection
from pathlib import Path

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline

TEMPLATE_PATH = (
    REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
)


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def _target_name(table: str) -> str:
    """``wordpress.wp_wc_reserved_stock`` -> ``WcReservedStock``."""
    name = _bare(table)
    name = name[3:] if name.startswith("wp_") else name
    return "".join(part.capitalize() for part in name.split("_"))


def dynamodb_served_tables(assignment: dict) -> list[str]:
    """The source tables the version's in-scope DynamoDB queries read, sorted."""
    return sorted(
        {
            str(t)
            for qa in assignment["query_assignments"]
            if qa["assigned_engine"] == "dynamodb" and qa.get("in_scope", True)
            for t in qa.get("source_tables") or []
        }
    )


def write_dynamodb_design(
    store: LocalArtifactStore,
    db: str,
    job_id: str,
    version: int,
    leave_out: Collection[str] = (),
) -> int:
    """Write a DynamoDB design for the version's DynamoDB queries; returns how
    many access patterns it has.

    The table map is the design this job's assignment would get (#1176): one
    target table per source table a DynamoDB query reads, so every mapped table
    is one DynamoDB serves and every table it serves is mapped. A table the
    committed design already defines keeps that definition (keys, attributes);
    the others get a key-value table. ``leave_out`` drops source tables from the
    map, for the explicit check of a design whose map misses tables it serves.
    """
    template = json.loads(TEMPLATE_PATH.read_text())
    assignment = store.read_json(f"{db}/{job_id}/assignment/v{version}/assignment.json")
    queries = [
        qa
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == "dynamodb" and qa.get("in_scope", True)
    ]
    known = {_bare(t["source_tables"][0]): t for t in template["table_definitions"]}
    dropped = {_bare(t) for t in leave_out}
    definitions = []
    for source in dynamodb_served_tables(assignment):
        bare = _bare(source)
        if bare in dropped:
            continue
        if bare in known:
            definitions.append(copy.deepcopy(known[bare]))
            continue
        key = f"{bare}_id"
        definitions.append(
            {
                **copy.deepcopy(template["table_definitions"][0]),
                "table_name": _target_name(source),
                "source_tables": [bare],
                "partition_key": {"attribute_name": key, "attribute_type": "N"},
                "sort_key": None,
                "attributes": [
                    {
                        **template["table_definitions"][0]["attributes"][0],
                        "name": key,
                        "source_table": bare,
                        "source_column": key,
                    }
                ],
            }
        )
    by_source = {d["source_tables"][0]: d["table_name"] for d in definitions}
    tables = [d["table_name"] for d in definitions]

    def target_of(qa: dict, i: int) -> str:
        for t in qa.get("source_tables") or []:
            if _bare(str(t)) in by_source:
                return str(by_source[_bare(str(t))])
        return str(tables[i % len(tables)])

    pattern = template["access_patterns"][0]
    design = copy.deepcopy(template)
    design["job_id"] = job_id
    design["table_definitions"] = definitions
    design["hot_partition_analysis"] = [
        h for h in design.get("hot_partition_analysis") or [] if h.get("table_name") in tables
    ]
    design["access_patterns"] = [
        {
            **pattern,
            "pattern_id": f"DDB-AP-{i + 1}",
            "query_ids": [qa["query_id"]],
            "source_tables": list(qa.get("source_tables") or pattern.get("source_tables") or []),
            "table_name": target_of(qa, i),
        }
        for i, qa in enumerate(queries)
    ]
    design["trade_offs"] = [
        *design.get("trade_offs", []),
        {
            "description": "[PE note] Validate the single-table layout with the team.",
            "impact": "One table serves every access pattern.",
            "source_tables": [],
            "target_tables": tables[:1],
            "query_ids": [queries[0]["query_id"]] if queries else [],
            "engine": "",
        },
    ]
    base = f"{db}/{job_id}/schema-dynamodb/v{version}"
    store.write_json(f"{base}/schema_output.json", design)
    store.write_json(f"{base}/design_trace.json", {"trace": []})
    return len(design["access_patterns"])


def _cached_queries(store: LocalArtifactStore, db: str, job_id: str, version: int) -> list[dict]:
    assignment = store.read_json(f"{db}/{job_id}/assignment/v{version}/assignment.json")
    return [
        qa
        for qa in assignment["query_assignments"]
        if qa.get("cache_engine") and not qa.get("cache_dropped")
    ]


# A long, model-shaped unsupported-pattern text (#950): the executive PDF must
# state its count, never a clipped quote of it.
UNSUPPORTED_CACHE_READ = (
    "Lookup by post title text with a status list, a sort and a limit. Titles are free "
    "text and the cache keys only on the option name, so the design cannot serve this read "
    "from a key, and the status filter changes with every publish."
)


def write_elasticache_design(store: LocalArtifactStore, db: str, job_id: str, version: int) -> None:
    cached = _cached_queries(store, db, job_id, version)
    ids = [str(qa["query_id"]) for qa in cached] or ["q-cache"]
    assignment = store.read_json(f"{db}/{job_id}/assignment/v{version}/assignment.json")
    uncached = next(
        (
            str(qa["query_id"])
            for qa in assignment["query_assignments"]
            if qa.get("in_scope", True) and not qa.get("cache_engine")
        ),
        "q-uncached",
    )
    design = ElastiCacheModelOutputContract.model_validate(
        {
            "job_id": job_id,
            "source_database": db,
            "key_designs": [
                {
                    "key_pattern": "wp:options:{option_name}",
                    "data_type": "string",
                    "use_case": "reference_data",
                    "source_tables": ["wordpress.wp_options"],
                    "fields_mapped": ["option_value"],
                    "ttl_seconds": 300,
                    "rationale": "Autoloaded options are read on every request.",
                    "example_key": "wp:options:siteurl",
                    "example_value": "https://example.com",
                }
            ],
            "access_patterns": [
                {
                    "pattern_id": f"EC-AP-{i + 1}",
                    "description": "Read a cached option by name",
                    "operation": "GET",
                    "key_pattern": "wp:options:{option_name}",
                    "command_example": "GET wp:options:siteurl",
                    "source_tables": ["wordpress.wp_options"],
                    "source_query_ids": [qid],
                }
                for i, qid in enumerate(ids)
            ],
            "trade_offs": [
                {
                    "description": "Cache-aside with a short TTL",
                    "impact": "A changed option is stale for up to five minutes.",
                }
            ],
            "unsupported_patterns": [
                {
                    "source_query_ids": [uncached],
                    "reason": UNSUPPORTED_CACHE_READ,
                    "workaround": "Serve the title lookup from Aurora MySQL with its own index "
                    "and keep only the option reads in the cache.",
                }
            ],
            "validation_passed": True,
        }
    )
    base = f"{db}/{job_id}/schema-elasticache/v{version}"
    store.write_json(f"{base}/schema_output.json", design.model_dump(mode="json"))


def write_aurora_mysql_design(
    store: LocalArtifactStore, db: str, job_id: str, version: int
) -> None:
    design = AuroraMySQLModelOutputContract.model_validate(
        {
            "job_id": job_id,
            "source_database": db,
            "migration_strategy": "carry_over",
            "table_definitions": [
                {
                    "table_name": "wp_posts",
                    "columns": [
                        {"name": "ID", "aurora_type": "BIGINT UNSIGNED", "script_derived": True},
                        {"name": "post_title", "aurora_type": "TEXT", "script_derived": True},
                    ],
                    "primary_key": ["ID"],
                    "indexes": [
                        "CREATE INDEX type_status_date ON wp_posts (post_type, post_status)"
                    ],
                },
                # A second table: two tables (and their query groups) render two
                # embedded tables, which must carry distinct names (landmark-unique).
                {
                    "table_name": "wp_comments",
                    "columns": [
                        {
                            "name": "comment_ID",
                            "aurora_type": "BIGINT UNSIGNED",
                            "script_derived": True,
                        },
                        {
                            "name": "comment_post_ID",
                            "aurora_type": "BIGINT UNSIGNED",
                            "script_derived": True,
                        },
                    ],
                    "primary_key": ["comment_ID"],
                },
            ],
            "generated_ddl": (
                "CREATE TABLE wp_posts (\n  ID BIGINT UNSIGNED NOT NULL,\n"
                "  post_title TEXT,\n  PRIMARY KEY (ID)\n);"
            ),
            "app_layer_notes": [
                {
                    "feature": "trigger",
                    "source_object": "wp_posts_after_insert",
                    "recommendation": "Run the update from the application after the insert.",
                }
            ],
            "optimizations": [
                {
                    "category": "read_replica",
                    "target": "Aurora cluster",
                    "recommendation": "Add a reader for the archive and search pages.",
                    "rationale": "Most traffic is reads.",
                },
                {
                    "category": "io_optimized",
                    "target": "Aurora MySQL cluster",
                    "recommendation": "Use I/O-Optimized storage.",
                    "rationale": "The workload is I/O heavy.",
                },
            ],
            "trade_offs": [
                {
                    "description": "Carry the schema over 1:1",
                    "impact": "No application change for the queries that stay.",
                }
            ],
            "validation_passed": True,
        }
    )
    base = f"{db}/{job_id}/schema-aurora_mysql/v{version}"
    store.write_json(f"{base}/schema_output.json", design.model_dump(mode="json"))


def run_designed_wordpress(
    artifact_root: Path, job_id: str, dynamodb_leave_out: Collection[str] = ()
) -> PipelineResult:
    """The designed wordpress job; ``dynamodb_leave_out`` drops source tables from
    the DynamoDB design's table map (see ``write_dynamodb_design``)."""
    result = run_pipeline("wordpress", artifact_root, job_id)
    store = LocalArtifactStore(base_dir=str(artifact_root))
    version = resolve_downstream_assignment_version(store, "wordpress", job_id)
    assert write_dynamodb_design(store, "wordpress", job_id, version, dynamodb_leave_out) > 0
    write_elasticache_design(store, "wordpress", job_id, version)
    write_aurora_mysql_design(store, "wordpress", job_id, version)
    common = ["--job-id", job_id, "--db", "wordpress", "--artifact-root", str(artifact_root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    return result
