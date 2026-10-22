"""Aurora decision trace contract, for PostgreSQL and MySQL
(``analysis-aurora_postgresql/decision-trace.json``,
``analysis-aurora_mysql/decision-trace.json``).

The two Aurora analysis agents write a trace of the same shape beside
``analysis.json`` for specialist calibration (ADR-017 Decision 3: it is not part
of the agent-to-agent contract). It used to be a plain dict, so a renamed field
passed every guard; one model types both on the shared ``DecisionTraceBase``
(#1187), and its ``contract_version`` is checked on read like every other stored
contract (ADR-008 Decisions 1 and 4).

The shared sections are narrowed where the Aurora traces write more: a query's
SQL features and locking clause, a pattern's ``total_calls_per_second``, a
derivation's ``migration_complexity``, and the ``llm_advisor`` status. The one
difference between the engines is the name of the query's feature list,
``pg_features`` on PostgreSQL and ``mysql_features`` on MySQL: each trace has
exactly one of them. Engine-specific sections added later are MINOR additions.

``trace_version`` is the trace's own calibration version from before this
contract (``"1.0"``); nothing checks it. ``contract_version`` is the version
readers check.

Version History:
- 1.0 (2026-10-21, #1190): Initial version, the sections the traces already had
  (``trace_version`` 1.0) plus ``contract_version``. A file without
  ``contract_version`` was written before this contract, and still loads, including
  one from before ``locking_clause`` (#799), which has no such key. Every
  model keeps unknown keys (``extra="allow"``).
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from src.contracts import decision_trace_base as base
from src.contracts.analysis_output import MigrationComplexity
from src.contracts.decision_trace_base import KEEP_UNKNOWN, LlmAdvisorTrace, is_none


class QueryMatch(base.QueryMatch):
    """The patterns and anti-patterns one query matched, and the SQL features it uses."""

    model_config = KEEP_UNKNOWN

    # PostgreSQL traces write ``pg_features``, MySQL traces ``mysql_features``; the other
    # is never written as null.
    pg_features: list[str] | None = Field(default=None, exclude_if=is_none)
    mysql_features: list[str] | None = Field(default=None, exclude_if=is_none)
    # A locking read's transactional need (#799): recorded, never scored. Null when the
    # query has no locking clause, and for a trace saved before #799, which has no such key
    # (it loads as null).
    locking_clause: str | None = None

    @model_validator(mode="after")
    def _one_engines_features(self) -> Self:
        if (self.pg_features is None) == (self.mysql_features is None):
            raise ValueError("a query match has exactly one of pg_features and mysql_features")
        return self


class PatternSummary(base.PatternSummary):
    """One detected pattern, with the call rate of the queries that matched it."""

    model_config = KEEP_UNKNOWN

    total_calls_per_second: int | float


class RecommendationDerivation(base.RecommendationDerivation):
    """How one table's recommendation score was derived, and its migration complexity."""

    model_config = KEEP_UNKNOWN

    migration_complexity: MigrationComplexity | None


class AuroraDecisionTrace(base.DecisionTraceBase):
    """``analysis-aurora_<engine>/decision-trace.json``: how the Aurora analysis reached its result."""

    model_config = KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        default="1.0", description="The trace's calibration version from before this contract"
    )
    agent: str
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[RecommendationDerivation]
    llm_advisor: LlmAdvisorTrace
