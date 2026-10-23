"""Shared inputs for the public ``assess()`` tests: the committed sample collections."""

from __future__ import annotations

import json
import zipfile
from functools import cache
from pathlib import Path
from typing import Any

from src.tools.database.collection_text import extract_json_object

REPO = Path(__file__).resolve().parents[3]

# wordpress (MySQL), discourse (PostgreSQL) and trailgear, the synthetic SQL Server
# sample (a heterogeneous source: the only one that reaches the Aurora engine choice).
SAMPLES = ("wordpress", "discourse", "trailgear")

# The trimmed AdventureWorks collection (SQL Server, a collector output): a second
# heterogeneous source, beside the samples.
ADVENTUREWORKS = "adventureworks"
COLLECTIONS = (*SAMPLES, ADVENTUREWORKS)


@cache
def _sample_text(name: str) -> str:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        return z.read(f"{name}-collection.json").decode("utf-8")


def sample_collection(name: str) -> dict[str, Any]:
    """A fresh copy of one sample's raw collection, as the collection scripts wrote it."""
    loaded: dict[str, Any] = json.loads(extract_json_object(_sample_text(name)))
    return loaded


def collection(name: str) -> dict[str, Any]:
    """A fresh copy of one of :data:`COLLECTIONS`: a sample's raw collection, or the
    AdventureWorks collector output (stamped with the collector contract's version,
    which its fixture predates)."""
    if name != ADVENTUREWORKS:
        return sample_collection(name)
    from src.contracts.collector_output import CollectorOutputContract

    path = REPO / "tests" / "fixtures" / "adventureworks_trimmed_collection.json"
    loaded: dict[str, Any] = json.loads(path.read_text())
    loaded["contract_version"] = CollectorOutputContract.model_fields["contract_version"].default
    return loaded


@cache
def _assessed(name: str, run_through: str) -> Any:
    from src.assessment import assess

    return assess(
        {
            "database_name": name,
            "collection": collection(name),
            "options": {"run_through": run_through},
        }
    )


PLACED_JOB = "assess02"


@cache
def _placed(name: str) -> tuple[Any, Any, Any]:
    import tempfile

    from src.assessment import assess
    from src.assessment.result_builder import load_run_state
    from src.storage.local_store import LocalArtifactStore

    # Kept for the test process, so a test can run a later phase on the same job.
    store = LocalArtifactStore(tempfile.mkdtemp(prefix=f"assess-{name}-"))
    result = assess(
        {"database_name": name, "collection": collection(name)}, store=store, job_id=PLACED_JOB
    )
    return result, load_run_state(store, name, PLACED_JOB, "reality_check"), store


def placed_run(name: str) -> tuple[Any, Any, Any]:
    """``(result, run state, store)`` of one ``run_through="reality_check"`` run that
    kept its artifacts (computed once per test process; the result is a copy)."""
    result, state, store = _placed(name)
    return result.model_copy(deep=True), state, store


def assessed(name: str, run_through: str) -> Any:
    """The deterministic result for one sample (computed once per test process)."""
    return _assessed(name, run_through).model_copy(deep=True)
