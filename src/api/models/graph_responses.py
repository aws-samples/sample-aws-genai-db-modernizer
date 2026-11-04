# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pydantic response models for curated graph query endpoints."""

from typing import Any

from pydantic import BaseModel

# The query decision trace (#339) lives with the contracts, because synthesis
# writes the same records into report.json; re-exported for the graph API.
from src.contracts.query_trace import (
    QueryTraceResponse,
    TraceBasis,
    TraceCandidate,
    TraceEngineScore,
    TraceOutscored,
    TraceRunnerUp,
    TraceStep,
)

__all__ = [
    "AffectedQuery",
    "CatalogEntryInfo",
    "CatalogListResponse",
    "CatalogNeed",
    "CatalogParam",
    "CatalogProjection",
    "CatalogRunResponse",
    "EngineDestination",
    "EngineDetailResponse",
    "EngineFallback",
    "EngineTarget",
    "LatencyPercentilesModel",
    "LoadTestPattern",
    "LoadTestQuery",
    "LoadTestResultsResponse",
    "ProvenanceDecision",
    "QueryProvenanceResponse",
    "QueryTraceResponse",
    "RiskHotspot",
    "RiskHotspotsResponse",
    "TableImpactResponse",
    "TraceBasis",
    "TraceCandidate",
    "TraceEngineScore",
    "TraceOutscored",
    "TraceRunnerUp",
    "TraceStep",
]


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


class EngineTarget(BaseModel):
    """One real object of the design (a table, GSI, collection, index, stream or key
    pattern) in the effective assignment version, with the queries that land on it."""

    target_id: str
    kind: str
    name: str
    #: A GSI's table: its name, and its ``target_id`` in ``parent_id`` (the join key).
    parent: str | None = None
    parent_id: str | None = None
    schema_version: int
    #: Every effective query that lands on the object, assigned or cache.
    query_count: int
    #: How many of those queries use the engine as their cache layer only.
    cache_query_count: int = 0


class EngineFallback(BaseModel):
    """A labelled fallback, never an object: ``label`` is ``not_designed``,
    ``out_of_scope`` or ``not_in_design``, ``description`` its wording."""

    target_id: str
    label: str
    description: str
    query_count: int
    cache_query_count: int = 0


class EngineDetailResponse(BaseModel):
    """``destinations`` is the engine itself (a query's Destination is its engine,
    #306) with the source tables and access patterns of the queries migrating to it.
    ``targets`` and ``fallbacks`` (#306, TARGETS edges) are what every effective query
    lands on in this engine's design, assigned or cache; ``cache_query_count`` says how
    many of a count's queries use the engine only as their cache layer, so a cache-only
    engine has empty ``destinations`` and all its counts cache. A query that lands on
    several objects counts once under each."""

    engine: str
    destinations: list[EngineDestination]
    targets: list[EngineTarget] = []
    fallbacks: list[EngineFallback] = []


class RiskHotspot(BaseModel):
    table_id: str
    total_calls_per_second: float
    risk_count: int
    anti_pattern_count: int


class RiskHotspotsResponse(BaseModel):
    hotspots: list[RiskHotspot]


class LatencyPercentilesModel(BaseModel):
    p50: float
    p90: float
    p95: float
    p99: float
    p999: float
    min: float
    max: float


class LoadTestQuery(BaseModel):
    query_id: str
    source_latency: LatencyPercentilesModel
    target_latency: LatencyPercentilesModel
    improvement_factor: float | None = None
    throughput_rps: float | None = None
    error_rate_pct: float | None = None


class LoadTestPattern(BaseModel):
    pattern_id: str
    engine: str
    schema_version: int
    description: str
    pattern_group: str
    design_rps: float
    queries: list[LoadTestQuery]


class LoadTestResultsResponse(BaseModel):
    job_id: str
    results: list[LoadTestPattern]


# ---------------------------------------------------------------------------
# The graph query catalog (#307): GET /graph/catalog, POST /graph/catalog/{entry_id}
# ---------------------------------------------------------------------------


class CatalogNeed(BaseModel):
    """A pipeline stage an entry reads from."""

    id: str
    stage: str
    produces: str


class CatalogParam(BaseModel):
    """One parameter of an entry, with the values it can take on this job's graph."""

    name: str
    kind: str
    description: str
    example: str
    choices: list[str]
    #: True when the graph holds more values than ``choices`` lists.
    choices_truncated: bool


class CatalogEntryInfo(BaseModel):
    id: str
    ref: str
    section: str
    title: str
    question: str
    note: str
    max_rows: int
    params: list[CatalogParam]
    needs: list[CatalogNeed]


class CatalogListResponse(BaseModel):
    job_id: str
    entries: list[CatalogEntryInfo]
    #: The most choices a parameter lists (``catalog.MAX_CHOICES``); a parameter
    #: with ``choices_truncated`` lists exactly this many.
    max_choices: int


class CatalogProjection(BaseModel):
    """The rows as a graph: ``nodes`` are ``{id, label}``, ``edges`` ``{source, target, label}``."""

    nodes: list[dict[str, str]]
    edges: list[dict[str, str]]


class CatalogRunResponse(BaseModel):
    job_id: str
    entry: str
    params: dict[str, str]
    columns: list[str]
    #: Values are returned as stored: customer text is data, never markup.
    rows: list[dict[str, Any]]
    row_count: int
    #: True when more rows matched than the entry's ``max_rows``.
    truncated: bool
    max_rows: int
    projection: CatalogProjection
    #: When there are no rows: the entry's stages that left nothing in this graph.
    unmet_needs: list[CatalogNeed]
    explanation: str | None = None
