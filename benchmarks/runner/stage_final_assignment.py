"""Final-assignment stage: run the deterministic pipeline through the reality check (#1074).

The ``assignment`` stage scores the resolver's first pass from gold triage and
analysis inputs. The deliverables show something later: the assignment the
reality check leaves behind, after consolidation. This stage runs the same
steps ``tests/e2e/pipeline.py`` runs, with no model, from a raw collection
(``collect``, ``triage``, every selected engine's ``analysis``, ``assignment``,
``reality check``) and returns ``{query_id: assigned_engine}`` from the newest
assignment version, so a key for a whole sample measures what a customer sees.

Collect runs as the ``scripts/run_collect.py`` subprocess (it parses the raw
collection and makes no routing decision); every routing step runs in this
process, so a test can patch the scorer and see the effect on the final
assignment. No model, no AWS call.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own collect script with a fixed argv
import sys
import zipfile
from pathlib import Path

from benchmarks.runner.loader import Case
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[2]
SAMPLES_DIR = REPO / "docs" / "examples"
_JOB = "case"


def sample_zip(sample: str) -> Path:
    """The bundled sample's zip, ``docs/examples/<sample>/<sample>.zip``."""
    path = SAMPLES_DIR / sample / f"{sample}.zip"
    if not path.is_file():
        raise FileNotFoundError(f"no bundled sample {sample!r} at {path}")
    return path


def extract_sample(sample: str, dest: Path) -> Path:
    """Unzip ``<sample>-collection.json`` into ``dest`` and return its path."""
    dest.mkdir(parents=True, exist_ok=True)
    member = f"{sample}-collection.json"
    with zipfile.ZipFile(sample_zip(sample)) as z:
        z.extract(member, dest)
    return dest / member


def case_db(case: Case) -> str:
    """The database name the case runs under: its sample name, as the e2e run uses."""
    return case.sample or case.case_id


def _collection_file(case: Case, work_dir: Path) -> Path:
    if case.sample:
        return extract_sample(case.sample, work_dir / "input")
    path = case.path / "collection.json"
    if not path.is_file():
        raise FileNotFoundError(f"{case.case_id}: no 'sample' in index.json and no {path}")
    return path


def _collect(collection: Path, db: str, artifact_root: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    args = ["--file", str(collection), "--db", db, "--job-id", _JOB]
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's own script
        [sys.executable, "scripts/run_collect.py", *args, "--artifact-root", str(artifact_root)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    last = (proc.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        status = json.loads(last).get("status")
    except json.JSONDecodeError:
        status = None
    if proc.returncode != 0 or status != "complete":
        raise RuntimeError(
            f"collect failed (exit {proc.returncode}): {last[:500]}\n{proc.stderr[-1500:]}"
        )


def run_pipeline_to_final_assignment(case: Case, work_dir: Path) -> tuple[LocalArtifactStore, str]:
    """Run collect through the reality check; return the store and the job prefix."""
    from scripts.run_analysis import run_standard as run_analysis
    from src.agents.referee.assignment_handler import run_assignment_resolver
    from src.agents.referee.reality_check_handler import run_reality_check_handler
    from src.agents.referee.triage_handler import run_triage

    work_dir = Path(work_dir)
    artifact_root = work_dir / "artifacts"
    db = case_db(case)
    _collect(_collection_file(case, work_dir), db, artifact_root)

    store = LocalArtifactStore(str(artifact_root))
    run_triage(_JOB, db, store)
    triage = store.read_json(f"{db}/{_JOB}/referee-triage/triage.json")
    for agent in triage.get("selected_agents", []):
        run_analysis(store, _JOB, db, agent["agent_type"], "none")
    run_assignment_resolver(_JOB, db, store)
    run_reality_check_handler(_JOB, db, store, llm_mode="none")
    return store, f"{db}/{_JOB}"


def final_assignment(store: LocalArtifactStore, db: str) -> dict:
    """The newest assignment version: the reality check's when it wrote one, else v1."""
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        resolve_downstream_assignment_version,
    )

    version = resolve_downstream_assignment_version(store, db, _JOB)
    return store.read_json(assignment_artifact_path(db, _JOB, version))


def run_final_assignment_case(case: Case, work_dir: Path) -> dict[str, str]:
    """Run the case's collection through the reality check; return {query_id: engine}."""
    store, _ = run_pipeline_to_final_assignment(case, work_dir)
    assignment = final_assignment(store, case_db(case))
    return {qa["query_id"]: qa["assigned_engine"] for qa in assignment.get("query_assignments", [])}
