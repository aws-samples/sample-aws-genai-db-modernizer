"""Access patterns are derived by the merge, not written by the model (#156).

Aurora is the only target whose access patterns can be *projected* rather than
designed: on carry_over the source statement runs as-is, so the measured
``calls_per_second`` is the pattern's RPS and the source latency travels with it
for a per-query before/after.

The property these tests protect is where that derivation happens. The delta
contract declares no access patterns at all, so the model has no channel to
author one and ``design_rps`` cannot be re-authored -- GitHub #136 is that same
field going wrong on the DocumentDB path, where the model does author it. That
is a structural absence rather than a prompt instruction, which is what
``test_the_delta_has_no_channel_to_author_access_patterns`` pins.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.schema_design_input import (
    AgentColumn,
    AgentIndex,
    AgentQueryPattern,
    AgentTable,
    NormalizedDataType,
    QueryType,
)
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

_EMPTY_DELTA = {"delta_version": "1.0"}


def _orders() -> AgentTable:
    return AgentTable(
        table_id="shop.orders",
        table_name="orders",
        row_count=5000,
        primary_key=["id"],
        columns=[
            AgentColumn(
                column_name="id",
                nullable=False,
                normalized_data_type=NormalizedDataType.integer,
            ),
            AgentColumn(
                column_name="status",
                nullable=True,
                normalized_data_type=NormalizedDataType.string,
                max_length=32,
            ),
        ],
        indexes=[AgentIndex(index_name="idx_orders_status", columns=["status"], is_unique=False)],
    )


def _query(
    qid: str,
    filters: list[str],
    *,
    calls_per_second: float = 4.5,
    p95: float | None = 17.5,
) -> AgentQueryPattern:
    return AgentQueryPattern(
        query_id=qid,
        query_text=f"SELECT * FROM orders WHERE {filters[0]} = ?",
        query_type=QueryType.SELECT,
        frequency_per_hour=calls_per_second * 3600,
        tables_accessed=["orders"],
        filter_columns=filters,
        calls_per_second=calls_per_second,
        execution_time_ms_p95=p95,
    )


def _base(queries: list[AgentQueryPattern]) -> AuroraDesignBase:
    return AuroraDesignBase(
        engine="aurora_postgresql",
        job_id="j",
        source_database="shop",
        source_engine="postgresql",
        tables=[_orders()],
        queries=queries,
    )


def test_the_merge_derives_access_patterns_from_the_observed_log():
    merged = merge_design_delta(_base([_query("q1", ["status"])]), _EMPTY_DELTA)

    assert merged.errors == []
    assert merged.output is not None
    patterns = merged.output["access_patterns"]
    assert [p["pattern_id"] for p in patterns] == ["q1"]
    assert patterns[0]["index_used"] == "idx_orders_status"
    assert patterns[0]["script_derived"] is True


def test_design_rps_is_the_measured_rate_not_an_estimate():
    """The #136 failure mode: an authored design_rps silently zeroes a load test."""
    merged = merge_design_delta(
        _base([_query("q1", ["status"], calls_per_second=7.25)]), _EMPTY_DELTA
    )

    assert merged.output is not None
    pattern = merged.output["access_patterns"][0]
    assert pattern["design_rps"] == pytest.approx(7.25)
    assert pattern["source_latency_ms_p95"] == pytest.approx(17.5)


def test_the_delta_has_no_channel_to_author_access_patterns():
    """Provenance is structural, not a prompt request.

    The delta contract is strict, so a model returning access patterns is a
    validation error rather than an accepted override. This is the test that
    would fail first if someone added the field to the delta.
    """
    with pytest.raises(ValidationError):
        AuroraDesignDeltaContract.model_validate(
            {"delta_version": "1.0", "access_patterns": [{"pattern_id": "q1"}]}
        )


def test_an_unresolvable_index_is_a_warning_not_a_merge_error():
    """A predicate no index leads with is reported, never guessed at."""
    merged = merge_design_delta(_base([_query("q1", ["shipped_at"])]), _EMPTY_DELTA)

    assert merged.errors == []
    assert merged.output is not None
    assert any("q1" in w for w in merged.warnings)


def test_an_empty_query_log_yields_no_patterns():
    """Back-compat: every construction site that omits queries keeps its output."""
    merged = merge_design_delta(_base([]), _EMPTY_DELTA)

    assert merged.errors == []
    assert merged.output is not None
    assert merged.output["access_patterns"] == []
