"""
Collector agent handler — bridges entrypoint.py with engine-specific collectors.

Called by entrypoint.py with env vars:
  AGENT_TYPE=collector, JOB_ID, DATABASE_NAME, S3_BUCKET
  + collector-specific: CLUSTER_ENDPOINT, PORT, SECRET_ARN,
    AUTOMATION_INSTANCE_ID, AWS_REGION, DB_INSTANCE_IDENTIFIER, ENGINE
"""

import os
import sys

from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import CollectorOutputContract
from src.storage.artifact_store import ArtifactStore


def run_collector(job_id: str, database_name: str, store: ArtifactStore) -> None:
    """Run the collector agent. Reads config from env vars, writes output via ArtifactStore."""
    cluster_endpoint = os.environ.get("CLUSTER_ENDPOINT", "")
    secret_arn = os.environ.get("SECRET_ARN", "")
    automation_instance_id = os.environ.get("AUTOMATION_INSTANCE_ID", "")
    region = os.environ.get("AWS_REGION", "us-east-1")
    db_instance_id = os.environ.get("DB_INSTANCE_IDENTIFIER", "")
    mode = os.environ.get("COLLECTION_MODE", "live")
    bucket = os.environ.get("S3_BUCKET", "")

    if mode == "live" and (not cluster_endpoint or not secret_arn):
        print("ERROR: CLUSTER_ENDPOINT and SECRET_ARN are required for live mode", file=sys.stderr)
        sys.exit(1)

    raw = _offline_collection(bucket, region) if mode == "offline" else None
    engine, port = _engine_and_port(mode, raw)

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

    # #963: the other AWS engines the application already runs, declared by the operator.
    declared = os.environ.get("ENGINES_IN_USE", "")
    if declared.strip():
        from src.shared.engines_in_use import parse_engines_in_use_option

        try:
            input_data["engines_in_use"] = parse_engines_in_use_option(declared)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    if region:
        input_data["aws_config"] = {
            "region": region,
            "db_instance_identifier": db_instance_id or None,
        }

    input_contract = CollectorInput.model_validate(input_data)

    print(f"Collector starting: engine={engine} endpoint={cluster_endpoint} mode={mode}")
    result = _dispatch_collect(engine, input_contract, raw)

    # Write output per ADR-016: {database_name}/{job_id}/collector/output.json
    key = f"{database_name}/{job_id}/collector/output.json"
    import json

    output = json.loads(result.model_dump_json())
    if input_contract.engines_in_use:
        from src.shared.engines_in_use import merge_engines_in_use

        metadata = output.setdefault("metadata", {})
        metadata["engines_in_use"] = merge_engines_in_use(
            metadata.get("engines_in_use"), input_contract.engines_in_use
        )
    store.write_json(key, output)

    print(f"Collector output written to {key}")
    print(
        f"Tables: {len(result.database_schema.tables)}, Queries: {len(result.queries.query_patterns)}"
    )


def _offline_collection(bucket: str, region: str) -> dict:
    """The offline collection file, downloaded once: its metadata names the source
    engine, and the collector builds the output from the same copy (#1177)."""
    from src.tools.database.offline_parser import fetch_offline_json

    raw = fetch_offline_json(
        bucket=os.environ.get("OFFLINE_S3_BUCKET", bucket),
        key=os.environ.get("OFFLINE_S3_KEY", ""),
        region=region,
    )
    return raw if isinstance(raw, dict) else {}


def _engine_and_port(mode: str, raw: dict | None) -> tuple[str, int]:
    """The source engine and port, from the collection itself or ``ENGINE``/``PORT``.

    Offline, the collection's own metadata wins when it names an engine
    (``offline_ingest.resolve_offline_engine``, the detector every ingest path uses,
    #1177): an upload form's ``ENGINE`` is a guess about a file the collector can read,
    so a disagreement is reported on stderr and the file's engine used. A collection
    that names none takes ``ENGINE``, else the detector's default. A live or DDL
    collection has only ``ENGINE`` to go by, so the handler refuses to guess (it used
    to assume MySQL). ``ENGINE=postgres`` reads as ``postgresql``. ``PORT`` defaults to
    the engine's own default port, not MySQL's.
    """
    from src.agents.collector.offline_ingest import resolve_offline_engine
    from src.tools.database.offline_parser import default_port, normalize_engine

    declared = normalize_engine(os.environ.get("ENGINE", ""))
    detected_port: int | None = None
    if mode == "offline":
        metadata = (raw or {}).get("metadata")
        engine, detected_port, note = resolve_offline_engine(
            metadata if isinstance(metadata, dict) else None, declared
        )
        if note:
            print(f"WARNING: {note}", file=sys.stderr)
    elif not declared:
        print(f"ERROR: ENGINE is required for {mode} mode", file=sys.stderr)
        sys.exit(1)
    else:
        engine = declared
        if engine not in _LIVE_ENGINES:
            print(
                f"ERROR: {engine} has no {mode} collector; collect it with the offline "
                "collection script and upload the file (COLLECTION_MODE=offline)",
                file=sys.stderr,
            )
            sys.exit(1)
    port_env = os.environ.get("PORT", "").strip()
    if port_env and port_env != "0":
        return engine, int(port_env)
    port = detected_port or default_port(engine)
    if port is None:
        print(f"ERROR: PORT is required for engine {engine!r}", file=sys.stderr)
        sys.exit(1)
    return engine, port


# Engines with a live and DDL collector (_dispatch_collect). Every engine the
# detector knows, DB2 included, has the shared offline one.
_LIVE_ENGINES = frozenset({"mysql", "mariadb", "postgresql", "sqlserver", "oracle"})


def _dispatch_collect(
    engine: str, input_contract: CollectorInput, raw: dict | None = None
) -> CollectorOutputContract:
    """Route to the correct collector based on engine type.

    Offline, every engine shares one collector (``mysql_collector.collect_offline``,
    the shared offline builder), fed the copy of the file the handler already read.
    """
    if getattr(input_contract, "mode", None) == CollectionMode.offline:
        from src.agents.collector.mysql_collector import collect_offline

        return collect_offline(input_contract, raw)
    if engine in ("mysql", "mariadb"):
        if engine == "mariadb":
            from src.agents.collector.mariadb_collector import collect
        else:
            from src.agents.collector.mysql_collector import collect
        return collect(input_contract)
    elif engine == "postgresql":
        from src.agents.collector.postgres_collector import collect as pg_collect

        return pg_collect(input_contract)
    elif engine == "sqlserver":
        from src.agents.collector.sqlserver_collector import collect as ss_collect

        return ss_collect(input_contract)
    elif engine == "oracle":
        from src.agents.collector.oracle_collector import collect as ora_collect

        return ora_collect(input_contract)
    else:
        raise ValueError(f"Unsupported engine: {engine}")
