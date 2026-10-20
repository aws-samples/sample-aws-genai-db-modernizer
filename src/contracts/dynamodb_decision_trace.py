"""DynamoDB decision trace contract (``analysis-dynamodb/decision-trace.json``).

The DynamoDB analysis agent writes this trace beside ``analysis.json`` for
specialist calibration (ADR-017 Decision 3: it is not part of the
agent-to-agent contract, and the referee never reads it). It used to be a plain
dict, so a renamed field or a new denormalization ``subtype`` passed every
guard; this model types it, and its ``contract_version`` is checked on read
like every other stored contract (ADR-008 Decisions 1 and 4).

The sections every engine's trace shares (``summary``, ``query_matches``,
``pattern_summaries``, ``recommendation_derivations`` and the LLM advisor's core) are
modelled once in ``src/contracts/decision_trace_base.py`` (#1187); this contract
extends ``DecisionTraceBase`` and narrows a shared model where the DynamoDB trace
writes more (a query's ``transaction_shape``, a required ``segments_contributing``,
the advisor's key designs and strategies). The file it writes is unchanged.

``trace_version`` is the trace's own calibration version from before this
contract (``"1.1"``); nothing checks it. ``contract_version`` is the version
readers check.

Version History:
- 1.0 (2026-10-17, #973): Initial version, the sections the trace already had
  (``trace_version`` 1.1) plus ``contract_version``. A file without
  ``contract_version`` was written before this contract, and still loads.
  ``timestamp_columns`` (#935) is optional: a trace saved before it has none.
  Every trace model keeps unknown keys (``extra="allow"``); the two LLM advisor
  answer models (``AggregateKeyDesign``, ``DenormStrategy``) keep pydantic's default,
  since they also parse the model's answer.
- 1.1 (2026-10-17, #915): a write's transaction shape can be ``"key scoped"`` (the
  rows one value of a non-unique key column addresses), and its trace may carry
  ``schema_note``, the schema-design requirement that shape implies. Both additive:
  a 1.0 trace still loads.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.contracts import decision_trace_base as base
from src.contracts.analysis_output import MigrationComplexity

# The shared sections, re-exported: the DynamoDB trace writes them as the base has them.
from src.contracts.decision_trace_base import (
    PatternSummary,
    Relationship,
    TableGroup,
    TraceSummary,
)

# The shapes ``src/agents/referee/transaction_shape.py`` classifies a write into.
TransactionShapeKind = Literal["single item", "bounded", "key scoped", "unbounded"]


class DenormSubtype(str, Enum):
    """Denormalization sub-type classification.

    Each maps to a specific DynamoDB modeling strategy.
    """

    BOUNDED_PARENT_CHILD = "bounded-parent-child"
    MANY_TO_MANY_JUNCTION = "many-to-many-junction"
    CO_ACCESSED_TABLES = "co-accessed-tables"
    ADJACENCY_LIST = "adjacency-list"


# Every trace model keeps unknown keys, neither refused nor dropped: an older or
# newer trace loads (ADR-008 Decision 5), and a writer's round trip through the
# model loses no key a builder added without declaring it here.
_KEEP_UNKNOWN = base.KEEP_UNKNOWN
_is_none = base.is_none


class TransactionShapeTrace(BaseModel):
    """Which transaction shape a write matched, and why (ADR-031 Decision 3)."""

    model_config = _KEEP_UNKNOWN

    shape: TransactionShapeKind
    items: int | None
    reason: str
    # A key-scoped write's schema-design requirement (the column must become a
    # partition key or a GSI's); written only for that shape, never as null.
    schema_note: str | None = Field(default=None, exclude_if=_is_none)


class QueryMatch(base.QueryMatch):
    """The patterns and anti-patterns one query matched, and a write's transaction shape."""

    model_config = _KEEP_UNKNOWN

    # Written only for a write that has a shape; never written as null.
    transaction_shape: TransactionShapeTrace | None = Field(default=None, exclude_if=_is_none)


class RecommendationDerivation(base.RecommendationDerivation):
    """How one table's recommendation score was derived; the DynamoDB trace always names
    the patterns behind it."""

    model_config = _KEEP_UNKNOWN

    segments_contributing: list[str]


