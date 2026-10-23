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


@cache
def _sample_text(name: str) -> str:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        return z.read(f"{name}-collection.json").decode("utf-8")


def sample_collection(name: str) -> dict[str, Any]:
    """A fresh copy of one sample's raw collection, as the collection scripts wrote it."""
    loaded: dict[str, Any] = json.loads(extract_json_object(_sample_text(name)))
    return loaded


@cache
def _assessed(name: str, run_through: str) -> Any:
    from src.assessment import assess

    return assess(
        {
            "database_name": name,
            "collection": sample_collection(name),
            "options": {"run_through": run_through},
        }
    )


def assessed(name: str, run_through: str) -> Any:
    """The deterministic result for one sample (computed once per test process)."""
    return _assessed(name, run_through).model_copy(deep=True)
