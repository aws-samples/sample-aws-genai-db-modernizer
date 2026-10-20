"""DocumentDB decision trace contract (``analysis-documentdb/decision-trace.json``).

The DocumentDB analysis agent writes this trace beside ``analysis.json`` for
specialist calibration (ADR-017 Decision 3: it is not part of the
agent-to-agent contract). It used to be a plain dict, so a renamed field or a
new embedding ``strategy`` passed every guard; this model types it on the shared
``DecisionTraceBase`` (#1187), and its ``contract_version`` is checked on read
like every other stored contract (ADR-008 Decisions 1 and 4).

``trace_version`` is the trace's own calibration version from before this
contract (``"1.0"``); nothing checks it. ``contract_version`` is the version
readers check.

Version History:
- 1.0 (2026-10-20, #1188): Initial version, the sections the trace already had
  (``trace_version`` 1.0) plus ``contract_version``. A file without
  ``contract_version`` was written before this contract, and still loads. Every
  model keeps unknown keys (``extra="allow"``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from src.contracts import decision_trace_base as base
from src.contracts.decision_trace_base import (
    KEEP_UNKNOWN,
    LlmAdvisorTrace,
    PatternSummary,
    QueryMatch,
    Relationship,
    TableGroup,
    TraceSummary,
    is_none,
)


class EmbeddingRelationshipType(str, Enum):
    """How a child table relates to its parent, for the embed-or-reference decision."""

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_MANY = "many_to_many"


class EmbeddingStrategy(str, Enum):
    """Whether a child is embedded in its parent's document, referenced, or both."""

    EMBED = "embed"
    REFERENCE = "reference"
    HYBRID = "hybrid"


class StrategyRiskLevel(str, Enum):
    """The risk of an embedding strategy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EmbeddingCandidateTrace(BaseModel):
    """A parent-child pair weighed for embedding (``EmbeddingCandidate``)."""

    model_config = KEEP_UNKNOWN

    parent_table: str
    child_table: str
    relationship_type: EmbeddingRelationshipType
    # ``int | float``: a sum of integer call rates is written as an integer, and
    # the trace keeps the number it was given.
    avg_children_per_parent: int | float
    co_access_ratio: int | float
    independent_child_reads: int
    child_write_frequency: int | float
    estimated_doc_size_kb: int | float
    exceeds_16mb: bool


class DenormalizationStrategyTrace(BaseModel):
    """The embed-or-reference strategy for one parent-child pair.

    Written by the rule-based fallback, or by the LLM advisor
    (``DenormalizationStrategy``), which also writes the two optional fields,
    as null when it has no value.
    """

    model_config = KEEP_UNKNOWN

    parent_table: str
    child_table: str
    strategy: EmbeddingStrategy
    rationale: str
    trade_offs: str
    # The fallback never writes these two; the advisor's answer may hold null.
    hybrid_embedded_fields: list[str] | None = Field(default=None, exclude_if=is_none)
    estimated_document_size_kb: float | None = Field(default=None, exclude_if=is_none)
    risk_level: StrategyRiskLevel


class DocumentDBCompatibilityTrace(BaseModel):
    """The DocumentDB target the analysis assumed, and the features it found."""

    model_config = KEEP_UNKNOWN

    target_version: str
    cluster_type: str
    version_dependent_features_detected: list[str]
    unsupported_features_detected: list[str]


class DocumentDBDecisionTrace(base.DecisionTraceBase):
    """``analysis-documentdb/decision-trace.json``: how the DocumentDB analysis reached its result."""

    model_config = KEEP_UNKNOWN

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        default="1.0", description="The trace's calibration version from before this contract"
    )
    agent: str = "documentdb-analysis-agent"
    summary: TraceSummary
    query_matches: list[QueryMatch]
    pattern_summaries: list[PatternSummary]
    recommendation_derivations: list[base.RecommendationDerivation]
    table_groups: list[TableGroup]
    embedding_candidates: list[EmbeddingCandidateTrace]
    polymorphic_tables: list[str]
    denormalization_strategies: list[DenormalizationStrategyTrace]
    llm_advisor: LlmAdvisorTrace
    documentdb_compatibility: DocumentDBCompatibilityTrace
    relationships: list[Relationship] = Field(default_factory=list)
