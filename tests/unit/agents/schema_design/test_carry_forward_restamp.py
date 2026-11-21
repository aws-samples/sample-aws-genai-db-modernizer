# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The carry-forward restamps a copied design trace to the current version (#2515).

A post-schema reroute copies an unchanged part's draft and design trace from version N
to N+1. The draft is copied byte for byte, but the trace goes through
``write_design_trace``, so it carries the contract version of the day, not the one it
was saved at. These tests call ``carry_forward_parts`` directly on a local store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.schema_design.carry_forward import carry_forward_parts
from src.contracts.schema_design_trace import DesignTraceGroup
from src.shared.contract_versioning import current_version
from src.storage.local_store import LocalArtifactStore

pytestmark = pytest.mark.passes_on_main

DB, JOB, ENGINE = "shop", "job-1", "dynamodb"
OLD = f"{DB}/{JOB}/schema-{ENGINE}/v1"
NEW = f"{DB}/{JOB}/schema-{ENGINE}/v2"
CURRENT = current_version(DesignTraceGroup)
DRAFT = b'{"table_definitions": []}'
REVIEWED = {
    "trace": [{"type": "pe_review", "iteration": 0, "verdict": "APPROVED"}],
    "routing_flags": [{"query_ids": ["q1"], "flagged_engine": "dynamodb"}],
}


def _put(root: Path, key: str, data: dict[str, Any]) -> None:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _raw(root: Path, key: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / key).read_text())
    return data


def _seed(root: Path, trace: dict[str, Any] | None) -> LocalArtifactStore:
    """One part, with the same input on v1 and v2; ``trace`` is v1's saved trace."""
    for base in (OLD, NEW):
        _put(root, f"{base}/groups_manifest.json", {"groups": [{"group_index": 1}]})
        _put(root, f"{base}/input_group_1.json", {"collector_output": {"queries": {}}})
    (root / OLD / "schema_draft_group_1.json").write_bytes(DRAFT)
    if trace is not None:
        _put(root, f"{OLD}/design_trace_group_1.json", trace)
    return LocalArtifactStore(base_dir=str(root))


def _carry(store: LocalArtifactStore) -> dict[int, int]:
    return carry_forward_parts(store, DB, JOB, ENGINE, 1, 2)


def test_a_copied_trace_is_restamped_to_the_current_version(tmp_path: Path) -> None:
    store = _seed(tmp_path, {"contract_version": "1.0", **REVIEWED})
    assert CURRENT != "1.0"

    assert _carry(store) == {1: 1}

    copied = _raw(tmp_path, f"{NEW}/design_trace_group_1.json")
    assert copied["contract_version"] == CURRENT
    assert copied["trace"] == REVIEWED["trace"]
    assert copied["routing_flags"] == REVIEWED["routing_flags"]
    # The source trace is left as it was saved.
    assert _raw(tmp_path, f"{OLD}/design_trace_group_1.json")["contract_version"] == "1.0"


def test_a_trace_saved_before_the_contract_gets_the_current_version(tmp_path: Path) -> None:
    store = _seed(tmp_path, dict(REVIEWED))

    assert _carry(store) == {1: 1}

    assert _raw(tmp_path, f"{NEW}/design_trace_group_1.json") == {
        "contract_version": CURRENT,
        **REVIEWED,
    }


def test_a_trace_already_at_the_current_version_is_copied_unchanged(tmp_path: Path) -> None:
    store = _seed(tmp_path, {"contract_version": CURRENT, **REVIEWED})

    assert _carry(store) == {1: 1}

    assert _raw(tmp_path, f"{NEW}/design_trace_group_1.json") == {
        "contract_version": CURRENT,
        **REVIEWED,
    }


def test_a_part_with_no_trace_still_carries_its_draft(tmp_path: Path) -> None:
    store = _seed(tmp_path, None)

    assert _carry(store) == {1: 1}

    assert (tmp_path / NEW / "schema_draft_group_1.json").read_bytes() == DRAFT
    assert not (tmp_path / NEW / "design_trace_group_1.json").exists()
    record = _raw(tmp_path, f"{NEW}/carried_forward.json")
    assert record["parts"][0]["trace_sha256"] is None
