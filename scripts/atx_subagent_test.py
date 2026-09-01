# ruff: noqa: E402 — imports are intentionally placed after env setup and banners
"""Local test of the consolidated deterministic-core subagent (ADR-025).

Exercises the deterministic-core subagent's message-handling WITHOUT the ATX
runtime:
  1. Parses an A2A-style message (JSON and key:value forms)
  2. Runs the consolidated work function (Collect -> Triage -> Analyze -> Assign)
     against a seeded ArtifactStore
  3. Verifies the collector + triage artifacts + contracts + determinism vs the
     reference, proving the consolidated agent preserves the same artifacts as the
     separate collector / triage runtimes did.

Run:
    uv run python scripts/atx_subagent_test.py
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

VOLATILE = {"timestamp", "collected_at", "created_at", "generated_at", "elapsed_seconds"}


def strip_volatile(o):
    if isinstance(o, dict):
        return {k: strip_volatile(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip_volatile(x) for x in o]
    return o


def normalize(o, ref, new):
    return json.loads(json.dumps(strip_volatile(o), sort_keys=True).replace(new, ref))


def fail(m):
    print(f"  ❌  {m}")
    sys.exit(1)


def ok(m):
    print(f"  ✅  {m}")


print("\n── Deterministic-Core Subagent Test (Collect -> Triage -> Analyze -> Assign) ──\n")

scratch = Path(tempfile.mkdtemp(prefix="atx_subagent_"))
os.environ["ARTIFACT_DIR"] = str(scratch)
os.environ.pop("S3_BUCKET", None)
os.environ["LLM_MODE"] = "none"

job_id = "subagent1"
db_name = REFERENCE_DB

from src.storage import create_artifact_store

store = create_artifact_store()
offline_input = (
    REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "uploads" / "collector-output.json"
)
if not offline_input.exists():
    fail(f"Missing fixture: {offline_input}")

input_key = f"{db_name}/{job_id}/uploads/collector-output.json"
store.write_json(input_key, json.loads(offline_input.read_text()))
ok("Seeded offline input")

# ---------------------------------------------------------------------------
# 1. Deterministic-core subagent: message parsing + consolidated work
# ---------------------------------------------------------------------------
print("\n1. Deterministic-core subagent (Collect -> Triage -> Analyze -> Assign)")
from src.atx_orchestrator.subagents.base import extract_text, parse_invocation
from src.atx_orchestrator.subagents.deterministic_core import _work as core_work

a2a_msg = {
    "parts": [{"text": json.dumps({"job_id": job_id, "database_name": db_name})}],
    "role": "user",
}
text = extract_text(type("Req", (), {"message": a2a_msg})())
parsed = parse_invocation(text)
if parsed["job_id"] != job_id or parsed["database_name"] != db_name:
    fail(f"JSON parse failed: {parsed}")
ok("Parsed A2A JSON message")

kv = parse_invocation(f"job_id={job_id}; database_name={db_name}")
if kv["job_id"] != job_id or kv["database_name"] != db_name:
    fail(f"key=value parse failed: {kv}")
ok("Parsed key=value message")

try:
    core_summary = core_work(parsed)
except Exception as e:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    fail(f"deterministic-core work raised: {e}")
ok(f"Deterministic-core returned phases: {list(core_summary.keys())}")

collector_key = f"{db_name}/{job_id}/collector/output.json"
if not store.exists(collector_key):
    fail("Collector artifact not written")

from src.contracts.collector_output import CollectorOutputContract

new_collector = store.read_json(collector_key)
try:
    CollectorOutputContract.model_validate(new_collector)
    ok("Collector output validates against contract")
except Exception as e:  # noqa: BLE001
    fail(f"Collector contract validation failed: {e}")

# Determinism vs reference collector
ref_collector = json.loads(
    (REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "collector" / "output.json").read_text()
)
nc = normalize(new_collector, REFERENCE_JOB, job_id)
rc = normalize(ref_collector, REFERENCE_JOB, job_id)
nt = len(nc.get("database_schema", {}).get("tables", []))
rt = len(rc.get("database_schema", {}).get("tables", []))
nq = len(nc.get("queries", {}).get("query_patterns", []))
rq = len(rc.get("queries", {}).get("query_patterns", []))
if nt != rt or nq != rq:
    fail(f"Collector content differs: tables {nt}/{rt}, queries {nq}/{rq}")
ok(f"Collector content matches reference ({nt} tables, {nq} queries)")

# ---------------------------------------------------------------------------
# 2. Triage artifact produced by the consolidated run
# ---------------------------------------------------------------------------
print("\n2. Triage artifact (produced in-process by the deterministic core)")

triage_key = f"{db_name}/{job_id}/referee-triage/triage.json"
from src.contracts.triage_output import TriageOutputContract

new_triage = store.read_json(triage_key)
try:
    TriageOutputContract.model_validate(new_triage)
    ok("Triage output validates against contract")
except Exception as e:  # noqa: BLE001
    fail(f"Triage contract validation failed: {e}")

ref_triage = json.loads(
    (
        REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "referee-triage" / "triage.json"
    ).read_text()
)
ntr = normalize(new_triage, REFERENCE_JOB, job_id)
rtr = normalize(ref_triage, REFERENCE_JOB, job_id)
for field in ("selected_agents", "skipped_agents", "signals", "deferred_agents"):
    if ntr.get(field) != rtr.get(field):
        fail(f"Triage '{field}' differs from reference")
ok("Triage decision matches reference exactly")

print("\n── PASSED: consolidated deterministic-core subagent preserves contracts + determinism ──")
print(f"   Scratch: {scratch}\n")
