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
        assert (
            "kept relational by aggregation and multi-table joins (900 of all 1283 routed)" in text
        )

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
        assert "kept relational by a utility or DDL statement (1 of all 1 routed)" in text

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


class TestAUtilityPinHasItsOwnNote:
    """#1373: "pinned" names the utility pin only. A query the assignment marks
    ``pinned`` is left out of the mean and of the capability lead, and the note
    after the lead counts it; the lead's count is out of every routed query."""

    def test_the_lead_leaves_pins_out_and_the_note_counts_them(self) -> None:
        joins = [
            _qa(
                f"q{i}",
                "aurora_mysql",
                "co-dependency group → aurora_mysql | [capability] dynamodb lacks "
                "required capability: complex_joins",
            )
            for i in range(3)
        ]
        pins = [
            {
                **_qa(
                    f"u{i}",
                    "aurora_mysql",
                    "utility/metadata statement, kept on the source-compatible relational "
                    "engine (Aurora MySQL)",
                ),
                "pinned": "utility",
            }
            for i in range(2)
        ]
        data = _data(joins + pins)
        entry = {**_ranking_entry("aurora_mysql", 3, "key_value_lookups", 1)}
        entry["routed_pinned_queries"] = 2
        text = _engine_rationale(data, entry)
        assert "across 3 queries" in text
        assert "kept relational by multi-table joins (3 of all 5 routed)" in text
        assert "utility or DDL" not in text
        assert "; 2 more queries are pinned (utility), not in the mean" in text
        assert text.count("pinned") == 1

    def test_an_engine_with_only_pins_says_so_not_an_average(self) -> None:
        pins = [
            {**_qa(f"u{i}", "aurora_mysql", "utility/metadata statement"), "pinned": "utility"}
            for i in range(3)
        ]
        entry = {
            **_ranking_entry("aurora_mysql", 0, None, 0),
            "routed_confidence": None,
            "routed_pinned_queries": 3,
            "assigned_queries": 3,
            "confidence_score": 64,
            "tables_analyzed": 9,
        }
        text = _engine_rationale(_data(pins), entry)
        assert text.startswith("3 queries routed, all are pinned (utility)")
        assert "64%" not in text and "average fit" not in text
