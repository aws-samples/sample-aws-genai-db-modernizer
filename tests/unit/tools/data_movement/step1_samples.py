# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's whole output for the bundled one-schema samples (#2494).

Each sample's collection goes through the offline builder into a finished job whose wave 1
moves every table into the Aurora engine of the source family. The output is every file
step 1 writes from it: the transformation contract and its findings, the plan, the files
``render_files`` writes, the DMS stack (also while a LOB size is unmeasured) and both verify
scripts.

``tests/fixtures/step1_one_schema_digests.json`` holds the SHA-256 of each file, recorded
on main before step 1 kept each table's schema. Record them again only for a deliberate
change of step 1's output:

    uv run python -m tests.unit.tools.data_movement.step1_samples --write

It is a generated baseline: ``scripts/regenerate_baselines.py`` names it when stale (this
module's ``--check``) and the baseline-edit hook refuses a hand edit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
DIGESTS = REPO / "tests" / "fixtures" / "step1_one_schema_digests.json"
SAMPLES = ("wordpress", "discourse")
_AURORA = {"mysql": "aurora_mysql", "postgresql": "aurora_postgresql"}


def _collection(name: str) -> dict:
    from src.tools.database.offline_collection_check import read_collection_text

    folder = REPO / "docs" / "examples" / name
    zipped = folder / f"{name}.zip"
    if zipped.exists():
        with zipfile.ZipFile(zipped) as z:
            text = z.read(f"{name}-collection.json").decode()
    else:
        text = (folder / f"{name}-collection.json").read_text()
    return read_collection_text(text, source=name)


def sample_outputs(name: str) -> dict[str, str]:
    """Every file step 1 writes for sample ``name``, by file name."""
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.debezium_connector import dump_template
    from src.tools.data_movement.dms_step import load_step1, render_dms_template, render_files
    from src.tools.data_movement.plan import write_plans
    from src.tools.data_movement.verify_scripts import render_script
    from src.tools.database.offline_parser import parse_offline_collection
    from src.tools.transformation_contract.write import write_step1

    collector = build_offline_collector_output(
        parse_offline_collection(_collection(name)), job_id="job1", database_name=name, s3_key="k"
    )
    engine = collector["metadata"]["source_database"]["engine"]
    ids = [t["table_id"] for t in collector["database_schema"]["tables"]]
    wave = {"wave": 1, "title": "Move to Aurora", "engines": [_AURORA[engine]],
            "moves_from": [engine], "homogeneity": "homogeneous", "tables": ids,
            "gate": "Aurora serves every query."}  # fmt: skip
    with tempfile.TemporaryDirectory() as root:
        store = LocalArtifactStore(root)
        job = f"{name}/job1"
        store.write_json(f"{job}/synthesis/v2/report.json",
                         {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
        store.write_json(f"{job}/assignment/v2/assignment.json",
                         {"version": 2, "query_assignments": []})  # fmt: skip
        store.write_json(f"{job}/collector/output.json", json.loads(json.dumps(collector)))
        write_step1(store, name, "job1")
        write_plans(store, name, "job1")
        out = {
            p: (Path(root) / job / p).read_text()
            for p in (
                "transformation-contract/v2/step-1.json",
                "transformation-contract/v2/findings.json",
                "data-movement/step-1/plan.json",
            )
        }
        inputs = load_step1(store, name, "job1")
        out.update(render_files(inputs))
        out["dms-step-1.yaml (rendered)"] = dump_template(render_dms_template(inputs))
        database = inputs.plan.source.database
        for side in ("source", "target"):
            out[f"verify-{side}.sql"] = render_script(
                inputs.contract, inputs.contract_sha256, side, target_database=database
            )
    return out


def digests(name: str) -> dict[str, str]:
    return {
        file: hashlib.sha256(text.encode()).hexdigest()
        for file, text in sorted(sample_outputs(name).items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="record the digests")
    mode.add_argument("--check", action="store_true", help="exit 1 when the recorded ones differ")
    args = parser.parse_args()
    found = {name: digests(name) for name in SAMPLES}
    text = json.dumps(found, indent=2, sort_keys=True) + "\n"
    if args.check:
        stale = not DIGESTS.exists() or json.loads(DIGESTS.read_text()) != found
        print(f"{'stale' if stale else 'fresh'}: {DIGESTS.relative_to(REPO)}")
        raise SystemExit(1 if stale else 0)
    if args.write:
        DIGESTS.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
