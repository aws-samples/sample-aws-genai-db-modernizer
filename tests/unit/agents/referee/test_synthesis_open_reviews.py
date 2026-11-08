# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis records each design's open reviews in its schema summary (#1992).

A design whose last review did not approve it holds its wave
(``src.shared.wave_readiness``); synthesis counts them from the design trace it already
loads, so the facts layer reads them from ``report.json`` alone.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_handler import _engine_schema_summaries
from src.contracts.schema_design_trace import DesignTrace

DESIGN = {
    "table_definitions": [{"table_name": "orders", "source_tables": ["shop.orders"]}],
    "access_patterns": [],
}


def _data(trace: dict | None) -> SynthesisData:
    data = SynthesisData(job_id="j", database_name="shop")
    data.engines["dynamodb"] = EngineArtifacts(
        engine="dynamodb",
        schema_design=DESIGN,
        design_trace=DesignTrace.model_validate(trace) if trace is not None else None,
    )
    return data


def _review(verdict: str) -> dict:
    return {"type": "pe_review", "iteration": 0, "verdict": verdict, "change_requests": 1}


def test_a_design_whose_last_review_asked_for_changes_counts_one() -> None:
    trace = {"trace": [{"type": "designer", "iteration": 0}, _review("CHANGES_REQUESTED")]}
    assert _engine_schema_summaries(_data(trace))["dynamodb"]["open_review_count"] == 1


def test_each_group_of_a_grouped_design_counts_on_its_own() -> None:
    trace = {
        "total_groups": 3,
        "groups": [
            {"trace": [_review("CHANGES_REQUESTED")]},
            {"trace": [_review("APPROVED")]},
            {"trace": [_review("REJECTED")]},
        ],
    }
    assert _engine_schema_summaries(_data(trace))["dynamodb"]["open_review_count"] == 2


def test_an_approved_or_unreviewed_design_counts_none() -> None:
    assert (
        _engine_schema_summaries(_data({"trace": [_review("APPROVED")]}))["dynamodb"][
            "open_review_count"
        ]
        == 0
    )
    assert _engine_schema_summaries(_data(None))["dynamodb"]["open_review_count"] == 0


@pytest.mark.passes_on_main  # a guard: a summary with no design is left as it was
def test_no_design_records_no_count() -> None:
    data = SynthesisData(job_id="j", database_name="shop")
    data.engines["dynamodb"] = EngineArtifacts(engine="dynamodb")
    assert "open_review_count" not in _engine_schema_summaries(data)["dynamodb"]


def test_a_review_that_failed_counts_as_failed_not_open() -> None:
    trace = {"trace": [_review("CHANGES_REQUESTED"), {"type": "pe_error", "iteration": 1}]}
    summary = _engine_schema_summaries(_data(trace))["dynamodb"]
    assert summary["open_review_count"] == 0 and summary["failed_review_count"] == 1
