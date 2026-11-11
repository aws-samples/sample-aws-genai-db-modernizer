# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's delivery for the deterministic wordpress job (#1798).

``render_dms_step.py`` runs after the transformation contract and the data movement plan and
writes the DMS stack, the suggested Aurora template and the DDL file; the stack selects the
contract's 50 tables one by one and the DDL creates all of them. The sample collection predates
#2128 and measured no LOB value, so step 1 first writes ``lob-sizes.sql`` and no stack (#2123);
the customer's output of that script, read by ``run_dms_step.py lob-sizes``, brings the stack.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env

FILES = ("aurora-target.yaml", "step-1.ddl.sql", "dms-step-1.yaml")


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=900
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _measure(root: Path, run: PipelineResult, step: Path) -> None:
    """What the customer's run of lob-sizes.sql prints, every LOB value 10,000 bytes long, and
    the inputs file run_dms_step.py reads (ids and ARNs only)."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_step import load_step1, lob_measure_columns
    from src.tools.data_movement.lob_sizes import END, MAGIC, layout_sha
    from tests.unit.scripts.test_run_dms_step import INPUTS

    inputs = load_step1(LocalArtifactStore(base_dir=str(root)), run.db, run.job_id, with_ddl=False)
    cols = lob_measure_columns(inputs)
    lines = [
        f"{MAGIC}\t1\t{inputs.contract_sha256}\t{layout_sha(cols)}\t2026-11-11T10:00:00.000000"
    ]
    lines += [f"column\t{c.n}\t{c.k}\t10000\t1" for c in cols] + [f"{END}\t\t\t\t"]
    (step / "lob-sizes.tsv").write_text("\n".join(lines) + "\n")
    (step / "inputs.json").write_text(json.dumps(INPUTS))


@pytest.mark.deterministic
def test_the_wordpress_job_gets_the_three_step_1_files(
    designed_run: PipelineResult, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    shutil.copytree(designed_run.job_dir(), root / designed_run.db / designed_run.job_id)
    common = [
        "--job-id",
        designed_run.job_id,
        "--db",
        designed_run.db,
        "--artifact-root",
        str(root),
    ]
    contract = _script("scripts/run_transformation_contract.py", *common)
    assert contract["rule_count"] == 50 and contract["blocked_rule_count"] == 0, contract
    _script("scripts/run_data_movement_plan.py", *common)
    out = _script("scripts/render_dms_step.py", *common)
    assert out["status"] == "complete" and out["lob_sizes_needed"] > 0, out
    step = root / designed_run.db / designed_run.job_id / "data-movement" / "step-1"
    assert (step / "lob-sizes.sql").is_file() and not (step / "dms-step-1.yaml").exists()
    _measure(root, designed_run, step)
    lob = _script("scripts/run_dms_step.py", "lob-sizes", *common)
    assert lob["status"] == "measured" and lob["unmeasured_tables"] == 0, lob
    for name in FILES:
        assert (step / name).is_file(), name
    dms = (step / "dms-step-1.yaml").read_text()
    # The table mappings are compact JSON (#1915), so match with or without a space.
    assert len(re.findall(r'"rule-type":\s*"selection"', dms)) == 50
    assert not re.search(r'"rule-type":\s*"transformation"', dms)
    ddl = (step / "step-1.ddl.sql").read_text()
    assert len(re.findall(r"^CREATE TABLE ", ddl, re.M)) == 50
