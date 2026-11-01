# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A file that is not an offline collection fails the collect phase with its message
(#131, #1755 review).

The collector handler raises ``OfflineCollectionError`` rather than exiting, so each
caller's failure path records it: the ``LocalOrchestrator`` (the API's phase resume)
marks ``collect_triage`` failed with the message, and ``entrypoint.py`` writes
``collector/error.json``. An exit from the
handler used to slip past both: the phase stayed in progress and no error was written.
"""

from __future__ import annotations

from importlib import reload
from pathlib import Path
from unittest.mock import patch

import pytest

from src.contracts.phase_models import Phase, PhaseStatus
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_collection_check import OfflineCollectionError

DB, JOB = "shop", "job-131"
NOT_A_COLLECTION = {"name": "shop", "rows": []}
OFFLINE_ENV = {
    "S3_BUCKET": "bucket",
    "COLLECTION_MODE": "offline",
    "OFFLINE_S3_KEY": f"{DB}/{JOB}/uploads/collector-output.json",
    "AWS_REGION": "us-east-1",
}


def test_the_orchestrator_resume_records_the_phase_failed_with_the_message(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/_meta.json", {"database_name": DB})
    orch = LocalOrchestrator(store)

    with (
        patch.dict("os.environ", OFFLINE_ENV, clear=True),
        patch(
            "src.tools.database.offline_parser.fetch_offline_json", return_value=NOT_A_COLLECTION
        ),
        pytest.raises(OfflineCollectionError),
    ):
        orch.resume(JOB, Phase.COLLECT_TRIAGE)

    record = orch.get_progression(JOB).phases[Phase.COLLECT_TRIAGE]
    assert record.status == PhaseStatus.FAILED
    assert record.error_message is not None
    assert record.error_message.startswith("The offline collection s3://bucket/")
    assert "neither 'tables' nor 'queries'" in record.error_message


def test_the_entrypoint_writes_collector_error_json(tmp_path: Path) -> None:
    env = {
        **OFFLINE_ENV,
        "AGENT_TYPE": "collector",
        "JOB_ID": JOB,
        "DATABASE_NAME": DB,
        "ARTIFACT_DIR": str(tmp_path),
    }
    env.pop("S3_BUCKET")  # a local store under ARTIFACT_DIR
    env["OFFLINE_S3_BUCKET"] = "bucket"
    with (
        patch.dict("os.environ", env, clear=True),
        patch(
            "src.tools.database.offline_parser.fetch_offline_json", return_value=NOT_A_COLLECTION
        ),
    ):
        import src.agents.entrypoint as ep

        reload(ep)
        with pytest.raises(OfflineCollectionError):
            ep.main()

    error = LocalArtifactStore(base_dir=str(tmp_path)).read_json(f"{DB}/{JOB}/collector/error.json")
    assert error["error_type"] == "OfflineCollectionError"
    assert "neither 'tables' nor 'queries'" in error["error_message"]
