"""Load test coordinator — engine-agnostic orchestrator."""

import os
import uuid

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner, BaseRunner, BaseScriptGenerator, BaseSeeder
from src.agents.load_test.dynamodb.script_generator import sanitize_metric_id
from src.agents.load_test.models import RunResult
from src.agents.query_journey_materializer import materialize_load_test
from src.contracts.load_test_models import (
    InfrastructureManifest,
    LatencyPercentiles,
    LoadTestOutput,
    PatternResult,
    SeedSummary,
    TestConfig,
)
from src.storage.artifact_store import ArtifactStore

logger = structlog.get_logger()

RRU_PRICE = 0.25 / 1_000_000
WRU_PRICE = 1.25 / 1_000_000


def create_engine_components(
    target_engine: str, region: str
) -> tuple[BaseProvisioner, BaseSeeder, BaseScriptGenerator, BaseRunner]:
    """Factory: create engine-specific components."""
    match target_engine:
        case "dynamodb":
            from src.agents.load_test.dynamodb import (
                DynamoDBProvisioner,
                DynamoDBScriptGenerator,
                DynamoDBSeeder,
                K6Runner,
            )

            return (
                DynamoDBProvisioner(region=region),
                DynamoDBSeeder(region=region),
                DynamoDBScriptGenerator(region=region),
                K6Runner(),
            )

        case "documentdb":
            from src.agents.load_test.documentdb import (
                DocumentDBProvisioner,
                DocumentDBScriptGenerator,
                DocumentDBSeeder,
            )
            from src.agents.load_test.dynamodb.runner import K6Runner

            return (
                DocumentDBProvisioner(region=region),
                DocumentDBSeeder(region=region),
                DocumentDBScriptGenerator(region=region),
                K6Runner(),
            )
        case "elasticache":
            from src.agents.load_test.elasticache import (
                ElastiCacheProvisioner,
                ElastiCacheScriptGenerator,
                ElastiCacheSeeder,
                ValkeyRunner,
            )

            return (
                ElastiCacheProvisioner(region=region),
                ElastiCacheSeeder(region=region),
                ElastiCacheScriptGenerator(region=region),
                ValkeyRunner(),
            )

        case "opensearch":
            from src.agents.load_test.opensearch import (
                OpenSearchProvisioner,
                OpenSearchRunner,
                OpenSearchScriptGenerator,
                OpenSearchSeeder,
            )

            return (
                OpenSearchProvisioner(region=region),
                OpenSearchSeeder(region=region),
                OpenSearchScriptGenerator(region=region),
                OpenSearchRunner(),
            )
        case _:
            raise ValueError(f"Unsupported engine: {target_engine}")


def _base_path(database_name: str, job_id: str, schema_version: int, target_engine: str) -> str:
    return f"{database_name}/{job_id}/load-test-{target_engine}/v{schema_version}"


def _get_testable_patterns(schema_output: dict) -> list[dict]:
    """Filter access patterns: in_scope=True AND design_rps > 0."""
    return [
        ap
        for ap in schema_output.get("access_patterns", [])
        if ap.get("in_scope", True) and ap.get("design_rps", 0) > 0
    ]


def _enrich_elasticache_patterns(schema_output: dict, query_map: dict) -> list[dict]:
    """Enrich ElastiCache access patterns with design_rps from collector CPS.

    ElastiCache schema designs don't include design_rps — derive it from
    the max calls_per_second of the source queries for each pattern.
    """
    enriched = []
    for ap in schema_output.get("access_patterns", []):
        source_qids = ap.get("source_query_ids") or []
        max_cps = max(
            (query_map.get(qid, {}).get("calls_per_second", 0) or 0 for qid in source_qids),
            default=0,
        )
        # Only include patterns with meaningful throughput
        if max_cps > 0:
            enriched_ap = {**ap, "design_rps": max(1, int(max_cps)), "in_scope": True}
            enriched.append(enriched_ap)
    return enriched


