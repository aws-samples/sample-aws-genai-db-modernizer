"""
Collector agent handler — bridges entrypoint.py with engine-specific collectors.

Called by entrypoint.py with env vars:
  AGENT_TYPE=collector, JOB_ID, DATABASE_NAME, S3_BUCKET
  + collector-specific: CLUSTER_ENDPOINT, PORT, SECRET_ARN,
    AUTOMATION_INSTANCE_ID, AWS_REGION, DB_INSTANCE_IDENTIFIER, ENGINE
"""

import os
import sys

from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.storage.artifact_store import ArtifactStore


def run_collector(job_id: str, database_name: str, store: ArtifactStore) -> None:
    """Run the collector agent. Reads config from env vars, writes output via ArtifactStore."""
    engine = os.environ.get("ENGINE", "mysql")
    cluster_endpoint = os.environ.get("CLUSTER_ENDPOINT", "")
    port = int(os.environ.get("PORT", "3306"))
    secret_arn = os.environ.get("SECRET_ARN", "")
    automation_instance_id = os.environ.get("AUTOMATION_INSTANCE_ID", "")
    region = os.environ.get("AWS_REGION", "us-east-1")
    db_instance_id = os.environ.get("DB_INSTANCE_IDENTIFIER", "")
    mode = os.environ.get("COLLECTION_MODE", "live")
    bucket = os.environ.get("S3_BUCKET", "")

    if mode == "live" and (not cluster_endpoint or not secret_arn):
        print("ERROR: CLUSTER_ENDPOINT and SECRET_ARN are required for live mode", file=sys.stderr)
        sys.exit(1)

    # Build CollectorInput from env vars
    input_data = {
        "job_id": job_id,
        "engine": engine,
        "cluster_endpoint": cluster_endpoint or "offline",
        "port": port,
        "database_name": database_name,
        "mode": mode,
    }

    if mode == "live":
        input_data["live_config"] = {
            "secret_arn": secret_arn,
            "automation_instance_id": automation_instance_id,
        }
    elif mode == "ddl":
        input_data["ddl_config"] = {
            "s3_bucket": os.environ.get("DDL_S3_BUCKET", bucket),
            "s3_key": os.environ.get("DDL_S3_KEY", ""),
        }
    elif mode == "offline":
        input_data["offline_config"] = {
            "s3_bucket": os.environ.get("OFFLINE_S3_BUCKET", bucket),
            "s3_key": os.environ.get("OFFLINE_S3_KEY", ""),
        }

    if region:
        input_data["aws_config"] = {
            "region": region,
            "db_instance_identifier": db_instance_id or None,
        }

    input_contract = CollectorInput.model_validate(input_data)

    print(f"Collector starting: engine={engine} endpoint={cluster_endpoint} mode={mode}")
    result = _dispatch_collect(engine, input_contract)

    # Write output per ADR-016: {database_name}/{job_id}/collector/output.json
    key = f"{database_name}/{job_id}/collector/output.json"
    import json

    store.write_json(key, json.loads(result.model_dump_json()))

    # Materialize query journey files (source section) — ADR-019
    from src.agents.query_journey_materializer import materialize_source

    materialize_source(json.loads(result.model_dump_json()), database_name, job_id, store)
    print(f"Collector output written to {key}")
    print(
        f"Tables: {len(result.database_schema.tables)}, Queries: {len(result.queries.query_patterns)}"
    )


def _dispatch_collect(engine: str, input_contract: CollectorInput) -> CollectorOutputContract:
    """Route to the correct collector based on engine type."""
    if engine in ("mysql", "mariadb"):
        if engine == "mariadb":
            from src.agents.collector.mariadb_collector import collect
        else:
            from src.agents.collector.mysql_collector import collect
        return collect(input_contract)
    elif engine == "postgresql":
        from src.agents.collector.postgres_collector import collect as pg_collect

        return pg_collect(input_contract)
    else:
        raise ValueError(f"Unsupported engine: {engine}")
