"""The shared core of every analysis agent's ``decision-trace.json`` (#1187).

Five of the six analysis engines (DynamoDB, DocumentDB, OpenSearch, Aurora
PostgreSQL and Aurora MySQL) write a trace with the same core: ``trace_version``,
``agent``, ``summary``, ``query_matches``, ``pattern_summaries`` and
``recommendation_derivations``, and most of them an ``llm_advisor`` section. This
module models that core once, so each engine's trace contract extends it instead
of repeating it, and the shared sections cannot drift apart between engines.

:class:`DecisionTraceBase` declares ``contract_version`` with no default: it is not
a contract of its own. Each engine's trace model sets its own default, which is the
version it writes and readers check (ADR-008 Decisions 1 and 2), and registers its
path in ``src/shared/contract_versioning.py``.

A field an engine writes in a different shape narrows it in its own subclass,
which keeps the field's position, so a trace's key order is the base's order
followed by the engine's own sections.

Every model keeps unknown keys (``extra="allow"``): an older or newer trace loads
(ADR-008 Decision 5), and a writer's round trip through the model loses no key a
builder added without declaring it.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

KEEP_UNKNOWN = ConfigDict(extra="allow")


def is_none(value: object) -> bool:
    """``exclude_if`` predicate: an optional field left at ``None`` is not written."""
    return value is None


class TraceSummary(BaseModel):
    """Query and pattern counts for the run."""

    model_config = KEEP_UNKNOWN

    queries_analyzed: int
    queries_matched: int
    queries_unmatched: int
    patterns_detected: int
    anti_patterns_detected: int


class QueryMatch(BaseModel):
    """The patterns and anti-patterns one query matched."""

    model_config = KEEP_UNKNOWN

    query_id: str
    query_text_preview: str
    matched_patterns: list[str]
    matched_anti_patterns: list[str]


class PatternSummary(BaseModel):
    """One detected pattern, with its catalog base score."""

    model_config = KEEP_UNKNOWN

    pattern_id: str
    pattern_type: str
    catalog_base_score: int | None
    queries_matched_count: int
    tables_involved: list[str]


class DerivationScores(BaseModel):
    """The four score dimensions of one table recommendation."""

    model_config = KEEP_UNKNOWN

    pattern_match: int
    complexity: int
    performance: int
    cost: int


class RecommendationDerivation(BaseModel):
    """How one table's recommendation score was derived."""

    model_config = KEEP_UNKNOWN

    table_id: str
    # The patterns behind the score. Not every engine writes it, so it is never
    # written as null; an engine that always writes it narrows it to required.
    segments_contributing: list[str] | None = Field(default=None, exclude_if=is_none)
    score_breakdown: DerivationScores
    weights_used: dict[str, float]
    weighted_confidence: int


class TableGroup(BaseModel):
    """An FK-connected cluster of more than one table."""

    model_config = KEEP_UNKNOWN

    group_root: str
    tables: list[str]


class LlmAdvisorTrace(BaseModel):
    """What the LLM advisor did: ``skipped``, ``success``, ``awaiting_external``, ..."""

    model_config = KEEP_UNKNOWN

    status: str
    duration_seconds: float
    attempts: int


class DecisionTraceBase(BaseModel):
    """The core every engine's decision trace contract extends."""

    model_config = KEEP_UNKNOWN

    contract_version: str = Field(
        ..., pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    trace_version: str = Field(
        ..., description="The trace's calibration version from before its contract"
    )
    agent: str
    summary: TraceSummary
    # Sequences, so an engine can narrow the item model in its own subclass. Every
    # subclass redeclares these three as ``list[...]``, so a trace is never parsed
    # into a tuple (or another sequence type).
    query_matches: Sequence[QueryMatch]
    pattern_summaries: Sequence[PatternSummary]
    recommendation_derivations: Sequence[RecommendationDerivation]
