# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""assess() never hands on a saved Aurora design's SQL text unchecked (#2199 review).

The result copies each design into ``artifacts.schema_designs``; a design whose script
has no table definitions to rebuild it from (missing, null or an object) or that sits in
a skip placeholder is refused when the run reads it, so no result carries it.
"""

from __future__ import annotations

import tempfile
from typing import Any

import pytest

from src.assessment import assess
from src.storage.local_store import LocalArtifactStore
from tests import assess_models
from tests.assess_models import models_patched
from tests.contract.test_saved_aurora_design_ddl import UNTABLED, _untabled
from tests.unit.assessment.conftest import collection

DB = "wordpress"


@pytest.mark.parametrize("shape", UNTABLED)
def test_assess_refuses_a_design_whose_script_has_no_tables(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, shape: str
) -> None:
    def unsafe(store: Any, database_name: str, job_id: str, assignment_version: int) -> None:
        key = (
            f"{database_name}/{job_id}/schema-aurora_postgresql/v{assignment_version}"
            "/schema_output.json"
        )
        store.write_json(key, _untabled("aurora_postgresql", shape))

    monkeypatch.setattr(assess_models, "write_aurora_postgresql_design", unsafe)
    store = LocalArtifactStore(base_dir=tempfile.mkdtemp(prefix="assess-unsafe-"))
    options = {
        "run_through": "schema_design",
        "deterministic": False,
        "target_engines": ["aurora_postgresql", "dynamodb"],
    }
    with models_patched(), pytest.raises(Exception) as caught:
        assess(
            {"database_name": DB, "collection": collection(DB), "options": options},
            store=store,
            job_id=f"unsafe-{shape}",
        )
    assert "DROP" not in str(caught.value)
    # The design phase failed because its read refused the saved file, nothing else.
    refusals = [
        r.exc_info[1]
        for r in caplog.records
        if r.exc_info and type(r.exc_info[1]).__name__ == "UnsafeArtifactError"
    ]
    assert refusals, [r.getMessage() for r in caplog.records]
    assert all("DROP" not in str(e) for e in refusals)
