# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 writer: three files, canonical bytes, a pin that matches the file (#1764)."""

from __future__ import annotations

import hashlib
import json
import re

from src.contracts.transformation_contract import StepContract
from src.shared.contract_versioning import current_version
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.plan import _contract_refs, build_step_plans, load_inputs
from src.tools.transformation_contract.hashing import canonical_json
from src.tools.transformation_contract.write import write_step1

from .conftest import col as _col
from .conftest import step1_inputs as _inputs
from .conftest import table as _table

BASE = "wp/j1/transformation-contract/v2"


def _store_with_job(tmp_path, waves=True, tables=None):
    store = LocalArtifactStore(str(tmp_path))
    inp = _inputs(tables=tables)
    report = dict(inp.report)
    if not waves:
        report["migration_waves"] = []
    store.write_json("wp/j1/synthesis/v2/report.json", report)
    store.write_json("wp/j1/assignment/v2/assignment.json", {"query_assignments": []})
    store.write_json("wp/j1/collector/output.json", inp.collector)
    return store


def test_two_runs_write_the_same_bytes(tmp_path):
    store = _store_with_job(tmp_path)
    a = write_step1(store, "wp", "j1")
    first = store.read_bytes(f"{BASE}/step-1.json")
    b = write_step1(store, "wp", "j1")
    second = store.read_bytes(f"{BASE}/step-1.json")
    assert first == second and a["sha256"] == b["sha256"]
    assert store.read_bytes(f"{BASE}/step-1.sha256").decode().strip() == a["sha256"]


def test_the_file_is_the_canonical_bytes_and_its_hash_is_the_pin(tmp_path):
    store = _store_with_job(tmp_path)
    out = write_step1(store, "wp", "j1")
    raw = store.read_bytes(f"{BASE}/step-1.json")
    contract = StepContract.model_validate(json.loads(raw))
    assert raw == canonical_json(contract)
    assert hashlib.sha256(raw).hexdigest() == out["sha256"]
    digest = store.read_bytes(f"{BASE}/step-1.sha256").decode()
    assert digest == out["sha256"] + "\n"
    assert re.fullmatch(r"[0-9a-f]{64}", digest.strip())
    assert out["paths"] == [f"{BASE}/step-1.json", f"{BASE}/step-1.sha256", f"{BASE}/findings.json"]
    assert out["status"] == "complete" and out["contract_status"] == contract.status


def test_findings_are_flattened_with_table_and_rule(tmp_path):
    tables = [_table("wp", "nokey", [_col("a", "int", 1)])]
    store = _store_with_job(tmp_path, tables=tables)
    out = write_step1(store, "wp", "j1")
    data = store.read_json(f"{BASE}/findings.json")
    assert data["step"] == 1 and data["findings"]
    for f in data["findings"]:
        assert f["table_id"] == "wp.nokey" and f["rule"] == "nokey" and f["code"]
    contract = StepContract.model_validate(json.loads(store.read_bytes(f"{BASE}/step-1.json")))
    assert len(data["findings"]) == sum(len(r.findings) for r in contract.rules)
    assert out["blocked_rule_count"] == contract.blocked_rule_count


def test_a_report_without_waves_is_skipped(tmp_path):
    store = _store_with_job(tmp_path, waves=False)
    assert write_step1(store, "wp", "j1") == {
        "status": "skipped",
        "reason": "no migration waves in the report",
        "paths": [],
    }
    assert not store.exists(f"{BASE}/step-1.json")


def test_the_written_contract_carries_the_registered_version(tmp_path):
    store = _store_with_job(tmp_path)
    write_step1(store, "wp", "j1")
    data = json.loads(store.read_bytes(f"{BASE}/step-1.json"))
    assert data["contract_version"] == current_version(StepContract)


def test_the_step_1_plan_pins_the_hash_write_step1_wrote(tmp_path):
    store = _store_with_job(tmp_path)
    written = write_step1(store, "wp", "j1")
    pinned = store.read_bytes(f"{BASE}/step-1.sha256").decode().strip()
    job = load_inputs(store, "wp", "j1")
    steps, _hub = build_step_plans(job, contracts=_contract_refs(store, job))
    step1 = next(p for p in steps if p.step == 1)
    assert step1.contract is not None
    assert step1.contract.sha256 == pinned == written["sha256"]
    assert step1.contract.artifact == "transformation-contract/v2/step-1.json"


def test_a_job_with_no_version_in_its_files_uses_the_one_from_its_path(tmp_path):
    from src.tools.data_movement.plan import write_plans

    store = LocalArtifactStore(str(tmp_path))
    inp = _inputs()
    store.write_json(
        "wp/j1/synthesis/v2/report.json", {"migration_waves": inp.report["migration_waves"]}
    )
    store.write_json("wp/j1/assignment/v2/assignment.json", {"query_assignments": []})
    store.write_json("wp/j1/collector/output.json", inp.collector)
    out = write_step1(store, "wp", "j1")
    assert out["paths"][0] == f"{BASE}/step-1.json"
    assert json.loads(store.read_bytes(f"{BASE}/step-1.json"))["assignment_version"] == 2
    write_plans(store, "wp", "j1")
    plan = store.read_json("wp/j1/data-movement/step-1/plan.json")
    assert plan["assignment_version"] == 2
    assert plan["contract"] == {
        "artifact": "transformation-contract/v2/step-1.json",
        "sha256": out["sha256"],
    }


def test_contract_refs_use_the_resolved_version_not_the_files(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    inp = _inputs()
    store.write_json(
        "wp/j1/synthesis/v2/report.json", {"migration_waves": inp.report["migration_waves"]}
    )
    store.write_json("wp/j1/assignment/v2/assignment.json", {"query_assignments": []})
    store.write_json("wp/j1/collector/output.json", inp.collector)
    out = write_step1(store, "wp", "j1")
    refs = _contract_refs(store, load_inputs(store, "wp", "j1"))
    assert refs[1].artifact == "transformation-contract/v2/step-1.json"
    assert refs[1].sha256 == out["sha256"]


def test_a_failed_write_leaves_no_stale_pin(tmp_path, monkeypatch):
    store = _store_with_job(tmp_path)
    write_step1(store, "wp", "j1")
    assert store.exists(f"{BASE}/step-1.sha256")
    real = store.write_bytes

    def fail_on_contract(path, data):
        if path.endswith("step-1.json"):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(store, "write_bytes", fail_on_contract)
    try:
        write_step1(store, "wp", "j1")
    except OSError:
        pass
    else:
        raise AssertionError("the write should have failed")
    assert not store.exists(f"{BASE}/step-1.sha256")