def _resolve_aws_credentials() -> dict[str, str]:
    """Resolve AWS credentials for k6 subprocess."""
    env_vars: dict[str, str] = {}
    if "AWS_ACCESS_KEY_ID" in os.environ:
        env_vars["AWS_ACCESS_KEY_ID"] = os.environ["AWS_ACCESS_KEY_ID"]
        env_vars["AWS_SECRET_ACCESS_KEY"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        if "AWS_SESSION_TOKEN" in os.environ:
            env_vars["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
    else:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials:
            frozen = credentials.get_frozen_credentials()
            env_vars["AWS_ACCESS_KEY_ID"] = frozen.access_key
            env_vars["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
            if frozen.token:
                env_vars["AWS_SESSION_TOKEN"] = frozen.token
    return env_vars


def run_load_test(
    job_id: str,
    database_name: str,
    target_engine: str,
    store: ArtifactStore,
    schema_version: int = 1,
    test_config: TestConfig | None = None,
    region: str = "us-east-1",
) -> LoadTestOutput | None:
    """Orchestrate: provision -> seed -> generate -> dry-run -> run -> parse -> teardown."""
    if test_config is None:
        test_config = TestConfig()

    run_id = uuid.uuid4().hex[:12]
    base = _base_path(database_name, job_id, schema_version, target_engine)
    log = logger.bind(job_id=job_id, run_id=run_id, target_engine=target_engine)

    # DynamoDB, DocumentDB, ElastiCache and OpenSearch are implemented currently
    SUPPORTED_ENGINES = {"dynamodb", "documentdb", "elasticache", "opensearch"}
    if target_engine not in SUPPORTED_ENGINES:
        log.info("load_test_skipped", reason=f"not implemented for {target_engine}")
        store.write_json(
            f"{base}/result.json",
            {
                "status": "skipped",
                "reason": f"Load testing not implemented for {target_engine}",
                "run_id": run_id,
                "target_engine": target_engine,
            },
        )
        return None

    log.info("load_test_start")

    # 1. Read schema output from S3
    schema_key = (
        f"{database_name}/{job_id}/schema-{target_engine}/v{schema_version}/schema_output.json"
    )
    collector_key = f"{database_name}/{job_id}/collector/output.json"

    schema_output = store.read_json(schema_key)
    collector_output = store.read_json(collector_key)

    # Build query lookup from collector
    query_patterns = collector_output.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in query_patterns}

    # 2. Filter testable patterns
    access_patterns = _get_testable_patterns(schema_output)

    # For ElastiCache: access patterns don't have design_rps — derive from collector CPS
    if not access_patterns and target_engine == "elasticache":
        access_patterns = _enrich_elasticache_patterns(schema_output, query_map)

    log.info("testable_patterns", count=len(access_patterns))

    # 3. Create engine components
    provisioner, seeder, generator, runner = create_engine_components(target_engine, region)

    manifest: InfrastructureManifest | None = None
    try:
        # 4. Provision
        tags = {"job_id": job_id, "run_id": run_id, "database_name": database_name}

        # DocumentDB and OpenSearch provisioners read collector_output for sizing
        # (the BaseProvisioner signature only carries schema_output + tags).
        if target_engine in ("documentdb", "opensearch"):
            schema_output["_collector_output"] = collector_output
        if target_engine == "documentdb":
            schema_output["_test_config"] = test_config

        manifest = provisioner.provision(schema_output, tags)
        log.info("provisioned", resources=len(manifest.resources))

        # DocumentDB seeder + script_generator need the cluster endpoint and
        # replica count from the deployed manifest. Stuff them on schema_output
        # so they're available without changing BaseSeeder / BaseScriptGenerator
        # signatures.
        if target_engine == "documentdb":
            cluster_resource = next(
                (r for r in manifest.resources if r.resource_type == "AWS::DocDB::DBCluster"),
                None,
            )
            if cluster_resource is not None:
                schema_output["_documentdb_endpoint"] = cluster_resource.configuration[
                    "cluster_endpoint"
                ]
                schema_output["_documentdb_replica_count"] = cluster_resource.configuration.get(
                    "replica_count", 0
                )

        # 4b. Inject provisioned ElastiCache endpoint into schema_output for seeder/generator
        for resource in manifest.resources:
            if resource.resource_type == "AWS::ElastiCache::ReplicationGroup":
                schema_output["_cluster_endpoint"] = resource.configuration.get(
                    "endpoint_address", ""
                )
                schema_output["_cluster_port"] = resource.configuration.get("endpoint_port", 6379)

        # OpenSearch seeder + script_generator need the domain endpoint and
        # master credentials from the provisioned manifest.
        if target_engine == "opensearch":
            os_resource = next(
                (
                    r
                    for r in manifest.resources
                    if r.resource_type == "AWS::OpenSearchService::Domain"
                ),
                None,
            )
            if os_resource is not None:
                schema_output["_opensearch_endpoint"] = os_resource.configuration["endpoint"]
                schema_output["_opensearch_master_user"] = os_resource.configuration.get(
                    "master_user", "loadtest_admin"
                )
                schema_output["_opensearch_master_password"] = os_resource.configuration.get(
                    "master_password", ""
                )

        # 5. Seed
        seed_manifest = seeder.seed(schema_output, max_items_per_table=10_000)
        log.info("seeded", total_items=seed_manifest.total_items)

        # 6. Generate scripts
        scripts_dir = generator.generate_all(
            access_patterns, schema_output, seed_manifest, test_config
        )
        log.info("scripts_generated", scripts_dir=scripts_dir)

        # 7. Resolve env vars
        env_vars = _resolve_aws_credentials()
        env_vars["AWS_REGION"] = region

        # Inject ElastiCache endpoint for k6 scripts
        if schema_output.get("_cluster_endpoint"):
            env_vars["ELASTICACHE_ENDPOINT"] = schema_output["_cluster_endpoint"]
            env_vars["ELASTICACHE_PORT"] = str(schema_output.get("_cluster_port", 6379))

        # Inject OpenSearch endpoint + auth for k6 scripts
        if target_engine == "opensearch" and schema_output.get("_opensearch_endpoint"):
            import base64

            endpoint = schema_output["_opensearch_endpoint"]
            user = schema_output.get("_opensearch_master_user", "loadtest_admin")
            password = schema_output.get("_opensearch_master_password", "")
            env_vars["OPENSEARCH_ENDPOINT"] = endpoint
            env_vars["OPENSEARCH_AUTH"] = f"{user}:{password}"
            env_vars["OPENSEARCH_AUTH_B64"] = base64.b64encode(
                f"{user}:{password}".encode()
            ).decode()

        # 8. Dry-run
        if not runner.dry_run(scripts_dir, env_vars):
            log.error("dry_run_failed")
            raise RuntimeError("k6 dry-run validation failed")

        # 9. Full run
        run_result = runner.run(scripts_dir, test_config.duration_minutes, env_vars)
        log.info(
            "run_complete",
            returncode=run_result.returncode,
            has_summary=run_result.summary is not None,
            summary_metrics_count=(
                len(run_result.summary.get("metrics", {})) if run_result.summary else 0
            ),
        )

        # 9b. Write k6 diagnostics to S3 for debugging
        summary_metrics = list((run_result.summary or {}).get("metrics", {}).keys())
        store.write_json(
            f"{base}/k6_diagnostics.json",
            {
                "returncode": run_result.returncode,
                "has_summary": run_result.summary is not None,
                "summary_metrics_count": len(summary_metrics),
                "summary_metric_keys": summary_metrics[:100],
                "stderr_tail": (run_result.stderr or "")[-2000:],
                "stdout_tail": (run_result.stdout or "")[-2000:],
            },
        )

        # 10. Parse results
        pattern_results = _build_pattern_results(
            run_result, access_patterns, query_map, runner, base
        )

        # 11. Build output
        output = _build_output(
            run_id,
            schema_version,
            target_engine,
            test_config,
            manifest,
            seed_manifest,
            pattern_results,
        )

        # 12. Write artifacts
        _write_artifacts(
            store,
            base,
            output,
            manifest,
            seed_manifest,
            pattern_results,
            test_config,
            job_id,
            run_id,
            database_name,
        )

        # 13. Enrich query journeys
        journey_data = [pr.model_dump() for pr in pattern_results]
        materialize_load_test(journey_data, database_name, job_id, store)

        log.info("load_test_complete", patterns_tested=output.total_patterns_tested)
        return output

    finally:
        if manifest:
            log.info("tearing_down")
            provisioner.teardown(manifest)


def _build_pattern_results(
    run_result: RunResult,
    access_patterns: list[dict],
    query_map: dict[str, dict],
    runner,
    base: str,
) -> list[PatternResult]:
    """Build PatternResult objects from k6 run results."""
    summary = run_result.summary or {}
    metrics = summary.get("metrics", {})
    results: list[PatternResult] = []
    seen: set[str] = set()

    for ap in access_patterns:
        for qid in ap.get("query_ids", []) or ap.get("source_query_ids", []):
            if qid in seen or qid not in query_map:
                continue
            seen.add(qid)

            # k6 emits per-pattern metrics keyed by the sanitized query id
            # (latency_<metric_id>, requests_<metric_id>, consumed_rcu_<metric_id>,
            # etc.). Read them back with the same transform so the keys match.
            metric_id = sanitize_metric_id(qid)

            collector_query = query_map[qid]
            source_p50_raw = float(collector_query.get("execution_time_ms_p50") or 1.0)

            # Add estimated network overhead to source latency for fair comparison.
            # Collector metrics are database-internal execution time only (no network).
            # Application-observed latency includes: TCP round-trip, protocol overhead,
            # connection pool checkout, and TLS handshake amortization.
            # Typical same-VPC overhead: 0.5–1.5ms for MySQL/PostgreSQL.
            source_network_overhead_ms = 1.0
            source_p50 = source_p50_raw + source_network_overhead_ms

            source_latency = LatencyPercentiles(
                p50=source_p50,
                p90=float(collector_query.get("execution_time_ms_p90") or source_p50_raw * 1.5)
                + source_network_overhead_ms,
                p95=float(collector_query.get("execution_time_ms_p95") or source_p50_raw * 2.0)
                + source_network_overhead_ms,
                p99=float(collector_query.get("execution_time_ms_p99") or source_p50_raw * 3.0)
                + source_network_overhead_ms,
                p999=float(collector_query.get("execution_time_ms_p999") or source_p50_raw * 5.0)
                + source_network_overhead_ms,
                min=float(collector_query.get("execution_time_ms_min") or source_p50_raw * 0.3)
                + source_network_overhead_ms,
                max=float(collector_query.get("execution_time_ms_max") or source_p50_raw * 10.0)
                + source_network_overhead_ms,
            )

            target_latency = runner.extract_scenario_latency(summary, metric_id)
            total_requests = runner.extract_scenario_iterations(summary, metric_id)

            target_p50 = target_latency.p50
            improvement = (source_p50 / target_p50) if target_p50 > 0 else 0.0

            rcu_avg = float(
                metrics.get(f"consumed_rcu_{metric_id}", {}).get("values", {}).get("avg", 0.0)
            )
            wcu_avg = float(
                metrics.get(f"consumed_wcu_{metric_id}", {}).get("values", {}).get("avg", 0.0)
            )
            error_count = int(
                metrics.get(f"errors_{metric_id}", {}).get("values", {}).get("count", 0)
            )
            error_rate = (error_count / total_requests * 100.0) if total_requests > 0 else 0.0

            results.append(
                PatternResult(
                    query_id=qid,
                    access_pattern_description=ap.get("description", ""),
                    original_query_text=collector_query.get("query_text", ""),
                    operation_type=ap.get("operation", ""),
                    steps=[ap.get("operation", "")],
                    source_latency_ms=source_latency,
                    target_latency_ms=target_latency,
                    improvement_factor=improvement,
                    throughput_rps=float(ap.get("design_rps", 0)),
                    total_requests=total_requests,
                    error_count=error_count,
                    error_rate_pct=error_rate,
                    throttle_count=0,
                    cost_per_operation_usd=(rcu_avg * RRU_PRICE) + (wcu_avg * WRU_PRICE),
                    consumed_capacity_avg=rcu_avg + wcu_avg,
                    code_artifact_path=f"{base}/scenarios/{qid}.js",
                )
            )

    return results


def _build_output(
    run_id, schema_version, target_engine, test_config, manifest, seed_manifest, pattern_results
) -> LoadTestOutput:
    total_cost = sum(r.cost_per_operation_usd for r in pattern_results)
    # A pattern that received zero requests is a failure, not a pass: error_rate
    # is error_count/total_requests, which is 0% when nothing ran — that would
    # otherwise let a broken run (no traffic reached the target) report green.
    patterns_failed = sum(
        1 for r in pattern_results if r.total_requests == 0 or r.error_rate_pct > 1.0
    )

    return LoadTestOutput(
        run_id=run_id,
        version=schema_version,
        target_engine=target_engine,
        test_duration_minutes=test_config.duration_minutes,
        total_patterns_tested=len(pattern_results),
        patterns_passed=len(pattern_results) - patterns_failed,
        patterns_failed=patterns_failed,
        total_cost_usd=total_cost,
        infrastructure_deployed=manifest,
        seed_summary=SeedSummary(
            total_records=seed_manifest.total_items,
            entities={},
            relationships={},
            seed_duration_seconds=seed_manifest.duration_seconds,
            key_registry_path="embedded",
        ),
        pattern_results=pattern_results,
        assumptions=[
            "Source latency includes +1.0ms network overhead estimate (collector reports in-engine execution time only).",
            "Target latency is the full application-observed round-trip (EC2 → ElastiCache → EC2).",
            "Source latency estimated from collector when percentiles unavailable.",
            "Uniform distribution for all keys.",
            f"First {test_config.warmup_seconds}s excluded (warmup).",
            "Cost from consumed RCU/WCU at on-demand pricing.",
        ],
    )


def _write_artifacts(
    store,
    base,
    output,
    manifest,
    seed_manifest,
    pattern_results,
    test_config,
    job_id,
    run_id,
    database_name,
):
    store.write_json(
        f"{base}/config.json",
        {
            "job_id": job_id,
            "run_id": run_id,
            "database_name": database_name,
            "test_config": test_config.model_dump(),
        },
    )
    store.write_json(f"{base}/infrastructure.json", manifest.model_dump())
    store.write_json(f"{base}/seed-manifest.json", seed_manifest.model_dump())
    for pr in pattern_results:
        store.write_json(f"{base}/results/{pr.query_id}.json", pr.model_dump())
    store.write_json(f"{base}/results/summary.json", output.model_dump())
