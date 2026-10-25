"""The gate block, the final summary and ``index.md`` state the engine scope (#1574, item 4).

A scoped job's deliverables named the scope, but the chat blocks ``/modernize`` quotes and
the deliverables folder's ``index.md`` did not, nor how many queries have no engine in the
scope. Each states both from the facts layer (``gate_block_facts`` for the gate, the
report's ``facts`` for the rest, ADR-032); a job with no scope prints neither line.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.report.deliverables import FOLDER_ENTRIES, render_folder_index
from src.report.printed_summary import render_final_summary, render_gate_block
from src.shared.gate_facts import gate_block_facts

SCOPE_LINE = "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)"
GATE: dict[str, Any] = {
    "before_distribution": {"aurora_postgresql": 900, "dynamodb": 700},
    "after_distribution": {"aurora_postgresql": 938, "dynamodb": 716},
    "eliminated_engines": [],
    "consolidations": [],
    "schema_design_engines": ["aurora_postgresql", "dynamodb"],
    "pending_choices": [],
}


def _gate(**changes: Any) -> str:
    return render_gate_block({**GATE, **changes}, database_name="discourse", job_id="job1")


def test_the_gate_facts_carry_the_scope_and_the_queries_with_no_engine() -> None:
    facts = gate_block_facts(
        {**GATE, "engine_scope": ["dynamodb", "aurora_postgresql"], "unservable_queries": 42}
    )
    assert facts["engine_scope"] == ["aurora_postgresql", "dynamodb"]
    assert facts["unservable_query_count"] == 42
    assert facts["query_total_count"] == 938 + 716 + 42
    plain = gate_block_facts(GATE)
    assert plain["engine_scope"] is None and plain["unservable_query_count"] == 0


def test_the_gate_block_states_the_scope_and_the_queries_with_no_engine() -> None:
    text = _gate(engine_scope=["aurora_postgresql", "dynamodb"], unservable_queries=42)
    assert f"{SCOPE_LINE}." in text
    assert (
        "42 of 1,696 queries have no engine in the customer's engine scope; a new assessment "
        "whose scope adds an engine can serve them." in text
    )
    # Stated after the engine table's total, before what runs next.
    assert text.index("Total: 1654 queries.") < text.index(SCOPE_LINE)
    assert text.index(SCOPE_LINE) < text.index("Schema design runs next for")


def test_one_query_with_no_engine_reads_in_the_singular() -> None:
    text = _gate(engine_scope=["aurora_postgresql", "dynamodb"], unservable_queries=1)
    assert "1 of 1,655 queries has no engine in the customer's engine scope" in text
    assert "can serve it." in text


@pytest.mark.passes_on_main  # the negative case: no scope, no line
def test_a_job_with_no_scope_prints_neither_line() -> None:
    text = _gate()
    assert "Engines in scope" not in text and "no engine in the customer's" not in text


def _report(**facts: Any) -> dict[str, Any]:
    return {
        "summary": "Analyzed.",
        "summary_deterministic": "Analyzed.",
        "summary_source": "deterministic",
        "ranking": [],
        "facts": {
            "projected_monthly_cost_usd": 168.23,
            "engine_count": 2,
            "migration_wave_count": 2,
            "open_risk_count": 0,
            "open_risks_by_severity": {},
            "engines": [],
            "retired_in_use_engines": [],
            **facts,
        },
        "tco_analysis": {"current_cost_known": False},
        "risk_assessment": {"risks": []},
    }


def _final(report: dict[str, Any]) -> str:
    return render_final_summary(
        report,
        database_name="discourse",
        job_id="job1",
        deliverables_dir="artifacts/discourse/job1/deliverables/",
        deliverables=FOLDER_ENTRIES,
    )


SCOPED = {
    "engine_scope": ["aurora_postgresql", "dynamodb"],
    "in_scope_query_count": 1654,
    "moving_query_count": 716,
    "no_engine_in_scope_query_count": 42,
    "query_total_count": 1696,
}


def test_the_final_summary_states_the_scope_and_the_queries_with_no_engine() -> None:
    text = _final(_report(**SCOPED))
    glance = text.split("### At a glance", 1)[1].split("### Engines", 1)[0]
    assert f"- {SCOPE_LINE}" in glance
    assert (
        "- 42 of 1,696 queries have no engine in the customer's engine scope; a new "
        "assessment whose scope adds an engine can serve them" in glance
    )


@pytest.mark.passes_on_main  # the negative case: no scope, no line
def test_the_final_summary_of_an_unscoped_job_has_neither_line() -> None:
    text = _final(
        _report(in_scope_query_count=1654, moving_query_count=716, query_total_count=1654)
    )
    assert "Engines in scope" not in text and "no engine in the customer's" not in text


def test_the_index_states_the_scope_and_the_queries_with_no_engine() -> None:
    index = render_folder_index("discourse", "job1", 2, FOLDER_ENTRIES, facts=SCOPED)
    head = index.split("| File |", 1)[0]
    assert f"{SCOPE_LINE}." in head
    assert "42 of 1,696 queries have no engine in the customer's engine scope" in head
    plain = render_folder_index("discourse", "job1", 2, FOLDER_ENTRIES)
    assert "Engines in scope" not in plain
