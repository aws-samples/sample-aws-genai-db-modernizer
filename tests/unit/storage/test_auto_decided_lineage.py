# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A decide-for-me version edits the lineage it was applied to (#1003): Reality
Check does not run again for it, and it is never taken for a customer approval."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.storage.assignment_versioning import reality_check_is_current
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"


def _write(store: LocalArtifactStore, version: int, **fields: object) -> None:
    store.write_json(
        f"{DB}/{JOB}/assignment/v{version}/assignment.json",
        {"version": version, "query_assignments": [], **fields},
    )


@pytest.mark.parametrize("source", ["auto_decided", "customer_gate"])
def test_a_decided_version_continues_the_reality_checked_lineage(tmp_path, source) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write(store, 1, source="assignment_resolution", previous_version=None)
    _write(store, 2, source="reality_check", previous_version=1, reality_check_applied=True)
    _write(store, 3, source=source, previous_version=2)
    assert reality_check_is_current(store, DB, JOB)


def test_approving_an_auto_decided_version_is_the_customer_s_own_approval(tmp_path) -> None:
    """A customer edit already implies approval and is left as is; a version the
    rule decided does not, so the customer's approve-as-is stamps it."""
    from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
    from src.contracts.assignment_models import (
        Assignment,
        AssignmentSource,
        AssignmentStatus,
    )

    store = LocalArtifactStore(base_dir=str(tmp_path))
    for status, source, expected in (
        (AssignmentStatus.AUTO_DECIDED, AssignmentSource.AUTO_DECIDED, "customer_approved"),
        (AssignmentStatus.CUSTOMER_MODIFIED, AssignmentSource.CUSTOMER_GATE, "customer_modified"),
    ):
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=status,
            source=source,
            timestamp=datetime.now(UTC),
            query_assignments=[],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        path = f"{DB}/{JOB}/assignment/v1/assignment.json"
        store.write_json(path, assignment.model_dump(mode="json"))
        mark_assignment_customer_approved(store, DB, JOB)
        assert store.read_json(path)["status"] == expected
