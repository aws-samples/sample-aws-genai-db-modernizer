"""
Agent entrypoint — dispatches to the correct agent based on AGENT_TYPE env var.

Step Functions launches ECS tasks with environment variables:
  - AGENT_TYPE: which agent to run (collector, referee-triage, dynamodb, etc.)
  - JOB_ID: unique job identifier (KSUID)
  - DATABASE_NAME: source database name (used in artifact path)
  - S3_BUCKET: storage bucket (when set → S3ArtifactStore, else LocalArtifactStore)
  - EVENT_BUS_NAME: EventBridge bus for progress notifications
  - ENVIRONMENT: deployment environment (dev, prod)
  - PROJECT_NAME: project identifier
  - ASSIGNMENT_VERSION: (optional) assignment version for phased mode
  - SCOPE_ENGINES: (optional) comma-separated engines for scoped execution

Exit code contract:
  - Exit 0 = success → Step Functions moves to next state
  - Exit 1 = failure → Step Functions retries or catches
  - Exit 2 = needs input → Step Functions Catch routes to SetAgentAwaitingInput
  - Never run a long-lived server — this must be run-to-completion
"""

import os
import sys
import traceback

from src.agents.interaction import AgentNeedsInputError
from src.storage import create_artifact_store

AGENT_TYPE = os.environ.get("AGENT_TYPE", "")
JOB_ID = os.environ.get("JOB_ID", "")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "")
ASSIGNMENT_VERSION = os.environ.get("ASSIGNMENT_VERSION", "")
SCOPE_ENGINES = os.environ.get("SCOPE_ENGINES", "")

ANALYSIS_AGENTS = {
    "dynamodb",
    "documentdb",
    "elasticache",
    "opensearch",
    "neptune",
    "keyspaces",
    "aurora",
    "aurora_postgresql",
    "aurora_mysql",
}

# Create store at module level so it's available to all functions
store = create_artifact_store()


def main():
    if not AGENT_TYPE:
        print("ERROR: AGENT_TYPE environment variable not set", file=sys.stderr)
        sys.exit(1)
    if not JOB_ID:
        print("ERROR: JOB_ID environment variable not set", file=sys.stderr)
        sys.exit(1)
    if not DATABASE_NAME:
        print("ERROR: DATABASE_NAME environment variable not set", file=sys.stderr)
        sys.exit(1)

    # Detect legacy vs. phased mode
    if ASSIGNMENT_VERSION:
        print(
            f"Starting agent: type={AGENT_TYPE} job={JOB_ID} db={DATABASE_NAME} "
            f"assignment_version={ASSIGNMENT_VERSION} scope_engines={SCOPE_ENGINES}"
        )
    else:
        print(f"Starting agent: type={AGENT_TYPE} job={JOB_ID} db={DATABASE_NAME}")

    try:
        _dispatch_agent()
    except AgentNeedsInputError:
        print(f"Agent needs input: type={AGENT_TYPE} job={JOB_ID} — exiting with code 2")
        sys.exit(2)
    except Exception as exc:
        _write_error_artifact(exc)
        raise

    print(f"Agent completed: type={AGENT_TYPE} job={JOB_ID}")


def _dispatch_agent():
    """Route to the correct agent handler based on AGENT_TYPE."""
    if AGENT_TYPE == "collector":
        from src.agents.collector.handler import run_collector

        run_collector(JOB_ID, DATABASE_NAME, store)
    elif AGENT_TYPE == "referee-triage":
        from src.agents.referee.triage_handler import run_triage

        run_triage(JOB_ID, DATABASE_NAME, store)
    elif AGENT_TYPE in ANALYSIS_AGENTS:
        from src.agents.analysis.handler import run_analysis

        run_analysis(JOB_ID, DATABASE_NAME, AGENT_TYPE, store)
    elif AGENT_TYPE == "referee-synthesis":
        from src.agents.referee.synthesis_handler import run_synthesis

        assignment_ver = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 0
        run_synthesis(JOB_ID, DATABASE_NAME, store, assignment_version=assignment_ver)
    elif AGENT_TYPE == "assignment-resolver":
        from src.agents.referee.assignment_handler import run_assignment_resolver

        run_assignment_resolver(JOB_ID, DATABASE_NAME, store)
    elif AGENT_TYPE == "reality-check":
        from src.agents.referee.reality_check_handler import run_reality_check_handler

        assignment_ver = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 1
        run_reality_check_handler(JOB_ID, DATABASE_NAME, store, assignment_version=assignment_ver)
    elif AGENT_TYPE == "schema-design":
        target_type = os.environ.get("TARGET_TYPE", "")
        if not target_type:
            print("ERROR: TARGET_TYPE not set for schema-design agent", file=sys.stderr)
            sys.exit(1)
        assignment_ver = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 0
        from src.agents.schema_design.handler import run_schema_design

        run_schema_design(
            JOB_ID, DATABASE_NAME, target_type, store, assignment_version=assignment_ver
        )
    elif AGENT_TYPE == "schema-split":
        target_type = os.environ.get("TARGET_TYPE", "")
        if not target_type:
            print("ERROR: TARGET_TYPE not set for schema-split agent", file=sys.stderr)
            sys.exit(1)
        assignment_ver = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 0
        from src.agents.schema_design.handler import run_schema_split

        run_schema_split(
            JOB_ID, DATABASE_NAME, target_type, store, assignment_version=assignment_ver
        )
    elif AGENT_TYPE == "schema-merge":
        target_type = os.environ.get("TARGET_TYPE", "")
        if not target_type:
            print("ERROR: TARGET_TYPE not set for schema-merge agent", file=sys.stderr)
            sys.exit(1)
        assignment_ver = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 0
        from src.agents.schema_design.handler import run_schema_merge

        run_schema_merge(
            JOB_ID, DATABASE_NAME, target_type, store, assignment_version=assignment_ver
        )
    elif AGENT_TYPE == "load-test":
        target_type = os.environ.get("TARGET_TYPE", "")
        if not target_type:
            print("ERROR: TARGET_TYPE not set for load-test agent", file=sys.stderr)
            sys.exit(1)
        schema_version = int(ASSIGNMENT_VERSION) if ASSIGNMENT_VERSION else 1
        from src.agents.load_test.handler import run_load_test

        run_load_test(JOB_ID, DATABASE_NAME, target_type, store, schema_version=schema_version)
    else:
        print(f"ERROR: Unknown AGENT_TYPE: {AGENT_TYPE}", file=sys.stderr)
        sys.exit(1)


def _write_error_artifact(exc: Exception):
    """Write structured error JSON via ArtifactStore for debugging and API consumption."""
    from datetime import UTC, datetime

    if not JOB_ID or not DATABASE_NAME:
        print("Cannot write error artifact: missing JOB_ID/DATABASE_NAME", file=sys.stderr)
        return

    error_payload = {
        "agent_type": AGENT_TYPE,
        "job_id": JOB_ID,
        "database_name": DATABASE_NAME,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exc(),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    key = f"{DATABASE_NAME}/{JOB_ID}/{AGENT_TYPE}/error.json"
    try:
        store.write_json(key, error_payload)
        print(f"Error artifact written to {key}")
    except Exception as store_err:
        print(f"Failed to write error artifact: {store_err}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
