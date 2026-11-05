# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ElastiCache never owns a query and never holds the only copy of the data (#296, #1860).

On every bundled sample's run (and a headless run's job), every saved assignment version
and the report it was rendered from: no query is owned by a cache or tied to one, no
table's owner, primary engine or source of truth is a cache, and every query a cache
fronts reads only tables whose owner is a system of record. The ledger's refusal is in
``tests/unit/agents/referee/test_elasticache_never_owns_a_query.py``.
"""

from __future__ import annotations

import json
from typing import Any

from src.shared.cache_engines import CACHE_ENGINES
from src.shared.catalog import ACTIVE, engine_ids
from tests.e2e.pipeline import PipelineResult

SYSTEMS_OF_RECORD = engine_ids(status=ACTIVE, system_of_record=True)


def _versions(run: PipelineResult) -> list[dict[str, Any]]:
    paths = sorted((run.job_dir() / "assignment").glob("v*/assignment.json"))
    assert paths, f"no saved assignment under {run.job_dir()}"
    return [json.loads(p.read_text()) for p in paths]


def test_no_saved_assignment_or_report_lets_a_cache_own_a_query(run: PipelineResult) -> None:
    for version in _versions(run):
        v = version.get("version")
        for qa in version["query_assignments"]:
            assert qa["assigned_engine"] not in CACHE_ENGINES, (v, qa["query_id"])
            assert not set(qa.get("tied_engines") or []) & CACHE_ENGINES, (v, qa["query_id"])
            if qa.get("cache_engine"):
                assert qa["cache_engine"] in CACHE_ENGINES, (v, qa["query_id"])
                assert qa["assigned_engine"] in engine_ids(status=ACTIVE) - CACHE_ENGINES
        for row in version.get("table_assignments") or []:
            assert row["primary_engine"] not in CACHE_ENGINES, (v, row["table_id"])
            assert row.get("source_of_truth") not in CACHE_ENGINES, (v, row["table_id"])
            assert not set(row.get("write_engines") or []) & CACHE_ENGINES, (v, row["table_id"])
    report = json.loads(run.report_json().read_text())
    for m in report["table_mappings"]:
        assert m.get("owner") not in CACHE_ENGINES, m["source_table"]
        assert m.get("source_of_truth") not in CACHE_ENGINES, m["source_table"]


def test_every_table_a_cache_fronts_keeps_a_system_of_record_owner(run: PipelineResult) -> None:
    """The cache is never the only copy: each table a cached query reads has a system of
    record as its owner in the report, and so in every deliverable that renders it."""
    report = json.loads(run.report_json().read_text())
    owners = {m["source_table"]: m for m in report["table_mappings"]}
    (latest,) = [
        v for v in _versions(run) if f"/synthesis/v{v['version']}/" in str(run.report_json())
    ]
    cached_tables = {
        t
        for qa in latest["query_assignments"]
        if qa.get("cache_engine") and qa.get("in_scope", True)
        for t in qa.get("source_tables") or []
        if t in owners
    }
    for table in cached_tables | {t for t, m in owners.items() if m.get("cached_by")}:
        row = owners[table]
        assert row["owner"] in SYSTEMS_OF_RECORD, (table, row["owner"], row.get("cached_by"))
        assert row.get("source_of_truth") in (None, *SYSTEMS_OF_RECORD), table
