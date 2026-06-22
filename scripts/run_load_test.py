#!/usr/bin/env python3
"""Run the load test stage locally against a completed job's artifacts.

Usage:
  uv run python scripts/run_load_test.py <database_name> <job_id> [options]

Example:
  uv run python scripts/run_load_test.py wordpress b70de5dc --engine dynamodb --schema-version 2
  uv run python scripts/run_load_test.py wordpress b70de5dc --duration 15 --dry-run-only

Prerequisites:
  - k6 installed (brew install k6)
  - AWS credentials configured (for DynamoDB provisioning)
  - Completed schema design artifacts in ./artifacts/<db>/<job>/schema-<engine>/v<N>/
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RUNTIME_MODE", "local")
os.environ.setdefault("ARTIFACT_DIR", "./artifacts")


def main():
    parser = argparse.ArgumentParser(description="Run load test locally against a completed job")
    parser.add_argument("database_name", help="Database name (artifact path prefix)")
    parser.add_argument("job_id", help="Job ID (artifact path segment)")
    parser.add_argument(
        "--engine",
        default="dynamodb",
        help="Target engine (default: dynamodb)",
    )
    parser.add_argument(
        "--schema-version",
        type=int,
        default=None,
        help="Schema version to test (default: latest)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=15,
        help="Test duration in minutes (default: 15)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=30,
        help="Warmup seconds (default: 30)",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Only run dry-run (1 iteration), skip full test",
    )
    parser.add_argument(
        "--skip-provision",
        action="store_true",
        help="Skip provisioning (table already exists)",
    )
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="Delete infrastructure after test (default: keep for reuse)",
    )
    args = parser.parse_args()

    from datetime import datetime

    from src.contracts.load_test_models import TestConfig
    from src.storage import create_artifact_store

    store = create_artifact_store()

    # Auto-detect schema version if not specified
    schema_version = args.schema_version
    if schema_version is None:
        schema_version = _find_latest_schema_version(
            store, args.database_name, args.job_id, args.engine
        )
        if schema_version is None:
            print(
                f"ERROR: No schema design found for {args.database_name}/{args.job_id}/schema-{args.engine}/"
            )
            sys.exit(1)

    print(f"\n{'='*60}")
    print("  Database Modernizer Assessment — Load Test Runner")
    print(f"{'='*60}")
    print(f"  Database:       {args.database_name}")
    print(f"  Job ID:         {args.job_id}")
    print(f"  Engine:         {args.engine}")
    print(f"  Schema version: v{schema_version}")
    print(f"  Duration:       {args.duration} min")
    print(f"  Warmup:         {args.warmup}s")
    print(f"  Region:         {args.region}")
    print(f"  Dry-run only:   {args.dry_run_only}")
    print(f"  Started:        {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}\n")

    test_config = TestConfig(
        duration_minutes=args.duration,
        warmup_seconds=args.warmup,
    )

    if args.dry_run_only:
        _run_dry_run_only(
            args.database_name,
            args.job_id,
            args.engine,
            store,
            schema_version,
            test_config,
            args.region,
        )
    else:
        from src.agents.load_test.handler import run_load_test

        try:
            output = run_load_test(
                job_id=args.job_id,
                database_name=args.database_name,
                target_engine=args.engine,
                store=store,
                schema_version=schema_version,
                test_config=test_config,
                region=args.region,
            )

            print(f"\n{'='*60}")
            print("  RESULTS")
            print(f"{'='*60}")
            print(f"  Run ID:           {output.run_id}")
            print(f"  Patterns tested:  {output.total_patterns_tested}")
            print(f"  Patterns passed:  {output.patterns_passed}")
            print(f"  Patterns failed:  {output.patterns_failed}")
            print(f"  Total cost (USD): ${output.total_cost_usd:.6f}")
            print("\n  Per-pattern results:")
            for pr in output.pattern_results:
                status = "✓" if pr.error_rate_pct <= 1.0 else "✗"
                if pr.improvement_factor >= 1.0:
                    improvement_str = f"{pr.improvement_factor:.1f}x faster"
                else:
                    slowdown = (1.0 / pr.improvement_factor) if pr.improvement_factor > 0 else 0
                    improvement_str = f"{slowdown:.0f}x slower"
                print(
                    f"    {status} {pr.query_id[:16]} | "
                    f"src={pr.source_latency_ms.p50:.2f}ms → tgt={pr.target_latency_ms.p50:.1f}ms | "
                    f"{improvement_str} | "
                    f"cost=${pr.cost_per_operation_usd:.8f}"
                )
            print(
                f"\n  Artifacts: ./artifacts/{args.database_name}/{args.job_id}/load-test/v{schema_version}/"
            )

            if args.teardown and args.engine == "opensearch":
                print("\n  Tearing down OpenSearch domain...")
                from src.agents.load_test.opensearch.provisioner import OpenSearchProvisioner

                provisioner = OpenSearchProvisioner(region=args.region)
                from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

                manifest = InfrastructureManifest(
                    resources=[
                        DeployedResource(
                            resource_type="AWS::OpenSearchService::Domain",
                            resource_arn="",
                            configuration={
                                "domain_name": f"loadtest-mod-{args.job_id[:12]}-os"[:28],
                            },
                        )
                    ],
                    tags={},
                )
                provisioner.teardown_force(manifest)
                print("  ✓ Domain deleted")

        except Exception as e:
            print(f"\nERROR: Load test failed: {e}")
            import traceback

            traceback.print_exc()
            sys.exit(1)


def _run_dry_run_only(database_name, job_id, engine, store, schema_version, test_config, region):
    """Run just the script generation + dry-run validation (no real load test)."""
    from src.agents.load_test.dynamodb import DynamoDBScriptGenerator, K6Runner
    from src.agents.load_test.handler import _get_testable_patterns, _resolve_aws_credentials
    from src.agents.load_test.models import SeedManifest

    schema_key = f"{database_name}/{job_id}/schema-{engine}/v{schema_version}/schema_output.json"

    print("[1/4] Reading artifacts...")
    schema_output = store.read_json(schema_key)

    access_patterns = _get_testable_patterns(schema_output)

    print(f"[2/4] Generating k6 scripts for {len(access_patterns)} patterns...")
    generator = DynamoDBScriptGenerator(region=region)

    # Build a minimal stub SeedManifest for dry-run (no real data needed)
    stub_resources = {}
    for td in schema_output.get("table_definitions", []):
        pk = td["partition_key"]
        sk = td.get("sort_key")
        stub_resources[td["table_name"]] = {
            "table_name": td["table_name"],
            "pk_attr": pk["attribute_name"],
            "pk_type": pk["attribute_type"],
            "pk_count": 100,
            "pk_pad_width": 4,
            "sk_attr": sk["attribute_name"] if sk else None,
            "sk_type": sk["attribute_type"] if sk else None,
            "sk_count": 10 if sk else None,
            "sk_pad_width": 4 if sk else None,
            "items_seeded": 1000,
        }
    seed_manifest = SeedManifest(
        resources=stub_resources,
        total_items=sum(r["items_seeded"] for r in stub_resources.values()),
        duration_seconds=0.0,
    )

    scripts_dir = generator.generate_all(access_patterns, schema_output, seed_manifest, test_config)
    print(f"[3/4] Scripts written to: {scripts_dir}")
    print("[4/4] Running k6 dry-run (k6 inspect)...")

    runner = K6Runner()
    env_vars = _resolve_aws_credentials()
    env_vars["AWS_REGION"] = region

    success = runner.dry_run(scripts_dir=scripts_dir, env_vars=env_vars)

    if success:
        print("\n  ✓ Dry-run PASSED — all scripts are valid k6 JavaScript")
        print(f"  Scripts ready at: {scripts_dir}/scenarios/")
    else:
        print("\n  ✗ Dry-run FAILED — check k6 output above")
        sys.exit(1)


def _find_latest_schema_version(store, db_name, job_id, engine) -> int | None:
    """Find the latest schema version by checking v1, v2, ... until gap of 5."""
    latest = None
    for version in range(1, 20):
        key = f"{db_name}/{job_id}/schema-{engine}/v{version}/schema_output.json"
        if store.exists(key):
            latest = version
    return latest


if __name__ == "__main__":
    main()
