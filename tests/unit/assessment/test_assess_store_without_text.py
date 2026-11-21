# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``assess(store=...)`` on a caller's store that cannot write text (#2514).

``assess()`` passes a caller's store through as given when it has ``write_text``. The
analyses write their ER diagrams (``er-diagram.mmd``) with it; a store whose
``write_text`` raises ``NotImplementedError`` still finishes every analysis, with no
diagram.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.assessment import assess
from src.storage.text_store import TextLocalStore
from tests.unit.assessment.conftest import collection

JOB = "notext01"
DB = "wordpress"


class _NoTextStore(TextLocalStore):
    """A caller's store: every artifact method works, ``write_text`` is refused."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        raise NotImplementedError("this store cannot write text")


@pytest.mark.passes_on_main  # main's tolerance, kept for assess(store=...) callers
def test_a_caller_store_that_cannot_write_text_still_finishes_every_analysis(
    tmp_path: Path,
) -> None:
    store = _NoTextStore(str(tmp_path))
    result = assess(
        {"database_name": DB, "collection": collection(DB), "options": {"run_through": "analysis"}},
        store=store,
        job_id=JOB,
    )
    assert result.summary.run_through_reached == "analysis"
    job = tmp_path / DB / JOB
    analyzed = sorted(p.parent.name for p in job.glob("analysis-*/analysis.json"))
    assert {"analysis-documentdb", "analysis-dynamodb"} <= set(analyzed)
    assert not list(job.rglob("*.mmd"))
    assert all(s.fit for s in result.statements)