class TimestampNameFallback(BaseModel):
    """A query on which some column was read by name although the collection has types."""

    model_config = _KEEP_UNKNOWN

    query_id: str
    timestamp_column: bool
    read_by_name: list[str]


class TimestampColumnsTrace(BaseModel):
    """How the timestamp-column fact was read (#935): by column type, or by name."""

    model_config = _KEEP_UNKNOWN

    read_by: str
    name_fallbacks: list[TimestampNameFallback]


class PkClassificationTrace(BaseModel):
    """A table's primary key mapped to DynamoDB key candidates (``PkClassification``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    pk_columns: list[str]
    partition_key_candidate: str | None
    sort_key_candidate: str | None
    needs_redesign: bool
    no_pk: bool


class AggregateTrace(BaseModel):
    """Related tables that migrate together (``scoring.Aggregate``)."""

    model_config = _KEEP_UNKNOWN

    aggregate_id: str
    root_table: str
    member_tables: list[str]
    co_access_evidence: list[dict[str, Any]]
    co_access_confidence: int
    combined_migration_complexity: MigrationComplexity


class GsiCandidateTrace(BaseModel):
    """A frequently queried column group outside the PK (``GsiCandidate``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    partition_key_columns: list[str]
    sort_key_columns: list[str]
    total_frequency_per_hour: float
    query_ids: list[str]
    existing_index_name: str | None
    is_sparse: bool
    estimated_population_rate: float | None


class DenormalizationOpportunityTrace(BaseModel):
    """A classified denormalization opportunity (``DenormalizationOpportunity``)."""

    model_config = _KEEP_UNKNOWN

    subtype: DenormSubtype
    tables: list[str]
    cardinality_ratio: float | None
    co_access_frequency: float | None
    hierarchy_depth: int | None
    is_cyclic: bool | None
    evidence: dict[str, Any]


class SecondaryIndexDominanceTrace(BaseModel):
    """A table whose dominant access uses a secondary index (``SecondaryIndexDominance``)."""

    model_config = _KEEP_UNKNOWN

    table_id: str
    dominant_index_name: str
    dominant_index_columns: list[str]
    frequency_share: float
    alternative_pk_candidate: str | None
    alternative_sk_candidate: str | None


class AggregateKeyDesign(BaseModel):
    """Recommended DynamoDB key design for an aggregate."""

    aggregate_id: str
    partition_key: str
    sort_key: str | None = None
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this key design",
    )


class DenormStrategy(BaseModel):
    """Denormalization strategy recommendation for a detected opportunity."""

    opportunity_id: str  # references DenormalizationOpportunity
    strategy: str  # e.g., "embed child items", "adjacency list with GSI"
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this strategy",
    )


class LlmAdvisorTrace(base.LlmAdvisorTrace):
    """What the LLM advisor did, and the key designs and strategies it recommended."""

    model_config = _KEEP_UNKNOWN

    aggregate_key_designs: list[AggregateKeyDesign]
    denormalization_strategies: list[DenormStrategy]


class DynamoDBDecisionTrace(base.DecisionTraceBase):
    """``analysis-dynamodb/decision-trace.json``: how the DynamoDB analysis reached its result."""

    model_config = _KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        default="1.1", description="The trace's calibration version from before this contract"
    )
    agent: str = "dynamodb-analysis-agent"
    summary: TraceSummary
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[RecommendationDerivation]
    table_groups: list[TableGroup]
    # Written since #935; a trace saved before it has none, so it is never written as null.
    timestamp_columns: TimestampColumnsTrace | None = Field(default=None, exclude_if=_is_none)
    pk_classifications: list[PkClassificationTrace]
    aggregates: list[AggregateTrace]
    gsi_candidates: list[GsiCandidateTrace]
    denormalization_opportunities: list[DenormalizationOpportunityTrace]
    secondary_index_dominance: list[SecondaryIndexDominanceTrace]
    llm_advisor: LlmAdvisorTrace
    relationships: list[Relationship] = Field(default_factory=list)
