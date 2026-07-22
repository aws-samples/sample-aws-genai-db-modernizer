"""Pydantic response models for curated graph query endpoints."""

from pydantic import BaseModel


class AffectedQuery(BaseModel):
    query_id: str
    calls_per_second: float
    destinations: list[str]
    access_patterns: list[str]
    anti_patterns: list[str]


class TableImpactResponse(BaseModel):
    table_id: str
    affected_queries: list[AffectedQuery]


class ProvenanceDecision(BaseModel):
    decision_id: str
    category: str
    description: str
    agent_id: str | None = None
    phase: str | None = None


class QueryProvenanceResponse(BaseModel):
    query_id: str
    destination: str | None = None
    confidence: float | None = None
    assignment_reason: str | None = None
    signals: list[str]
    decisions: list[ProvenanceDecision]


class EngineDestination(BaseModel):
    destination_id: str
    source_tables: list[str]
    access_patterns: list[str]
    query_count: int


class EngineDetailResponse(BaseModel):
    engine: str
    destinations: list[EngineDestination]


class RiskHotspot(BaseModel):
    table_id: str
    total_calls_per_second: float
    risk_count: int
    anti_pattern_count: int


class RiskHotspotsResponse(BaseModel):
    hotspots: list[RiskHotspot]
