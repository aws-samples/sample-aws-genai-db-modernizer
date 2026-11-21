# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collection ingest writes no literal value to disk (#1625).

A collector output handed in as the collection (or one saved before #1625) can hold
the literals of the text its collector read. ``run_assessment.py``, ``run_collect.py``
and the library ingest redact it before they write anything, so no file under the job holds a seeded value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment, run_collect
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection
from tests.fixtures.pii_collections import ENGINES, keep_literals, leaked, pii_collection


def _uploaded_collector_output(engine: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A collector output whose text still holds the seeded values (an older one, or
    one a caller built), at today's contract version."""
    with monkeypatch.context() as patch:
        keep_literals(patch)
        out = build_offline_collector_output(
            parse_offline_collection(pii_collection(engine)),
            job_id="old",
            database_name="trailgear",
            s3_key="x",
        )
    out["contract_version"] = CollectorOutputContract.model_fields["contract_version"].default
    assert leaked(json.dumps(out))
    return out


def _no_file_leaks(root: Path) -> None:
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert not leaked(path.read_text(errors="ignore")), path


@pytest.mark.parametrize("engine", ENGINES)
def test_run_assessment_writes_an_uploaded_collector_output_redacted(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(_uploaded_collector_output(engine, monkeypatch)))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), None, LocalArtifactStore(base_dir=str(root)))
    assert list(root.rglob("collector/output.json"))
    _no_file_leaks(root)


@pytest.mark.parametrize("engine", ENGINES)
def test_run_collect_writes_an_uploaded_collector_output_redacted(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(_uploaded_collector_output(engine, monkeypatch)))
    root = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys, "argv", ["run_collect.py", "--file", str(path), "--artifact-root", str(root)]
    )
    run_collect.main()
    assert list(root.rglob("collector/output.json"))
    _no_file_leaks(root)


@pytest.mark.parametrize("engine", ENGINES)
def test_the_scripts_copy_of_a_raw_collection_is_redacted(engine: str, tmp_path: Path) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(pii_collection(engine)))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), None, LocalArtifactStore(base_dir=str(root)))
    assert list(root.rglob("uploads/collector-output.json"))
    _no_file_leaks(root)
