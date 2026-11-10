# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1990: the assignment gate states the assumptions block, the lines the chat gate prints
(``src.shared.assumptions.gate_assumption_lines``), with the customer's growth when set."""

from __future__ import annotations

from src.report.printed_summary import prose, render_gate_block
from src.shared.assumptions import gate_assumption_lines, record_growth
from tests.unit.api.test_assignment_cache_reads import DB, JOB, _store, _url, client


def test_the_gate_response_states_each_assumption_and_its_origin(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    lines = client.get(_url()).json()["assumptions"]
    assert lines[0].startswith("Growth: 5× (default), shown as a separate what-if scenario.")
    assert lines[1].startswith("Peak: not measured")
    assert lines[-1].startswith("Region and prices: ") and lines[-1].endswith("(default)")


def test_the_customers_growth_is_stated_as_the_customers(tmp_path, monkeypatch) -> None:
    from src.api.routes import assignments

    _store(tmp_path, monkeypatch, cached=False)
    record_growth(assignments.artifact_store, DB, JOB, 2, via="api")
    assert (
        client.get(_url()).json()["assumptions"][0].startswith("Growth: 2× (set by the customer)")
    )


def test_the_chat_gate_prints_the_same_lines_once() -> None:
    collector = {"queries": {"query_patterns": [{"query_id": "q", "calls_per_second": 1.0}]}}
    lines = gate_assumption_lines(collector, None)
    text = render_gate_block(
        {"after_distribution": {"dynamodb": 1}, "assumptions": lines},
        database_name="shop",
        job_id="job-1",
    )
    assert "Assumptions (approving the assignment approves them;" in text
    for line in lines:
        assert text.count(f"- {prose(line)}.") == 1, line
