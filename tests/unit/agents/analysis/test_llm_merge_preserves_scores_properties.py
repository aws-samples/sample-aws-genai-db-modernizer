"""
Property 10: LLM merge preserves deterministic scores

For any collector output, the deterministic scores (confidence_score,
score_breakdown) on every TableRecommendation shall be identical whether
the LLM advisor is enabled or disabled. LLM recommendations are additive only.

Feature: enhanced-dynamodb-analysis, Property 10: LLM merge preserves deterministic scores
Validates: Requirements 7.2
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.tools.analysis.dynamodb_analysis_tools import (
    AggregateKeyDesign,
    DenormStrategy,
    LlmAdvisorOutput,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,10}", fullmatch=True)


@st.composite
def _table_strategy(draw: st.DrawFn) -> dict:
    """Generate a single table dict with 1-2 PK columns and basic structure."""
    tid = draw(_table_id)
    tname = tid.split(".")[-1]
    pk_count = draw(st.integers(min_value=1, max_value=2))
    pk_cols = draw(st.lists(_column_name, min_size=pk_count, max_size=pk_count, unique=True))
    extra_cols = draw(
        st.lists(_column_name, min_size=0, max_size=3, unique=True).filter(
            lambda cols: not set(cols) & set(pk_cols)
        )
    )
    all_cols = pk_cols + extra_cols

    columns = [
        {
            "column_name": c,
            "ordinal_position": i + 1,
            "data_type": "varchar",
            "nullable": i >= pk_count,
        }
        for i, c in enumerate(all_cols)
    ]

    return {
        "table_id": tid,
        "table_name": tname,
        "row_count": draw(st.integers(min_value=100, max_value=100_000)),
        "size_mb": draw(st.floats(min_value=0.1, max_value=1000.0)),
        "columns": columns,
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": pk_cols,
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        ],
        "primary_key": pk_cols,
        "foreign_keys": [],
    }


@st.composite
def _query_strategy(draw: st.DrawFn, table_ids: list[str]) -> dict:
    """Generate a query dict referencing one of the given table_ids."""
    if not table_ids:
        return {}
    tid = draw(st.sampled_from(table_ids))
    query_type = draw(st.sampled_from(["SELECT", "INSERT", "UPDATE"]))
    freq = draw(st.floats(min_value=0.1, max_value=100.0))
    filter_col = draw(_column_name)

    return {
        "query_id": draw(st.from_regex(r"q-[0-9]{3}", fullmatch=True)),
        "query_text": f"{query_type} ... FROM {tid.split('.')[-1]} WHERE {filter_col} = ?",
        "query_type": query_type,
        "frequency_per_hour": freq * 3600,
        "calls_per_second": freq,
        "tables_accessed": [tid],
        "rows_returned_avg": draw(st.floats(min_value=1.0, max_value=100.0)),
        "filter_columns": [filter_col] if query_type == "SELECT" else [],
        "execution_time_ms_avg": draw(st.floats(min_value=0.1, max_value=50.0)),
    }


@st.composite
def _collector_output_strategy(draw: st.DrawFn) -> dict:
    """Generate a valid collector output with 1-3 tables and 0-5 queries."""
    tables = draw(
        st.lists(_table_strategy(), min_size=1, max_size=3, unique_by=lambda t: t["table_id"])
    )
    table_ids = [t["table_id"] for t in tables]
    queries = draw(
        st.lists(_query_strategy(table_ids), min_size=0, max_size=5).filter(
            lambda qs: all(q for q in qs)
        )
    )

    return {
        "job_id": "test-job",
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


def _make_mock_llm_output() -> LlmAdvisorOutput:
    """Create a valid LlmAdvisorOutput for mocking."""
    return LlmAdvisorOutput(
        aggregate_recommendations=[
            AggregateKeyDesign(
                aggregate_id="agg-test",
                partition_key="id",
                sort_key=None,
                rationale="Test recommendation",
            )
        ],
        denormalization_strategies=[
            DenormStrategy(
                opportunity_id="denorm-test",
                strategy="embed child items",
                rationale="Test strategy",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


class TestLlmMergePreservesDeterministicScores:
    """Property 10: LLM merge preserves deterministic scores.

    **Validates: Requirements 7.2**
    """

    @given(data=_collector_output_strategy())
    @settings(max_examples=100)
    def test_scores_identical_with_and_without_llm(self, data: dict):
        """Deterministic scores are identical whether LLM is enabled or disabled.

        **Validates: Requirements 7.2**
        """
        # Run 1: LLM disabled
        inp_disabled = AnalysisInput(
            job_id=data["job_id"],
            collector_output=data,
            target_database=TargetDatabase.dynamodb,
        )
        with patch.dict("os.environ", {"ENABLE_LLM_ADVISOR": "false"}):
            result_disabled, _, _ = analyze_for_dynamodb(inp_disabled)

        # Run 2: LLM enabled but mocked (mock the LlmAdvisor to return output)
        # Since Task 10 hasn't wired LLM into orchestration yet, we patch
        # LlmAdvisor at the module level to simulate what will happen when
        # the orchestration calls the advisor.
        mock_output = _make_mock_llm_output()

        inp_enabled = AnalysisInput(
            job_id=data["job_id"],
            collector_output=data,
            target_database=TargetDatabase.dynamodb,
        )

        with patch("src.agents.analysis.dynamodb_analysis_agent.LlmAdvisor") as MockAdvisorClass:
            mock_advisor = MockAdvisorClass.return_value
            mock_advisor.enabled = True
            mock_advisor.advise.return_value = mock_output
            mock_advisor.attempts_made = 1
            mock_advisor.MAX_RETRIES = 3

            result_enabled, _, _ = analyze_for_dynamodb(inp_enabled)

        # Assert: every TableRecommendation has identical deterministic scores
        recs_disabled = sorted(result_disabled.table_recommendations, key=lambda r: r.table_id)
        recs_enabled = sorted(result_enabled.table_recommendations, key=lambda r: r.table_id)

        assert len(recs_disabled) == len(recs_enabled), (
            f"Different number of recommendations: " f"{len(recs_disabled)} vs {len(recs_enabled)}"
        )

        for rec_d, rec_e in zip(recs_disabled, recs_enabled, strict=False):
            assert rec_d.table_id == rec_e.table_id

            # confidence_score must be identical
            assert rec_d.confidence_score == rec_e.confidence_score, (
                f"Table {rec_d.table_id}: confidence_score differs: "
                f"{rec_d.confidence_score} vs {rec_e.confidence_score}"
            )

            # score_breakdown must be identical
            assert (
                rec_d.score_breakdown.pattern_match_score
                == rec_e.score_breakdown.pattern_match_score
            ), (
                f"Table {rec_d.table_id}: pattern_match_score differs: "
                f"{rec_d.score_breakdown.pattern_match_score} vs "
                f"{rec_e.score_breakdown.pattern_match_score}"
            )
            assert (
                rec_d.score_breakdown.complexity_score == rec_e.score_breakdown.complexity_score
            ), f"Table {rec_d.table_id}: complexity_score differs"
            assert (
                rec_d.score_breakdown.performance_score == rec_e.score_breakdown.performance_score
            ), f"Table {rec_d.table_id}: performance_score differs"
            assert (
                rec_d.score_breakdown.cost_score == rec_e.score_breakdown.cost_score
            ), f"Table {rec_d.table_id}: cost_score differs"
