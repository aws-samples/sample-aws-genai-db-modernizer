# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's transformation contract for the sample jobs (#1764).

``scripts/run_transformation_contract.py`` runs on a copy of each finished job and writes
``step-1.json``. It must load as the registered contract, cover every table of wave 1
(a rule, or a view), and write a hash file that is the SHA-256 of the file's bytes
(tests/unit/tools/transformation_contract/test_write.py checks that the data movement plan
reads that hash back).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from collections import Counter
from pathlib import Path

from src.contracts.transformation_contract import StepContract
from src.shared.contract_versioning import load_contract
from tests.e2e.pipeline import REPO, PipelineResult, _env

SCRIPT = "scripts/run_transformation_contract.py"


def _write(run: PipelineResult, dest: Path) -> tuple[Path, dict]:
    root = dest / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            SCRIPT,
            "--job-id",
            run.job_id,
            "--db",
            run.db,
            "--artifact-root",
            str(root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=900,
    )
    assert proc.returncode == 0, f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    out: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return root, out


def _report(root: Path, run: PipelineResult) -> dict:
    reports = sorted((root / run.db / run.job_id / "synthesis").glob("v*/report.json"))
    data: dict = json.loads(reports[-1].read_text())
    return data


# Not marked deterministic: it also runs against an external job (E2E_ARTIFACT_ROOT), where
# every check below holds; only the wordpress numbers are tied to the bundled sample.
def test_each_sample_job_gets_a_valid_step_1_contract(run: PipelineResult, tmp_path: Path) -> None:
    root, out = _write(run, tmp_path)
    assert out["status"] == "complete", out
    path = root / out["paths"][0]
    contract = load_contract(StepContract, json.loads(path.read_bytes()), artifact=out["paths"][0])
    assert contract.step == 1 and contract.status in {
        "valid",
        "has_blocked_rules",
        "no_relational_anchor",
        "staged_move_pending",
    }

    digest = (root / out["paths"][1]).read_text()
    assert re.fullmatch(r"[0-9a-f]{64}\n", digest)
    assert digest.strip() == hashlib.sha256(path.read_bytes()).hexdigest() == out["sha256"]

    findings = json.loads((root / out["paths"][2]).read_text())
    assert findings["step"] == 1
    assert len(findings["findings"]) == sum(len(r.findings) for r in contract.rules)

    if contract.status in {"valid", "has_blocked_rules"}:
        wave1 = next(w for w in _report(root, run)["migration_waves"] if w["wave"] == contract.wave)
        covered = {r.source_table for r in contract.rules} | set(contract.views)
        assert set(wave1["tables"]) <= covered
        assert contract.blocked_rule_count == sum(r.status == "blocked" for r in contract.rules)

    codes = Counter(f.code for r in contract.rules for f in r.findings)
    blocked = [r.source_table for r in contract.rules if r.status == "blocked"]
    print(  # noqa: T201 - per-sample record for the PR
        f"STEP1 {run.db}: status={contract.status} rules={len(contract.rules)} "
        f"blocked={len(blocked)} findings={dict(codes)} blocked_tables={blocked[:5]}"
    )
    if run.db == "wordpress":
        assert contract.target.engine == "aurora_mysql"
        assert len(contract.rules) == 50
        assert contract.blocked_rule_count == 0 and out["blocked_rule_count"] == 0
