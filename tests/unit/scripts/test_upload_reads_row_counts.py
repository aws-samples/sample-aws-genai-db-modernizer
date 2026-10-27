"""An uploaded collector output is read as a stored one is, before it is stamped (#1636).

``run_collect.py``, ``run_assessment.py`` and the library ingest
(``ingest_offline_collection``, which ``assess()`` and the API use) take a collector
output as the collection and write it under today's version. Before #1636 they only
dropped its sample rows and relabelled it, so an older output's 0 for a table its
source had no count for became a real 0 under the new label, while the same file read
from a job came back unknown. Each now reads the upload through
``read_contract_payload``: the adapter of an older MAJOR and every read's transforms.
Both are checked: an upload at today's version, and one at the MAJOR before today's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment, run_collect
from src.atx_orchestrator.core import ingest_offline_collection
from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import current_version
from src.storage.local_store import LocalArtifactStore


def _old_output() -> dict[str, Any]:
    """A PostgreSQL 13 collector output as 4.x wrote it: unknown counts are 0, no basis."""
    column = {"column_name": "id", "data_type": "int", "nullable": False}
    return {
        "job_id": "old",
        "metadata": {
            "collection_timestamp": "2026-10-20T00:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {
                "engine": "postgresql",
                "version": "13.4",
                "hostname": "h",
                "database_name": "shop",
            },
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.never_analysed",
                    "table_name": "never_analysed",
                    "row_count": 0,
                    "size_mb": 2.0,
                    "columns": [column],
                },
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 1200,
                    "size_mb": 2.0,
                    "columns": [column],
                },
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


@pytest.fixture(params=["current", "older_major"])
def upload(request: pytest.FixtureRequest) -> dict[str, Any]:
    out = _old_output()
    current = current_version(CollectorOutputContract)
    major = int(current.split(".")[0])
    if request.param == "older_major":
        # A file the MAJOR before today's wrote (4.x since #1636's bump): read through
        # its adapter, then stamped with today's version.
        out["contract_version"] = f"{major - 1}.3"
    else:
        out["contract_version"] = current
    return out


def _stored(root: Path) -> dict[str, Any]:
    [path] = list(root.rglob("collector/output.json"))
    stored: dict[str, Any] = json.loads(path.read_text())
    return stored


def _counts(stored: dict[str, Any]) -> dict[str, tuple]:
    return {
        t["table_id"]: (t["row_count"], t.get("row_count_basis"))
        for t in stored["database_schema"]["tables"]
    }


EXPECTED = {"shop.never_analysed": (None, None), "shop.orders": (1200, "estimate")}


def test_run_collect_reads_an_uploaded_output_by_the_row_count_rule(
    upload: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(upload))
    root = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys, "argv", ["run_collect.py", "--file", str(path), "--artifact-root", str(root)]
    )
    run_collect.main()
    stored = _stored(root)
    assert stored["contract_version"] == current_version(CollectorOutputContract)
    assert _counts(stored) == EXPECTED


def test_run_assessment_reads_an_uploaded_output_by_the_row_count_rule(
    upload: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(upload))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), None, LocalArtifactStore(base_dir=str(root)))
    stored = _stored(root)
    assert stored["contract_version"] == current_version(CollectorOutputContract)
    assert _counts(stored) == EXPECTED


def test_the_library_ingest_reads_an_uploaded_output_by_the_row_count_rule(
    upload: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    returned = ingest_offline_collection(
        LocalArtifactStore(base_dir=str(root)), "job1", "shop", upload
    )
    assert _counts(returned) == EXPECTED
    assert _counts(_stored(root)) == EXPECTED


def test_a_stored_output_with_no_version_reads_the_same(tmp_path: Path) -> None:
    # A collector output written before #972 has no contract_version: every read's
    # transform still reads its counts by the rule.
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/job1/collector/output.json", _old_output())
    assert _counts(store.read_json("shop/job1/collector/output.json")) == EXPECTED


def _discourse_as_4x_wrote_it() -> dict[str, Any]:
    """discourse's collector output as the 4.x collector stored it: every count clamped
    (``max(row_count or 0, 0)``, so PostgreSQL 16's -1 became 0) and no basis."""
    import zipfile

    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.collection_text import extract_json_object
    from src.tools.database.offline_parser import parse_offline_collection

    repo = Path(__file__).resolve().parents[3]
    with zipfile.ZipFile(repo / "docs" / "examples" / "discourse" / "discourse.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        raw = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="old", database_name="discourse", s3_key="k"
    )
    out["contract_version"] = "4.3"
    for table in out["database_schema"]["tables"]:
        table["row_count"] = max(table["row_count"] or 0, 0)
        for key in ("row_count_basis", "stats_analyzed_at", "data_size_mb"):
            table.pop(key, None)
    return out


def test_a_discourse_4x_upload_stores_its_265_unknown_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = _discourse_as_4x_wrote_it()
    tables = upload["database_schema"]["tables"]
    assert sum(1 for t in tables if t["row_count"] == 0) == 265
    # The library ingest (assess(), the API) and the CLI store the same thing.
    returned = ingest_offline_collection(
        LocalArtifactStore(base_dir=str(tmp_path / "lib")), "job1", "discourse", upload
    )
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(upload))
    root = tmp_path / "cli"
    monkeypatch.setattr(
        sys, "argv", ["run_collect.py", "--file", str(path), "--artifact-root", str(root)]
    )
    run_collect.main()
    for stored in (returned, _stored(root)):
        counts = [t["row_count"] for t in stored["database_schema"]["tables"]]
        assert (len(counts), counts.count(None), counts.count(0)) == (311, 265, 0)
