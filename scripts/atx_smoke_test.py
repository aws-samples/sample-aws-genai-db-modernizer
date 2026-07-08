# ruff: noqa: E402, F401 — imports placed after setup; availability-check imports are intentional
"""Smoke test — verifies the ATX orchestrator wires up correctly.

Checks:
  1. All tools import without error
  2. Tool metadata (name, docstring) is present
  3. DBModernizationOrchestrator can be instantiated (SDK imports work)
  4. All tools are registered on the orchestrator

Run from repo root:
    uv run python scripts/atx_smoke_test.py
"""

from __future__ import annotations

import sys


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "✅" if condition else "❌"
    msg = f"  {mark}  {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    if not condition:
        sys.exit(1)


print("\n── AWS Transform Orchestrator Smoke Test ──\n")

# 1. Tool imports
print("1. Tool imports")
try:
    from src.atx_orchestrator.tools import (
        get_job_status,
        get_synthesis_report,
        run_assignment,
        run_collect,
        run_collect_and_triage,
        run_collect_via_a2a,
        run_full_assessment,
        run_reality_check,
        run_schema_design,
        run_synthesis,
        run_triage,
        run_triage_via_a2a,
    )

    tools = [
        run_collect,
        run_triage,
        run_collect_and_triage,
        run_collect_via_a2a,
        run_triage_via_a2a,
        run_assignment,
        run_reality_check,
        run_schema_design,
        run_synthesis,
        run_full_assessment,
        get_job_status,
        get_synthesis_report,
    ]
    check("All tools imported", True)
except ImportError as e:
    check("All tools imported", False, str(e))

# 2. Tool metadata
print("\n2. Tool metadata")
for t in tools:
    name = getattr(t, "__name__", None) or getattr(t, "name", None)
    doc = getattr(t, "__doc__", None)
    check(f"Tool '{name}' has docstring", bool(doc))

# 3. Orchestrator import (SDK must be installed)
print("\n3. Orchestrator import")
try:
    from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS, DBModernizationOrchestrator

    check("DBModernizationOrchestrator imported", True)
    check(
        "PIPELINE_TOOLS count",
        len(PIPELINE_TOOLS) == 13,
        f"expected 13, got {len(PIPELINE_TOOLS)}",
    )
except ImportError as e:
    check("DBModernizationOrchestrator imported", False, str(e))
    print("\n  ℹ️  Install the SDK first:")
    print("     uv pip install 'agent-builder-sdk-aws-transform>=1.0.0'")
    sys.exit(1)

# 4. LocalOrchestrator still importable (existing pipeline untouched)
print("\n4. Existing pipeline intact")
try:
    from src.orchestrator.local_orchestrator import LocalOrchestrator
    from src.storage import create_artifact_store

    check("LocalOrchestrator importable", True)
    check("create_artifact_store importable", True)
except ImportError as e:
    check("Existing pipeline intact", False, str(e))

print("\n── All checks passed ──\n")
