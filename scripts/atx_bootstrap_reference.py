# ruff: noqa: E402 — imports are intentionally placed after env setup and banners
"""One-shot fixture bootstrap for the AWS Transform integration test scripts.

Regenerates the golden reference artifacts at::

    artifacts/discourse/15e6403d/collector/output.json
    artifacts/discourse/15e6403d/referee-triage/triage.json

by running the deterministic collect + triage phases against the committed
offline input at::

    artifacts/discourse/15e6403d/uploads/collector-output.json

which is extracted from ``docs/examples/discourse/discourse.zip``.

WHY THIS SCRIPT EXISTS
======================
The four ``scripts/atx_*_test.py`` scripts diff their computed collect + triage
output against these two "reference" files to prove:

  * Contract preservation — output validates against the Pydantic contracts.
  * Determinism — decision-bearing content is identical run-to-run.

Because the reference files are not committed to the repo (``artifacts/`` is
gitignored), a fresh clone needs to *create* them once before the tests can
run. That first creation is inherently tautological — we compute the pipeline
output and immediately declare it the reference. This is acceptable because:

  1. The pipeline is fully deterministic (LLM_MODE=none, pattern matching only).
     Handoff §3 covers the audit.
  2. Every run AFTER the bootstrap catches regressions — any drift between the
     new pipeline output and the checked-in reference will fail the tests.
  3. Re-run this script intentionally when the pipeline *is meant to change*
     (contract bump, algorithm update, etc.). Anything else running this script
     is a bug — the tests should have failed instead.

WHEN TO RE-RUN
==============
  * After a contract version bump that affects collector/output or triage/triage.
  * After a deliberate change to collector or triage decision logic.
  * NEVER as a way to make failing tests pass.

Run::

    uv run python scripts/atx_bootstrap_reference.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFERENCE_DB = "discourse"
REFERENCE_JOB = "15e6403d"

# Route the artifact store at the repo-local artifacts/ dir; do NOT hit S3.
os.environ["ARTIFACT_DIR"] = str(REPO / "artifacts")
os.environ.pop("S3_BUCKET", None)
os.environ.setdefault("LLM_MODE", "none")

sys.path.insert(0, str(REPO))


def fail(msg: str) -> None:
    print(f"  ❌  {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def header(msg: str) -> None:
    print(f"\n{msg}")


print("\n── ATX Reference Fixture Bootstrap ──\n")

offline_input = (
    REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "uploads" / "collector-output.json"
)
ref_collector = REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "collector" / "output.json"
ref_triage = REPO / "artifacts" / REFERENCE_DB / REFERENCE_JOB / "referee-triage" / "triage.json"

header("1. Verify offline input exists (from docs/examples/discourse/discourse.zip)")
if not offline_input.exists():
    fail(
        f"Offline input missing: {offline_input}\n"
        "     Extract it first with:\n"
        "       mkdir -p artifacts/discourse/15e6403d/uploads\n"
        "       unzip -p docs/examples/discourse/discourse.zip discourse-collection.json"
        " > artifacts/discourse/15e6403d/uploads/collector-output.json"
    )
ok(f"Offline input present ({offline_input.stat().st_size:,} bytes)")

with open(offline_input) as f:
    raw = json.load(f)
n_raw_tables = len(raw.get("tables", []))
n_raw_queries = len(raw.get("queries", []))
ok(f"Offline input parses cleanly ({n_raw_tables} tables, {n_raw_queries} queries)")

header("2. Run deterministic collect + triage (LLM_MODE=none)")

from src.atx_orchestrator.core import run_collect_triage_core  # noqa: E402

try:
    result = run_collect_triage_core(job_id=REFERENCE_JOB, database_name=REFERENCE_DB)
except Exception as e:
    fail(f"Pipeline crashed: {type(e).__name__}: {e}")

ok(
    f"Pipeline succeeded — tables={result['tables']}, queries={result['queries']}, "
    f"selected_engines={result['selected_engines']}, signals={result['signal_count']}"
)

header("3. Verify reference files are on disk")
for label, path in (("collector output", ref_collector), ("triage decision", ref_triage)):
    if not path.exists():
        fail(f"{label} not written at expected path: {path}")
    ok(f"{label}: {path.relative_to(REPO)} ({path.stat().st_size:,} bytes)")

header("4. Validate contracts")

from src.contracts.collector_output import CollectorOutputContract  # noqa: E402
from src.contracts.triage_output import TriageOutputContract  # noqa: E402

with open(ref_collector) as f:
    collector_json = json.load(f)
try:
    CollectorOutputContract.model_validate(collector_json)
except Exception as e:
    fail(f"Collector reference fails CollectorOutputContract: {type(e).__name__}: {e}")
ok("Collector reference validates against CollectorOutputContract")

with open(ref_triage) as f:
    triage_json = json.load(f)
try:
    TriageOutputContract.model_validate(triage_json)
except Exception as e:
    fail(f"Triage reference fails TriageOutputContract: {type(e).__name__}: {e}")
ok("Triage reference validates against TriageOutputContract")

print("\n── Bootstrap complete ──")
print("\nNext step:")
print("  uv run python scripts/atx_smoke_test.py")
print("  uv run python scripts/atx_contract_test.py")
print("  uv run python scripts/atx_tool_test.py")
print("  uv run python scripts/atx_subagent_test.py")
