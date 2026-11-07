# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1989: synthesis estimates each hot-partition entry's load per key value once, from the
collection, and the risk and ``facts.hot_partitions`` read that one computation.

The design below reproduces the shape behind #1987: a table total at about three times
one key value's read limit, on a key with tens of thousands of distinct values (a unique
index), next to a write entry whose key has no collected distinct values.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.synthesis_output import SynthesisOutputContract
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-hot"

QUERIES = [
    {
        "query_id": q,
        "query_text": f"SELECT * FROM {t} WHERE k = ?",  # nosec B608 -- test fixture text
        "query_type": "SELECT" if q.startswith("r") else "UPDATE",
        "calls_per_second": 50.0,
        "rows_returned_avg": 1,
        "tables_accessed": [t],
    }
    for q, t in (("r1", "options"), ("w1", "meta"), ("r2", "orders"))
]

ASSIGNMENT = {
    "job_id": JOB,
    "version": 2,
    "query_assignments": [
        {"query_id": "r1", "assigned_engine": "dynamodb", "source_tables": ["options"]},
        {"query_id": "w1", "assigned_engine": "dynamodb", "source_tables": ["meta"]},
        {"query_id": "r2", "assigned_engine": "aurora_mysql", "source_tables": ["orders"]},
    ],
    "table_assignments": [
        {"table_id": t, "primary_engine": e, "engines": [e], "query_count": 1}
        for t, e in (("options", "dynamodb"), ("meta", "dynamodb"), ("orders", "aurora_mysql"))
    ],
}

TABLES = [
    {
        "table_id": "options",
        "table_name": "options",
        "row_count": 55_527,
        "primary_key": ["option_id"],
        "indexes": [{"index_name": "name", "columns": ["option_name"], "is_unique": True}],
        "columns": [{"column_name": "option_id"}, {"column_name": "option_name"}],
    },
    {
        "table_id": "meta",
        "table_name": "meta",
        "row_count": 900_000,
        "primary_key": ["meta_id"],
        "indexes": [{"index_name": "post", "columns": ["post_id"], "is_unique": False}],
        "columns": [{"column_name": "meta_id"}, {"column_name": "post_id"}],
    },
    {"table_id": "orders", "table_name": "orders", "row_count": 10, "columns": []},
]


def _table(name: str, source: str, column: str) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source],
        "partition_key": {"attribute_name": column, "attribute_type": "S"},
        "attributes": [
            {"name": column, "type": "S", "source_table": source, "source_column": column}
        ],
        "gsis": [],
        "item_count": 1,
        "item_size_bytes": 100,
    }


def _hot(table: str, op: str, units: float, qid: str) -> dict:
    limit = 3000.0 if op == "read" else 1000.0
    return {
        "table_name": table,
        "gsi_name": None,
        "operation": op,
        "rcu_or_wcu_per_second": units,
        "partition_limit": limit,
        "utilization_pct": min(100.0, round(units / limit * 100, 1)),  # as main capped it
        "at_risk": True,
        "contributing_patterns": [qid],
        "mitigation": "Spread over the key's values",
    }


DESIGN = {
    "target_engine": "dynamodb",
    "table_definitions": [
        _table("Options", "options", "option_name"),
        _table("Meta", "meta", "post_id"),
    ],
    "access_patterns": [],
    "hot_partition_analysis": [
        _hot("Options", "read", 9_087.95, "r1"),
        _hot("Meta", "write", 1_004.5, "w1"),
    ],
    "validation_passed": True,
}


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ["dynamodb", "aurora_mysql"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {"tables": TABLES},
            "queries": {"query_patterns": QUERIES},
        },
    )
    for engine in engines:
        s.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json",
            {
                "table_recommendations": [{"table_id": "options", "confidence_score": 80}],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                "cost_estimate": {"monthly_cost_usd": 10},
            },
        )
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT)
    s.write_json(f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json", DESIGN)
    return s


def _report(store) -> dict:
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    _write_synthesis_report(store, result, assignment_version=2)
    report: dict = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    return report


def test_the_facts_state_each_entrys_total_and_its_per_key_load(store) -> None:
    report = _report(store)
    SynthesisOutputContract.model_validate(report)
    rows = {r["table_name"]: r for r in report["facts"]["hot_partitions"]}
    options, meta = rows["Options"], rows["Meta"]
    assert options["total_pct_of_key_limit"] == pytest.approx(302.9317)  # not capped
    assert options["distinct_key_values"] == 55_527
    assert options["per_key_basis"] == "distinct_values"
    assert options["per_key_pct_of_limit"] == pytest.approx(0.0055, abs=1e-4)
    assert meta["per_key_basis"] == "not_measured"
    assert meta["per_key_units_per_second"] is None


def test_the_risk_is_low_and_reads_the_same_rows(store) -> None:
    report = _report(store)
    (risk,) = [r for r in report["risk_assessment"]["risks"] if r["rule"] == "design.hot_partition"]
    assert risk["severity"] == "LOW"
    text = risk["description"]
    # Per-key first (the maintainer's decision on #2001), then the total as an upper bound.
    assert (
        text.index("the busiest estimated partition key value is at 0.0055%")
        < text.index("an upper bound reached only if every request used the same key value")
        < text.index("the highest at 302.9%")
    )

    assert "Per-key load not measured for 1 of these tables" in risk["description"]
    assert len(risk["details"]) == len(report["facts"]["hot_partitions"]) == 2
    # The total never raised the overall level on its own.
    assert report["risk_assessment"]["overall_risk_level"] != "HIGH" or any(
        r["severity"] == "HIGH" and r["rule"] != "design.hot_partition"
        for r in report["risk_assessment"]["risks"]
    )


def test_the_executive_report_states_the_per_key_figure_before_the_total(store) -> None:
    from src.report import executive_report
    from tests.executive_pdf import body_text

    text = " ".join(
        body_text(executive_report.render_executive_report_pdf(_report(store)), DB).split()
    )
    assert text.index("busiest estimated partition key value is at 0.0055%") < text.index(
        "the highest at 302.9%"
    )
