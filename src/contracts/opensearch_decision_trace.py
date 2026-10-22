"""OpenSearch decision trace contract (``analysis-opensearch/decision-trace.json``).

The OpenSearch analysis agent writes this trace beside ``analysis.json`` for
specialist calibration (ADR-017 Decision 3: it is not part of the
agent-to-agent contract). It used to be a plain dict, so a renamed field or a
new workload type passed every guard; this model types it on the shared
``DecisionTraceBase`` (#1187), and its ``contract_version`` is checked on read
like every other stored contract (ADR-008 Decisions 1 and 4).

The shared sections are narrowed where the OpenSearch trace writes more: the
summary's per-workload table counts, a query's ``signals``, a pattern's
``total_calls_per_second``, and a derivation's ``workload_type`` and
``final_recommendation``. The OpenSearch-only sections are
``workload_classifications`` and ``timestamp_columns``. The file keeps every
key it had; JSON objects are unordered, and the model writes the shared
sections first.

``trace_version`` is the trace's own calibration version from before this
contract (``"1.0"``); nothing checks it. ``contract_version`` is the version
readers check.

Version History:
- 1.0 (2026-10-21, #1189): Initial version, the sections the trace already had
  (``trace_version`` 1.0) plus ``contract_version``. A file without
  ``contract_version`` was written before this contract, and still loads.
  ``timestamp_columns`` (#935) is optional: a trace saved before it has none.
  Every model keeps unknown keys (``extra="allow"``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from src.contracts import decision_trace_base as base
from src.contracts.analysis_output import RecommendationLevel
from src.contracts.decision_trace_base import (
    KEEP_UNKNOWN,
    TimestampColumnsTrace,
    is_none,
)


class OpenSearchWorkloadType(str, Enum):
    """What a table is for the OpenSearch analysis: a search or time-series workload, or neither."""

    SEARCH = "SEARCH"
    TIMESERIES = "TIMESERIES"
    NOT_SUITABLE = "NOT_SUITABLE"


class TraceSummary(base.TraceSummary):
    """The shared counts, and how many tables fell into each workload type."""

    model_config = KEEP_UNKNOWN

    tables_search: int
    tables_timeseries: int
    tables_not_suitable: int


class QueryMatch(base.QueryMatch):
    """The patterns and anti-patterns one query matched, and the signals behind them."""

    model_config = KEEP_UNKNOWN

    signals: list[str]


class PatternSummary(base.PatternSummary):
    """One detected pattern, with the call rate of the queries that matched it."""

    model_config = KEEP_UNKNOWN

    total_calls_per_second: int | float


class RecommendationDerivation(base.RecommendationDerivation):
    """How one table's recommendation score was derived, and the workload type behind it."""

    model_config = KEEP_UNKNOWN

    workload_type: OpenSearchWorkloadType
    final_recommendation: RecommendationLevel


class TimeseriesCriteriaMet(BaseModel):
    """Which time-series criteria a table passed."""

    model_config = KEEP_UNKNOWN

    timestamp_range: bool
    high_ingest: bool
    staleness: bool


class WorkloadClassificationTrace(BaseModel):
    """How one table was classified as a search or time-series workload, and why."""

    model_config = KEEP_UNKNOWN

    table_id: str
    workload_type: OpenSearchWorkloadType
    reason: str
    search_pattern_count: int
    timeseries_criteria_met: TimeseriesCriteriaMet


class OpenSearchDecisionTrace(base.DecisionTraceBase):
    """``analysis-opensearch/decision-trace.json``: how the OpenSearch analysis reached its result."""

    model_config = KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        default="1.0", description="The trace's calibration version from before this contract"
    )
    agent: str = "opensearch-analysis-agent"
    summary: TraceSummary
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[RecommendationDerivation]
    workload_classifications: list[WorkloadClassificationTrace]
    # Written since #935; a trace saved before it has none, so it is never written as null.
    timestamp_columns: TimestampColumnsTrace | None = Field(default=None, exclude_if=is_none)
