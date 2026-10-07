"""Aurora's "Why" cell leads with the reason its queries stay relational, not with
the routed workload signal (#393).

Evidence (judged Discourse run, #375 review): the Aurora PostgreSQL "Why" cell read
"led by key-value lookups (559 of 1283)" right next to the rest of the report
framing Aurora as the relational core kept for joins and aggregation -- the leading
*workload* signal is often key-value lookups riding along in the same
co-dependency group as the joins/aggregation/utility statements that actually pin
the table to a relational engine, not the reason the table is relational at all.
``_capability_pin_reason`` and the resolved-risk pin text already read a query's own
``assignment_reason`` for that reason (``[capability] <engine> lacks required
capability: ...`` / ``utility/metadata statement``); this reuses it for the "Why"
cell too, so the two can never contradict each other.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import SynthesisData
from src.agents.referee.synthesis_report import _engine_rationale


def _qa(query_id: str, engine: str, reason: str) -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "in_scope": True,
        "assignment_reason": reason,
    }


def _ranking_entry(target: str, n: int, lead: str, lead_count: int) -> dict:
    return {
        "target": target,
        "routed_confidence": 93,
        "routed_queries": n,
        "routed_tables": 2,
        "routed_confidence_evidence": "table",
        "routed_queries_without_table_evidence": 0,
        "routed_lead": lead,
        "routed_lead_count": lead_count,
        "target_tables": 0,
        "monthly_cost_usd": 0,
    }


def _data(query_assignments: list[dict]) -> SynthesisData:
    return SynthesisData(
        job_id="j", database_name="db", assignment={"query_assignments": query_assignments}
    )


class TestAuroraRationaleLeadsWithTheRelationalReason:
    def test_capability_pin_replaces_the_workload_signal(self) -> None:
        """Evidence shape: 1283 queries routed to aurora_postgresql, 900 of them
        pinned by a hard capability (joins/aggregation), the rest riding along --
        the "Why" cell leads with the pin, not with "key-value lookups (559 of
        1283)"."""
        pinned = [
            _qa(
                f"q{i}",
                "aurora_postgresql",
                "co-dependency group → aurora_postgresql | [capability] dynamodb lacks "
                "required capability: aggregation, complex_joins",
            )
            for i in range(900)
        ]
        riding_along = [
            _qa(f"q{i}", "aurora_postgresql", "highest confidence for aurora_postgresql")
            for i in range(900, 1283)
        ]
        data = _data(pinned + riding_along)
        entry = _ranking_entry("aurora_postgresql", 1283, "key_value_lookups", 559)
        text = _engine_rationale(data, entry)
        assert "led by key-value lookups" not in text
        assert "pinned by aggregation and multi-table joins (900 of 1283)" in text

    def test_utility_statement_pin_is_named_too(self) -> None:
        data = _data(
            [
                _qa(
                    "q1",
                    "aurora_mysql",
                    "utility/metadata statement — kept on source-compatible relational "
                    "engine (aurora_mysql) | [capability] dynamodb lacks required capability: "
                    "sql_admin",
                )
            ]
        )
        entry = _ranking_entry("aurora_mysql", 1, "key_value_lookups", 1)
        text = _engine_rationale(data, entry)
        assert "pinned by a utility or DDL statement (1 of 1)" in text

    def test_no_pin_falls_back_to_the_workload_signal(self) -> None:
        """An Aurora engine with no capability-pinned query at all (e.g. a 1:1
        carry-over not yet scored by the capability gate) keeps today's "led by"
        phrasing instead of inventing a reason that is not actually there."""
        data = _data([_qa("q1", "aurora_mysql", "highest confidence for aurora_mysql")])
        entry = _ranking_entry("aurora_mysql", 1, "aggregations", 1)
        text = _engine_rationale(data, entry)
        assert "led by aggregations (1 of 1)" in text

    def test_non_aurora_engine_is_unaffected(self) -> None:
        """DynamoDB's own "Why" cell still leads with its workload signal -- the
        capability-pin framing is specific to an Aurora engine staying relational."""
        data = _data([_qa("q1", "dynamodb", "highest confidence for dynamodb")])
        entry = _ranking_entry("dynamodb", 1, "key_value_lookups", 1)
        text = _engine_rationale(data, entry)
        assert "led by key-value lookups (1 of 1)" in text
