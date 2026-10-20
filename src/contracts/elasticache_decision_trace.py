"""ElastiCache decision trace contract (``analysis-elasticache/decision-trace.json``).

The ElastiCache analysis agent writes this trace beside ``analysis.json``. It
used to be a plain dict built inline in
``src/agents/analysis/elasticache_analysis_agent.py``; this model types it as it
is, and its ``contract_version`` is checked on read like every other stored
contract (ADR-008 Decisions 1 and 4).

It does not extend ``DecisionTraceBase``: it has no ``trace_version``,
``summary``, ``query_matches``, ``pattern_summaries`` or
``recommendation_derivations``, only run counts and the timestamp-column
reading (#935, the shared ``TimestampColumnsTrace``). It is listed in the
base guard's exemption set. Adding the shared sections is approved as a later
MINOR with optional fields (#1276), not part of typing it.

Version History:
- 1.0 (2026-10-20, #1191): Initial version, the six keys the trace already had
  plus ``contract_version``. A file without ``contract_version`` was written
  before this contract, and still loads; so does one from before #935, with no
  ``timestamp_columns``. Every model keeps unknown keys (``extra="allow"``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.contracts.decision_trace_base import KEEP_UNKNOWN, TimestampColumnsTrace, is_none


class ElastiCacheDecisionTrace(BaseModel):
    """``analysis-elasticache/decision-trace.json``: what the ElastiCache analysis found."""

    model_config = KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    agent: str = "elasticache-analysis-agent"
    patterns_detected: int
    anti_patterns_detected: int
    tables_analyzed: int
    recommendations_generated: int
    # How the timestamp-column fact was read (#935): by type, or by name and why. A
    # trace saved before #935 has none, so it is never written as null.
    timestamp_columns: TimestampColumnsTrace | None = Field(default=None, exclude_if=is_none)
