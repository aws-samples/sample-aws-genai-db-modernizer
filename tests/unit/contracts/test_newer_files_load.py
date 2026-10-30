# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A newer file, with a field this code does not know, still loads (ADR-008 Decision 5, #976).

The models below used ``extra="forbid"``, so an older reader rejected a newer
file outright (an ``Assignment`` whose ``moves`` gained a field, a groups
manifest, a feasibility finding). They are read back across versions, so they
now accept unknown fields and keep them on a round trip. A model's answer still
refuses them (``tests/contract/test_extra_policy.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts.assignment_models import Assignment
from src.contracts.design_metrics import DesignMetricsContract
from src.contracts.executive_summary_response import ExecutiveSummaryResponse
from src.contracts.feasibility_models import FeasibilityFinding, FindingKind, FindingSeverity
from src.contracts.model_flag import ModelFlag
from src.contracts.move_ledger import MoveEvidence
from src.contracts.schema_design_input import SchemaDesignGroupsManifest
from src.shared.contract_versioning import load_contract
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.ledger_fakes import recorded_ledger
from tests.unit.agents.referee.test_cache_safety_net_notes_reset import ASSIGNMENT_V2

NEWER = {"added_in_a_later_minor": {"kept": True}}


def _assignment_with_moves_and_findings() -> dict:
    before = [{"query_id": "hot1", "assigned_engine": "aurora_mysql"}]
    after = [{"query_id": "hot1", "assigned_engine": "dynamodb"}]
    (move,) = recorded_ledger(before, after).moves
    finding = FeasibilityFinding(
        kind=FindingKind.READ_WRITE_SPLIT,
        severity=FindingSeverity.ADVISORY,
        table="t.orders",
        engines=["dynamodb", "opensearch"],
        message="needs replication",
    )
    data: dict = json.loads(
        Assignment.model_validate(
            {**ASSIGNMENT_V2, "moves": [move], "accepted_feasibility_findings": [finding]}
        ).model_dump_json()
    )
    data["moves"][0].update(NEWER)
    data["accepted_feasibility_findings"][0].update(NEWER)
    return data


def test_an_assignment_with_a_newer_move_and_finding_loads(tmp_path: Path) -> None:
    key = "db/job/assignment/v2/assignment.json"
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_assignment_with_moves_and_findings()))

    stored = LocalArtifactStore(base_dir=str(tmp_path)).read_json(key)
    loaded = load_contract(Assignment, stored, artifact=key)

    dumped = json.loads(loaded.model_dump_json(by_alias=True))
    assert dumped["moves"][0]["added_in_a_later_minor"] == {"kept": True}
    assert dumped["accepted_feasibility_findings"][0]["added_in_a_later_minor"] == {"kept": True}


def test_a_newer_move_evidence_field_is_kept() -> None:
    data = _assignment_with_moves_and_findings()
    # The settle Move carries no evidence of its own; give it a newer one.
    data["moves"][0]["evidence"] = {**MoveEvidence().model_dump(mode="json"), **NEWER}
    loaded = Assignment.model_validate(data)
    assert loaded.moves[0].evidence is not None
    assert loaded.moves[0].evidence.model_dump()["added_in_a_later_minor"] == {"kept": True}


def test_a_newer_groups_manifest_loads() -> None:
    manifest = {
        "job_id": "job",
        "database_name": "db",
        "target_engine": "dynamodb",
        "total_queries": 0,
        "total_groups": 0,
        "groups": [],
        **NEWER,
    }
    loaded = load_contract(
        SchemaDesignGroupsManifest,
        manifest,
        artifact="db/job/schema-dynamodb/v1/groups_manifest.json",
    )
    assert loaded.model_dump()["added_in_a_later_minor"] == {"kept": True}


def test_newer_design_metrics_load() -> None:
    metrics = {
        **DesignMetricsContract(engine="dynamodb").model_dump(mode="json"),
        **NEWER,
        "queries": [{"query_id": "q1", "base_rps": 1.0, "design_rps": 1.0, **NEWER}],
    }
    loaded = DesignMetricsContract.model_validate(metrics)
    assert loaded.model_dump()["added_in_a_later_minor"] == {"kept": True}
    assert loaded.queries[0].model_dump()["added_in_a_later_minor"] == {"kept": True}


def test_a_model_answer_with_an_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        ExecutiveSummaryResponse.model_validate(
            {"executive_summary": "A summary long enough to pass.", **NEWER}
        )
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        ModelFlag.model_validate(
            {
                "query_ids": ["q1"],
                "flagged_engine": "dynamodb",
                "reason_category": "full_scan",
                "explanation": "scans every item",
                "target_engine": "documentdb",
            }
        )
