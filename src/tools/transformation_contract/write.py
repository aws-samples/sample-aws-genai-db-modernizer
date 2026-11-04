# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Write step 1's transformation contract for a finished local job (#1764)."""

from __future__ import annotations

from typing import Any

from src.tools.data_movement.plan import load_inputs
from src.tools.transformation_contract.hashing import canonical_json, contract_sha256
from src.tools.transformation_contract.shape_check import shape_check
from src.tools.transformation_contract.step1 import build_step1
from src.tools.transformation_contract.validate import validate_step1


def write_step1(store: Any, db: str, job_id: str) -> dict[str, Any]:
    """Build, validate and shape-check step 1, then write ``step-1.json`` (the canonical
    bytes, so its SHA-256 is ``step-1.sha256``), ``step-1.sha256`` and ``findings.json``.

    A report with no migration waves writes nothing and returns ``status: skipped``."""
    inputs = load_inputs(store, db, job_id)
    if not inputs.report.get("migration_waves"):
        return {"status": "skipped", "reason": "no migration waves in the report", "paths": []}
    contract = shape_check(validate_step1(build_step1(inputs), inputs), inputs)
    digest = contract_sha256(contract)
    base = f"{db}/{job_id}/transformation-contract/v{contract.assignment_version}"
    findings = [
        {**f.model_dump(mode="json"), "table_id": r.source_table, "rule": r.target_table}
        for r in contract.rules
        for f in r.findings
    ]
    contract_path = f"{base}/step-1.json"
    digest_path = f"{base}/step-1.sha256"
    findings_path = f"{base}/findings.json"
    # The pin goes first and comes back last: a crash between the two never leaves a hash
    # that describes the previous file.
    store.delete(digest_path)
    store.write_bytes(contract_path, canonical_json(contract))
    store.write_bytes(digest_path, (digest + "\n").encode("ascii"))
    store.write_json(findings_path, {"step": 1, "findings": findings})
    return {
        "status": "complete",
        "paths": [contract_path, digest_path, findings_path],
        "contract_status": contract.status,
        "blocked_rule_count": contract.blocked_rule_count,
        "rule_count": len(contract.rules),
        "sha256": digest,
    }
