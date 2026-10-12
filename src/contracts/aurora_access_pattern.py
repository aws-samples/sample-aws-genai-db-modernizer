"""Shared Aurora access-pattern contract (#157).

Identical for both Aurora dialects — nothing about this model is MySQL- or
PostgreSQL-specific — so it is defined once here and imported by both
``aurora_mysql_model_output.py`` and ``aurora_postgresql_model_output.py``
rather than duplicated.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AccessPattern(BaseModel):
    """A query the Aurora target must serve, derived from the observed query log.

    Script-derived, not LLM-authored: the schema-design merge
    (``src/tools/schema/aurora_common/delta_merge.py``) derives these from
    the draft next to ``generated_ddl``, the same way, and for the same
    reason, that field is derived rather than accepted from the model's
    delta. ``design_rps`` is the collector's measured ``calls_per_second``;
    the source latency and DB-load percentages travel with the pattern so a
    load test can report a per-query before/after rather than only an
    aggregate. ``index_used`` is the single judgment field — see
    ``aurora_common/access_patterns.py``.
    """

    pattern_id: str = Field(..., description="Stable id — the source query_id")
    description: str = Field(..., description="Human-readable summary of the pattern")
    operation: str = Field(..., description="SELECT / INSERT / UPDATE / DELETE / MERGE / OTHER")
    sql_text: str | None = Field(None, description="Source SQL; runs as-is on a carry_over target")
    source_tables: list[str] = Field(
        default_factory=list, description="Tables the query touches, from the collector"
    )
    source_query_ids: list[str] = Field(
        default_factory=list, description="Collector query_ids this pattern came from"
    )
    filter_columns: list[str] = Field(default_factory=list, description="Predicate columns")
    sort_columns: list[str] = Field(default_factory=list, description="ORDER BY columns")

    index_used: str = Field(
        ...,
        description=(
            "Index expected to serve this pattern, by leftmost-prefix; "
            "'__seq_scan__' when no index leads with a filtered column, or when "
            "the predicate itself is unknown"
        ),
    )
    design_rps: float = Field(
        ..., ge=0, description="Measured calls_per_second — never invented; 0.0 when unmeasured"
    )
    source_latency_ms_p95: float | None = Field(
        None, description="Source p95 latency, for the load test's before/after comparison"
    )
    source_db_load_pct: float | None = Field(
        None, description="Share of source DB load, for prioritising which patterns matter"
    )

    script_derived: bool = Field(
        ..., description="True if the serving index was resolved deterministically"
    )
    needs_judgment: bool = Field(
        default=False, description="True if index selection was ambiguous and needs the LLM"
    )
    judgment_reason: str | None = Field(
        None, description="Why judgment is needed, or why a sequential scan is expected"
    )
    in_scope: bool = Field(
        default=True,
        description=(
            "Mirrors the assignment scope only: false when none of this query's "
            "tables are designed for this target (assigned to another engine, or "
            "missing from the collector). Does not reflect load-testability — see "
            "load_testable for that."
        ),
    )
    out_of_scope_reason: str | None = Field(
        None, description="Why the pattern is out of scope, when in_scope=false"
    )
    load_testable: bool = Field(
        default=True,
        description=(
            "False when this pattern cannot drive a load test — no measured call "
            "rate in the query log, or the pattern is out of scope. Independent of "
            "in_scope: a pattern can be in scope with nothing to measure it by."
        ),
    )
    load_testable_reason: str | None = Field(
        None, description="Why the pattern is not load-testable, when load_testable=false"
    )

    model_config = ConfigDict(extra="ignore")
