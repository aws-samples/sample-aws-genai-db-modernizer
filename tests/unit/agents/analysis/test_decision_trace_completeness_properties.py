"""
Property 2: Decision trace completeness

For any collector output processed by the analysis agent, the decision trace
shall contain: one PK classification entry per table, one entry per identified
aggregate, one entry per GSI candidate, one entry per denormalization
opportunity, one entry per secondary-index-dominant table, and an llm_advisor
section recording the advisor status (invoked/skipped/failed).

Feature: enhanced-dynamodb-analysis, Property 2: Decision trace completeness
Validates: Requirements 1.5, 2.5, 3.6, 4.6, 5.3, 7.4
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_col_name = st.from_regex(r"[a-z][a-z0-9_]{1,9}", fullmatch=True)
_table_name = st.from_regex(r"[a-z_]{3,10}", fullmatch=True)


def _table_strategy(
    pk_size: st.SearchStrategy[int] = st.integers(min_value=1, max_value=3),
) -> st.SearchStrategy[dict]:
    """Generate a table dict with a configurable PK column count."""
    return st.builds(
        lambda schema, name, pk_cols, row_count: {
            "table_id": f"{schema}.{name}",
            "table_name": name,
            "row_count": row_count,
            "size_mb": 1.0,
            "columns": [
                {
                    "column_name": c,
                    "ordinal_position": i + 1,
                    "data_type": "varchar",
                    "nullable": False,
                }
                for i, c in enumerate(pk_cols)
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": pk_cols[:1] if pk_cols else [],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                }
            ],
            "primary_key": pk_cols,
        },
        schema=st.just("app"),
        name=_table_name,
        pk_cols=pk_size.flatmap(lambda n: st.lists(_col_name, min_size=n, max_size=n, unique=True)),
        row_count=st.integers(min_value=100, max_value=100_000),
    )


def _collector_output_strategy() -> st.SearchStrategy[dict]:
    """Generate a collector output with 1-4 tables and optional queries."""
    return (
        st.lists(_table_strategy(), min_size=1, max_size=4, unique_by=lambda t: t["table_id"])
        .flatmap(
            lambda tables: st.tuples(
                st.just(tables),
                st.lists(
                    st.builds(
                        lambda qid, freq, tbl: {
                            "query_id": qid,
                            "query_text": f"SELECT * FROM {tbl['table_name']}",  # nosec B608 — test fixture, not executed
                            "query_type": "SELECT",
                            "frequency_per_hour": freq,
                            "calls_per_second": freq / 3600.0,
                            "tables_accessed": [tbl["table_id"]],
                            "rows_returned_avg": 1.0,
                            "filter_columns": tbl["primary_key"][:1] if tbl["primary_key"] else [],
                        },
                        qid=st.from_regex(r"q-[0-9]{3}", fullmatch=True),
                        freq=st.floats(min_value=1.0, max_value=5000.0),
                        tbl=st.sampled_from(tables),
                    ),
                    min_size=0,
                    max_size=5,
                    unique_by=lambda q: q["query_id"],
                ),
            )
        )
        .map(
            lambda pair: {
                "job_id": "trace-test",
                "database_schema": {"tables": pair[0]},
                "queries": {"query_patterns": pair[1]},
            }
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_agent(collector_output: dict) -> dict:
    """Run the DynamoDB agent and return the decision trace.

    LLM advisor is explicitly disabled to avoid real Bedrock calls in tests.
    """
    with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
        inp = AnalysisInput(
            job_id=collector_output.get("job_id", "test"),
            collector_output=collector_output,
            target_database=TargetDatabase.dynamodb,
        )
        _, trace, _ = analyze_for_dynamodb(inp)
        return trace


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestDecisionTraceCompleteness:
    """Property 2: Decision trace completeness."""

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_trace_version_is_1_1(self, data: dict):
        """Trace version shall be 1.1 after enhancement."""
        trace = _run_agent(data)
        assert trace["trace_version"] == "1.1"

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_pk_classification_entry_per_table(self, data: dict):
        """Trace shall contain one PK classification entry per table."""
        trace = _run_agent(data)
        tables = data["database_schema"]["tables"]
        pk_entries = trace["pk_classifications"]

        assert len(pk_entries) == len(tables)
        pk_table_ids = {e["table_id"] for e in pk_entries}
        for t in tables:
            assert t["table_id"] in pk_table_ids

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_aggregates_section_present(self, data: dict):
        """Trace shall contain an aggregates section (list)."""
        trace = _run_agent(data)
        assert "aggregates" in trace
        assert isinstance(trace["aggregates"], list)

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_gsi_candidates_section_present(self, data: dict):
        """Trace shall contain a gsi_candidates section (list)."""
        trace = _run_agent(data)
        assert "gsi_candidates" in trace
        assert isinstance(trace["gsi_candidates"], list)

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_denormalization_opportunities_section_present(self, data: dict):
        """Trace shall contain a denormalization_opportunities section (list)."""
        trace = _run_agent(data)
        assert "denormalization_opportunities" in trace
        assert isinstance(trace["denormalization_opportunities"], list)

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_secondary_index_dominance_section_present(self, data: dict):
        """Trace shall contain a secondary_index_dominance section (list)."""
        trace = _run_agent(data)
        assert "secondary_index_dominance" in trace
        assert isinstance(trace["secondary_index_dominance"], list)

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_llm_advisor_section_present_with_status(self, data: dict):
        """Trace shall contain an llm_advisor section with status field."""
        trace = _run_agent(data)
        assert "llm_advisor" in trace
        llm = trace["llm_advisor"]
        assert "status" in llm
        assert "duration_seconds" in llm
        assert "attempts" in llm
        # LLM is disabled by default in tests
        assert llm["status"] in ("skipped", "success") or llm["status"].startswith("failed_after_")

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_llm_skipped_by_default(self, data: dict):
        """When ENABLE_LLM_ADVISOR is not set, status shall be 'skipped'."""
        trace = _run_agent(data)
        assert trace["llm_advisor"]["status"] == "skipped"
        assert trace["llm_advisor"]["attempts"] == 0
        assert trace["llm_advisor"]["duration_seconds"] == 0.0

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_all_v1_0_sections_still_present(self, data: dict):
        """v1.0 trace sections shall still be present in v1.1."""
        trace = _run_agent(data)
        for key in (
            "agent",
            "summary",
            "query_matches",
            "pattern_summaries",
            "recommendation_derivations",
            "table_groups",
        ):
            assert key in trace, f"Missing v1.0 section: {key}"
