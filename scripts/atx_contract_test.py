# ruff: noqa: E402 — imports are intentionally placed after env setup and banners
"""Contract + determinism test for the AWS Transform collect+triage path.

Proves three things, skeptic-grade:
  1. Running collect+triage through the EXISTING handlers (the same ones the
     ATX orchestrator tools call) reproduces the known-good reference artifacts.
  2. The produced artifacts still validate against their Pydantic contracts.
  3. The output is deterministic — the decision-bearing content is identical
     vs the committed reference job discourse/15e6403d.

Uses the offline collection input already committed at
  artifacts/discourse/15e6403d/uploads/collector-output.json
fed to the collector through a mocked S3 (moto) — no real DB, no real AWS.

Run:
    uv run python scripts/atx_contract_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFERENCE_DB = "discourse"
REFERENCE_JOB = "15e6403d"

VOLATILE_KEYS = {"timestamp", "collected_at", "created_at", "generated_at", "elapsed_seconds"}


def strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    return obj


def fail(msg: str):
    print(f"  ❌  {msg}")
    sys.exit(1)


def ok(msg: str):
    print(f"  ✅  {msg}")


print("\n── ATX Collect+Triage Contract & Determinism Test ──\n")

from src.storage.local_store import LocalArtifactStore

ref_collector = REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "collector" / "output.json"
ref_triage = REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "referee-triage" / "triage.json"
offline_input = (
    REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "uploads" / "collector-output.json"
)

for p in (ref_collector, ref_triage, offline_input):
    if not p.exists():
        fail(f"Missing reference fixture: {p}")
ok("Reference fixtures present (collector, triage, offline input)")

scratch = Path(tempfile.mkdtemp(prefix="atx_contract_"))
job_id = "atxtest1"
db_name = REFERENCE_DB
store = LocalArtifactStore(str(scratch))

# ---------------------------------------------------------------------------
# Step 1: Run collector via the EXISTING handler, with S3 mocked by moto
# ---------------------------------------------------------------------------
print("\n1. Run collector (existing handler, offline mode, S3 mocked)")
import os

import boto3

try:
    from moto import mock_aws
except ImportError:
    fail("moto not installed — run: uv pip install 'moto[s3,stepfunctions,logs]>=5.2.2'")

BUCKET = "atx-test-bucket"
OFFLINE_KEY = "uploads/collector-output.json"

os.environ["COLLECTION_MODE"] = "offline"
os.environ["ENGINE"] = "postgresql"
os.environ["ARTIFACT_DIR"] = str(scratch)
os.environ["OFFLINE_S3_BUCKET"] = BUCKET
os.environ["OFFLINE_S3_KEY"] = OFFLINE_KEY
os.environ["AWS_REGION"] = "us-east-1"
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

with mock_aws():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key=OFFLINE_KEY, Body=offline_input.read_bytes())

    try:
        from src.agents.collector.handler import run_collector

        run_collector(job_id, db_name, store)
        ok("Collector ran without error")
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        fail(f"Collector raised: {e}")

new_collector_key = f"{db_name}/{job_id}/collector/output.json"
if not store.exists(new_collector_key):
    fail(f"Collector did not write {new_collector_key}")
ok("Collector output written")

# ---------------------------------------------------------------------------
# Step 2: Validate collector output against its contract
# ---------------------------------------------------------------------------
print("\n2. Validate collector output against contract")
from src.contracts.collector_output import CollectorOutputContract

new_collector = store.read_json(new_collector_key)
try:
    CollectorOutputContract.model_validate(new_collector)
    ok("Collector output validates against CollectorOutputContract")
except Exception as e:  # noqa: BLE001
    fail(f"Collector output failed contract validation: {e}")

# ---------------------------------------------------------------------------
# Step 3: Run triage via the EXISTING handler
# ---------------------------------------------------------------------------
print("\n3. Run triage (existing handler)")
try:
    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, db_name, store)
    ok("Triage ran without error")
except Exception as e:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    fail(f"Triage raised: {e}")

new_triage_key = f"{db_name}/{job_id}/referee-triage/triage.json"
if not store.exists(new_triage_key):
    fail(f"Triage did not write {new_triage_key}")

from src.contracts.triage_output import TriageOutputContract

new_triage = store.read_json(new_triage_key)
try:
    TriageOutputContract.model_validate(new_triage)
    ok("Triage output validates against TriageOutputContract")
except Exception as e:  # noqa: BLE001
    fail(f"Triage output failed contract validation: {e}")

# ---------------------------------------------------------------------------
# Step 4: Determinism — diff new vs reference (modulo volatile + job_id)
# ---------------------------------------------------------------------------
print("\n4. Determinism vs reference job discourse/15e6403d")


def normalize(obj, ref_job: str, new_job: str):
    stripped = strip_volatile(obj)
    text = json.dumps(stripped, sort_keys=True)
    text = text.replace(new_job, ref_job)
    return json.loads(text)


ref_triage_data = json.loads(ref_triage.read_text())
new_norm = normalize(new_triage, REFERENCE_JOB, job_id)
ref_norm = normalize(ref_triage_data, REFERENCE_JOB, job_id)

for field in ("selected_agents", "skipped_agents", "signals", "deferred_agents"):
    new_val = new_norm.get(field)
    ref_val = ref_norm.get(field)
    if new_val != ref_val:
        print(f"\n  Field '{field}' differs.")
        if isinstance(new_val, list) and isinstance(ref_val, list):
            print(f"    new count={len(new_val)} ref count={len(ref_val)}")
        fail(f"Triage field '{field}' is NOT deterministic vs reference")
    ok(f"Triage '{field}' matches reference exactly")

ref_collector_data = json.loads(ref_collector.read_text())
new_c = normalize(new_collector, REFERENCE_JOB, job_id)
ref_c = normalize(ref_collector_data, REFERENCE_JOB, job_id)

new_tables = new_c.get("database_schema", {}).get("tables", [])
ref_tables = ref_c.get("database_schema", {}).get("tables", [])
if len(new_tables) != len(ref_tables):
    fail(f"Table count differs: new={len(new_tables)} ref={len(ref_tables)}")
ok(f"Collector table count matches reference ({len(new_tables)} tables)")

new_queries = new_c.get("queries", {}).get("query_patterns", [])
ref_queries = ref_c.get("queries", {}).get("query_patterns", [])
if len(new_queries) != len(ref_queries):
    fail(f"Query count differs: new={len(new_queries)} ref={len(ref_queries)}")
ok(f"Collector query count matches reference ({len(new_queries)} queries)")

print("\n── PASSED: contracts preserved, output deterministic ──")
print(f"   Scratch artifacts: {scratch}\n")
