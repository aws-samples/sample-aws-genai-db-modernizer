"""Retained engines must survive synthesis (defect (f)).

``recommended_architecture.databases`` used to admit an engine only when it had
net-new table mappings or a schema design. A retained relational core — an Aurora
baseline keeping the source schema — has neither by definition, so it fell out of
the list while still carrying workload and cost. The architecture then undercounted
the allocation and did not reconcile to ``tco_analysis.cost_breakdown`` (an ATX run
dropped ~69% of the allocation this way).

The ElastiCache half of (f) is covered in test_synthesis_elasticache_shapes.py.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    build_architecture_recommendation,
    build_ranking,
    build_table_mappings,
    build_tco_analysis,
)

_DDB_SCHEMA: dict[str, Any] = {
    "table_definitions": [
        {"table_name": "Orders", "source_tables": ["orders"], "aggregate_pattern": "separate"},
    ],
    "access_patterns": [{"pattern_id": "DDB-AP-1", "pattern_group": "order_lookup"}],
}

_DDB_ANALYSIS: dict[str, Any] = {
    "table_recommendations": [{"table_id": "orders", "confidence_score": 85}],
    "workload_analysis": {"patterns_detected": ["key_value"]},
    "cost_estimate": {"monthly_cost_usd": 120.0},
}

_AURORA_ANALYSIS: dict[str, Any] = {
    "table_recommendations": [
        {"table_id": "customers", "confidence_score": 90},
        {"table_id": "invoices", "confidence_score": 88},
    ],
    "workload_analysis": {"patterns_detected": ["relational_joins"]},
    "cost_estimate": {"monthly_cost_usd": 400.0},
}

_ASSIGNMENT: dict[str, Any] = {
    "query_assignments": [
        {"assigned_engine": "dynamodb", "assignment_reason": "key-value access", "in_scope": True},
        {"assigned_engine": "aurora_postgresql", "assignment_reason": "joins", "in_scope": True},
        {"assigned_engine": "aurora_postgresql", "assignment_reason": "joins", "in_scope": True},
    ],
    "table_assignments": [
        {"table_id": "orders", "primary_engine": "dynamodb"},
        {"table_id": "customers", "primary_engine": "aurora_postgresql"},
        {"table_id": "invoices", "primary_engine": "aurora_postgresql"},
    ],
}


def _data(
    assignment: dict | None = _ASSIGNMENT, aurora_schema: dict | None = None
) -> SynthesisData:
    return SynthesisData(
        job_id="job-f",
        database_name="billing",
        assignment=assignment,
        engines={
            "dynamodb": EngineArtifacts(
                engine="dynamodb", analysis=_DDB_ANALYSIS, schema_design=_DDB_SCHEMA
            ),
            # Same-family target: no migration design, schema carries over.
            "aurora_postgresql": EngineArtifacts(
                engine="aurora_postgresql",
                analysis=_AURORA_ANALYSIS,
                schema_design=aurora_schema if aurora_schema is not None else {},
            ),
        },
    )


def _architecture(data: SynthesisData) -> dict:
    return build_architecture_recommendation(data, build_ranking(data), build_table_mappings(data))


def _db(architecture: dict, engine: str) -> dict:
    return next(d for d in architecture["databases"] if d["service"] == engine)


class TestRetainedEngineIsListed:
    def test_retained_engine_with_workload_reaches_databases(self) -> None:
        architecture = _architecture(_data())

        assert {d["service"] for d in architecture["databases"]} == {
            "dynamodb",
            "aurora_postgresql",
        }
        assert architecture["architecture_type"] == "MULTI_DATABASE"

    def test_retained_engine_is_tagged_retained_not_migration_target(self) -> None:
        architecture = _architecture(_data())

        assert _db(architecture, "aurora_postgresql")["role"] == "retained"
        assert _db(architecture, "dynamodb")["role"] == "migration_target"

    def test_skipped_design_placeholder_still_reads_as_retained(self) -> None:
        """The schema designer writes a status-only placeholder when it skips."""
        architecture = _architecture(_data(aurora_schema={"status": "skipped"}))

        assert _db(architecture, "aurora_postgresql")["role"] == "retained"

    def test_retained_tables_come_from_the_assignment(self) -> None:
        """No table_mappings entry exists for a retained table, so table_count 0
        would read as an engine holding nothing."""
        retained = _db(_architecture(_data()), "aurora_postgresql")

        assert sorted(retained["tables"]) == ["customers", "invoices"]
        assert retained["table_count"] == 2

    def test_rationale_names_the_retained_engine(self) -> None:
        assert "aurora_postgresql" in _architecture(_data())["rationale"]


class TestReconcilesToCost:
    def test_databases_cost_sums_to_cost_breakdown(self) -> None:
        data = _data()
        architecture = _architecture(data)
        tco = build_tco_analysis(data)

        arch_total = sum(d["monthly_cost_usd"] for d in architecture["databases"])
        tco_total = sum(c["monthly_cost_usd"] for c in tco["cost_breakdown"])

        assert arch_total == tco_total == 520.0
        assert {d["service"] for d in architecture["databases"]} == {
            c["database"] for c in tco["cost_breakdown"]
        }


class TestNoOverInclusion:
    def test_without_an_assignment_an_undesigned_engine_stays_out(self) -> None:
        """No assignment means no workload signal, so there is no basis for
        calling the engine retained; the old gating holds."""
        architecture = _architecture(_data(assignment=None))

        assert {d["service"] for d in architecture["databases"]} == {"dynamodb"}

    def test_engine_with_zero_assigned_queries_stays_out(self) -> None:
        """load_synthesis_data normally drops these first; the builder must not
        re-admit one that slips through."""
        assignment = {
            **_ASSIGNMENT,
            "query_assignments": [
                qa for qa in _ASSIGNMENT["query_assignments"] if qa["assigned_engine"] == "dynamodb"
            ],
        }

        architecture = _architecture(_data(assignment=assignment))

        assert "aurora_postgresql" not in {d["service"] for d in architecture["databases"]}
