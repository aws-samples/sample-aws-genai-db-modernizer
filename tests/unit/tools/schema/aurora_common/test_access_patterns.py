"""Tests for deterministic Aurora access-pattern derivation.

Two properties matter here and are tested separately.

Derivation must be a *projection*, not a design: ``design_rps`` is the
collector's measured ``calls_per_second`` and is never invented, and
``index_used`` is resolved only when the leftmost-prefix rule gives an
unambiguous answer. Anything else becomes a typed residual (ADR-028).

Provenance holds *structurally* rather than by prompt: the designer returns an
``AuroraDesignDeltaContract``, which declares no access patterns at all, and the
merge derives them from the draft. There is no path by which a model can
re-author ``design_rps`` — the #136 failure mode is absent by construction, not
defended against.
"""

from __future__ import annotations

import pytest

from src.contracts.schema_design_input import (
    AgentColumn,
    AgentIndex,
    AgentQueryPattern,
    AgentTable,
    NormalizedDataType,
    QueryType,
)
from src.tools.schema.aurora_common.access_patterns import (
    NO_INDEX,
    derive_access_patterns,
)


def _col(name: str) -> AgentColumn:
    return AgentColumn(
        column_name=name,
        nullable=True,
        normalized_data_type=NormalizedDataType.integer,
    )


def _orders() -> AgentTable:
    """A table with a PK, a compound index and a single-column index."""
    return AgentTable(
        table_id="public.orders",
        table_name="orders",
        row_count=1000,
        columns=[_col("id"), _col("customer_id"), _col("status"), _col("created_at")],
        primary_key=["id"],
        indexes=[
            AgentIndex(
                index_name="idx_cust_created",
                columns=["customer_id", "created_at"],
                is_unique=False,
            ),
            AgentIndex(index_name="idx_status", columns=["status"], is_unique=False),
        ],
    )


def _query(
    query_id: str,
    filter_columns: list[str],
    *,
    calls_per_second: float | None = 5.0,
    table: str = "orders",
    p95: float | None = 12.0,
    db_load: float | None = 14.0,
    sort_columns: list[str] | None = None,
) -> AgentQueryPattern:
    return AgentQueryPattern(
        query_id=query_id,
        query_text=f"SELECT * FROM {table} -- {query_id}",
        query_type=QueryType.SELECT,
        frequency_per_hour=(calls_per_second or 0) * 3600,
        calls_per_second=calls_per_second,
        tables_accessed=[table],
        filter_columns=filter_columns,
        sort_columns=sort_columns or [],
        execution_time_ms_p95=p95,
        db_load_contribution_percent=db_load,
    )


def _only(queries: list[AgentQueryPattern], tables: list[AgentTable]) -> dict:
    result = derive_access_patterns(queries, tables)
    assert len(result.patterns) == 1
    return result.patterns[0]


# ---------------------------------------------------------------------------
# Index resolution — the leftmost-prefix rule
# ---------------------------------------------------------------------------


def test_primary_key_lookup_resolves_deterministically():
    """The commonest relational pattern must not reach the LLM.

    The collector does not always list the PK among a table's indexes, so the
    PK is injected as a synthetic candidate; without that, most point reads
    would come back ambiguous.
    """
    pattern = _only([_query("q", ["id"])], [_orders()])

    assert pattern["index_used"] == "PRIMARY"
    assert pattern["script_derived"] is True
    assert pattern["needs_judgment"] is False


def test_compound_index_matches_on_a_leading_prefix():
    pattern = _only([_query("q", ["customer_id", "created_at"])], [_orders()])

    assert pattern["index_used"] == "idx_cust_created"
    assert pattern["script_derived"] is True


def test_filter_on_a_non_leading_column_does_not_match():
    """A B-tree on (a, b) cannot serve a predicate on b alone.

    This is the case a naive "do the filter columns intersect the index
    columns?" check gets wrong, and getting it wrong invents an index that
    cannot serve the query.
    """
    result = derive_access_patterns([_query("q", ["created_at"])], [_orders()])
    pattern = result.patterns[0]

    assert pattern["index_used"] == NO_INDEX
    assert pattern["needs_judgment"] is True
    assert len(result.residuals) == 1
    assert result.residuals[0]["pattern_id"] == "q"


def test_ambiguous_prefix_is_handed_over_rather_than_guessed():
    """Two non-unique indexes on the same prefix is a genuine tie.

    The tiebreak a human applies is selectivity, which is not in the contract,
    so the residual names every candidate instead of picking one.
    """
    table = AgentTable(
        table_id="t",
        table_name="users",
        row_count=10,
        columns=[_col("email"), _col("tenant")],
        indexes=[
            AgentIndex(index_name="idx_a", columns=["email"], is_unique=False),
            AgentIndex(index_name="idx_b", columns=["email", "tenant"], is_unique=False),
        ],
    )
    result = derive_access_patterns([_query("q", ["email"], table="users")], [table])
    pattern = result.patterns[0]

    assert pattern["needs_judgment"] is True
    assert "idx_a" in pattern["judgment_reason"]
    assert "idx_b" in pattern["judgment_reason"]
    assert len(result.residuals) == 1


