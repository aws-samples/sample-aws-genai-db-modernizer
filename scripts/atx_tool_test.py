# ruff: noqa: E402 — imports are intentionally placed after env setup and banners
"""End-to-end test of the ATX `run_collect_triage_core` shared function.

This exercises the EXACT code path the collector and triage subagents invoke —
it reads the offline collection through the ArtifactStore, ingests it, runs
triage, and writes artifacts. We then diff the triage decision against the
committed reference job discourse/15e6403d to prove the contract + determinism
hold through the shared core.

No DB, no AWS, no boto3 — pure ArtifactStore (local dir).

Run:
    uv run python scripts/atx_tool_test.py
"""

from __future__ import annotations

import json
import os
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


def normalize(obj, ref_job, new_job):
    text = json.dumps(strip_volatile(obj), sort_keys=True).replace(new_job, ref_job)
    return json.loads(text)


def fail(msg):
    print(f"  ❌  {msg}")
    sys.exit(1)


def ok(msg):
    print(f"  ✅  {msg}")


print("\n── ATX run_collect_triage_core End-to-End Test ──\n")

# Scratch artifact dir — the ArtifactStore points here via ARTIFACT_DIR.
scratch = Path(tempfile.mkdtemp(prefix="atx_tool_"))
os.environ["ARTIFACT_DIR"] = str(scratch)
os.environ.pop("S3_BUCKET", None)  # force LocalArtifactStore
os.environ["LLM_MODE"] = "none"

job_id = "atxtool1"
db_name = REFERENCE_DB

# Seed the offline collection input into the store at the default input key.
from src.storage import create_artifact_store

store = create_artifact_store()
offline_input = (
    REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "uploads" / "collector-output.json"
)
if not offline_input.exists():
    fail(f"Missing offline input fixture: {offline_input}")

input_key = f"{db_name}/{job_id}/uploads/collector-output.json"
store.write_json(input_key, json.loads(offline_input.read_text()))
ok(f"Seeded offline input at {input_key} (LocalArtifactStore)")

# ---------------------------------------------------------------------------
# Run the ACTUAL ATX tool
# ---------------------------------------------------------------------------
print("\n1. Invoke run_collect_triage_core")
from src.atx_orchestrator.core import run_collect_triage_core

try:
    result = run_collect_triage_core(job_id, db_name)
except Exception as e:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    fail(f"Core raised: {e}")

ok(f"Core returned: {result}")

# ---------------------------------------------------------------------------
# Verify artifacts written + contracts valid
# ---------------------------------------------------------------------------
print("\n2. Verify artifacts + contracts")
collector_key = f"{db_name}/{job_id}/collector/output.json"
triage_key = f"{db_name}/{job_id}/referee-triage/triage.json"

if not store.exists(collector_key):
    fail("Collector output not written")
if not store.exists(triage_key):
    fail("Triage output not written")
ok("Both artifacts written via ArtifactStore")

from src.contracts.collector_output import CollectorOutputContract
from src.contracts.triage_output import TriageOutputContract

new_collector = store.read_json(collector_key)
new_triage = store.read_json(triage_key)
try:
    CollectorOutputContract.model_validate(new_collector)
    TriageOutputContract.model_validate(new_triage)
    ok("Both outputs validate against their contracts")
except Exception as e:  # noqa: BLE001
    fail(f"Contract validation failed: {e}")

# ---------------------------------------------------------------------------
# Determinism vs reference
# ---------------------------------------------------------------------------
print("\n3. Determinism vs reference discourse/15e6403d")
ref_triage = json.loads(
    (
        REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "referee-triage" / "triage.json"
    ).read_text()
)
ref_collector = json.loads(
    (REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "collector" / "output.json").read_text()
)

new_t = normalize(new_triage, REFERENCE_JOB, job_id)
ref_t = normalize(ref_triage, REFERENCE_JOB, job_id)

for field in ("selected_agents", "skipped_agents", "signals", "deferred_agents"):
    if new_t.get(field) != ref_t.get(field):
        fail(f"Triage field '{field}' differs from reference")
    ok(f"Triage '{field}' matches reference exactly")

new_c = normalize(new_collector, REFERENCE_JOB, job_id)
ref_c = normalize(ref_collector, REFERENCE_JOB, job_id)
nt = len(new_c.get("database_schema", {}).get("tables", []))
rt = len(ref_c.get("database_schema", {}).get("tables", []))
nq = len(new_c.get("queries", {}).get("query_patterns", []))
rq = len(ref_c.get("queries", {}).get("query_patterns", []))
if nt != rt:
    fail(f"Table count differs: {nt} vs {rt}")
if nq != rq:
    fail(f"Query count differs: {nq} vs {rq}")
ok(f"Collector content matches reference ({nt} tables, {nq} queries)")

print("\n── PASSED: ATX tool reproduces reference exactly via ArtifactStore ──")
print(f"   Scratch: {scratch}\n")