def test_uniqueness_breaks_a_same_prefix_tie():
    """A unique index on the same prefix is strictly more selective."""
    table = AgentTable(
        table_id="t",
        table_name="accts",
        row_count=10,
        columns=[_col("code")],
        indexes=[
            AgentIndex(index_name="idx_plain", columns=["code"], is_unique=False),
            AgentIndex(index_name="idx_uq", columns=["code"], is_unique=True),
        ],
    )
    pattern = _only([_query("q", ["code"], table="accts")], [table])

    assert pattern["index_used"] == "idx_uq"
    assert pattern["script_derived"] is True


def test_quoted_and_mixed_case_identifiers_still_match():
    """Folding is for comparison only; the emitted name keeps its source form."""
    table = AgentTable(
        table_id="t",
        table_name="Mixed",
        row_count=10,
        columns=[_col("Id")],
        primary_key=['"Id"'],
    )
    pattern = _only([_query("q", ["id"], table="Mixed")], [table])

    assert pattern["index_used"] == "PRIMARY"
    assert pattern["script_derived"] is True


# ---------------------------------------------------------------------------
# The two cases that are deliberately NOT missing-index findings
# ---------------------------------------------------------------------------


def test_unfiltered_aggregate_is_not_a_missing_index():
    """``SELECT count(*)`` is correctly served by a full scan.

    Flagging it would ask the designer to "confirm whether an index should be
    added" for a query where the answer is obviously no.
    """
    result = derive_access_patterns([_query("q", [])], [_orders()])
    pattern = result.patterns[0]

    assert pattern["index_used"] == NO_INDEX
    assert pattern["needs_judgment"] is False
    assert pattern["script_derived"] is True
    assert result.residuals == []


def test_query_on_a_table_assigned_elsewhere_is_out_of_scope():
    """Assignment splits a source across engines, so this input is expected.

    Without this branch every DynamoDB- or DocumentDB-assigned query returns
    here as an "add an index" recommendation.
    """
    result = derive_access_patterns([_query("q", ["email"], table="ghost")], [_orders()])
    pattern = result.patterns[0]

    assert pattern["in_scope"] is False
    assert pattern["out_of_scope_reason"]
    assert pattern["needs_judgment"] is False
    assert result.residuals == []


# ---------------------------------------------------------------------------
# design_rps is measured, never invented
# ---------------------------------------------------------------------------


def test_design_rps_comes_from_the_measured_call_rate():
    pattern = _only([_query("q", ["id"], calls_per_second=42.5)], [_orders()])

    assert pattern["design_rps"] == pytest.approx(42.5)
    assert pattern["in_scope"] is True


def test_design_rps_falls_back_to_hourly_frequency():
    query = _query("q", ["id"], calls_per_second=None)
    query.frequency_per_hour = 7200.0
    pattern = _only([query], [_orders()])

    assert pattern["design_rps"] == pytest.approx(2.0)


def test_unmeasured_rate_is_out_of_scope_rather_than_assumed():
    """A pattern with no measured rate cannot drive a load test.

    Reporting 0.0 and ``in_scope=False`` is honest; substituting a plausible
    default would produce a load test calibrated to a number nobody measured.
    """
    query = _query("q", ["id"], calls_per_second=None)
    query.frequency_per_hour = 0.0
    pattern = _only([query], [_orders()])

    assert pattern["design_rps"] == 0.0
    assert pattern["in_scope"] is False
    assert pattern["out_of_scope_reason"]


def test_source_baseline_travels_with_the_pattern():
    """The before-number for the load test's before/after comparison."""
    pattern = _only([_query("q", ["id"], p95=31.0, db_load=22.0)], [_orders()])

    assert pattern["source_latency_ms_p95"] == pytest.approx(31.0)
    assert pattern["source_db_load_pct"] == pytest.approx(22.0)


def test_derivation_is_deterministic():
    queries = [_query("a", ["id"]), _query("b", ["status"]), _query("c", ["created_at"])]
    tables = [_orders()]

    assert derive_access_patterns(queries, tables).patterns == (
        derive_access_patterns(queries, tables).patterns
    )


def test_empty_query_log_yields_no_patterns():
    result = derive_access_patterns([], [_orders()])

    assert result.patterns == []
    assert result.residuals == []
