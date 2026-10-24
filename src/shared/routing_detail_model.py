"""The shape of the detailed engineering report's routing detail (#1364).

``src.shared.report_facts.routing_detail_facts`` builds it from the job's saved routing
artifacts (the assignment lineage and its Moves, the access-pattern record, each
engine's analysis) and the synthesis report; ``src.report.detailed_report`` only
formats it (ADR-032). Its figure fields are owned by the renderers-only-format guard
(``tests/unit/report/test_renderers_only_format.py``), like ``ReportFacts``'. Not a
pipeline contract: it is built when the deliverables are rendered and never saved.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EngineScoreCell(BaseModel):
    """One engine's routed score for a query, or the gate that kept it out."""

    engine: str
    score: float | None = None
    gate: str | None = None
    gate_text: str | None = None


class TableQueryRow(BaseModel):
    """A query that touches a table: its engine, class and routed score per engine."""

    query_id: str
    engine: str | None = None
    access_class: str | None = None
    scores: list[EngineScoreCell] = Field(default_factory=list)


class TableDetail(BaseModel):
    """One source table: its owner line (#212) and every query that touches it."""

    table: str
    owner: str | None = None
    owner_basis: str | None = None
    owner_reason: str | None = None
    also_read_by: list[str] = Field(default_factory=list)
    cached_by: str | None = None
    query_count: int = 0
    queries: list[TableQueryRow] = Field(default_factory=list)


class TableDimension(BaseModel):
    """One of a query's tables as an engine's analysis rated it (the complexity and
    performance dimensions are each table's)."""

    table: str
    complexity: float | None = None
    performance: float | None = None


class DimensionRow(BaseModel):
    """A query's scores by dimension on one analysed engine, as the artifacts record them."""

    engine: str
    routed: EngineScoreCell
    pattern_score: float | None = None
    redesigned_pattern_score: float | None = None
    parity_anchor: str | None = None
    intrinsic_penalty: float = 0
    intrinsic_findings: list[str] = Field(default_factory=list)
    fixable_findings: list[str] = Field(default_factory=list)
    tables: list[TableDimension] = Field(default_factory=list)


class StepDetail(BaseModel):
    """One Move of the query's ledger, worded by ``src.shared.move_reasons``."""

    version: int
    seq: int
    step: int
    rule: str
    actor: str
    from_engine: str | None = None
    to_engine: str | None = None
    explanation: str


class KeyCandidateDetail(BaseModel):
    """A column the redesign considered as the query's key (#1361)."""

    column: str
    outcome: str
    explanation: str


class QueryDetail(BaseModel):
    """One query's full routing record."""

    query_id: str
    excerpt: str | None = None
    tables: list[str] = Field(default_factory=list)
    in_scope: bool = True
    engine: str | None = None
    final_score: float | None = None
    pinned: str | None = None
    access_class: str | None = None
    access_reason: str | None = None
    redesign_class: str | None = None
    redesign_reason: str | None = None
    assumed_keys: list[str] = Field(default_factory=list)
    dimensions: list[DimensionRow] = Field(default_factory=list)
    steps: list[StepDetail] = Field(default_factory=list)
    summary: str = ""
    tied_engines: list[str] = Field(default_factory=list)
    close_call: bool = False
    settled_tie: bool = False
    runner_up: str | None = None
    outscored_by: list[str] = Field(default_factory=list)
    key_candidates: list[KeyCandidateDetail] = Field(default_factory=list)


class GroupMember(BaseModel):
    """A group member: its engine, its own routed score there, and whether it fits poorly
    (below the catalog's floor), with the target-native guidance when the report has it
    (#1362)."""

    query_id: str
    engine: str | None = None
    own_score: float | None = None
    poor_fit: bool = False
    intent_name: str | None = None
    approach: str | None = None


class GroupDetail(BaseModel):
    """A write group (a table's write statements, ADR-031 Amendment 9) or a co-dependency
    group, placed as one unit: its members, the group score per engine the placing Move
    compared, the winner's margin and the members that fit poorly."""

    kind: Literal["write", "co_dependency"]
    name: str
    member_count: int
    engines: list[str] = Field(default_factory=list)
    placed_version: int | None = None
    placed_seq: int | None = None
    placed_rule: str | None = None
    placement_group_key: str | None = None
    placement_member_count: int | None = None
    group_scores: dict[str, float] = Field(default_factory=dict)
    winner: str | None = None
    runner_up: str | None = None
    margin: float | None = None
    members: list[GroupMember] = Field(default_factory=list)
    poor_fit_count: int = 0
    guidance_recorded: bool = False


class RoutingDetail(BaseModel):
    """Everything the detailed engineering report states, and which records it had."""

    effective_version: int | None = None
    has_ledger: bool = False
    has_access_record: bool = False
    has_analysis: bool = False
    has_carried_writes: bool = False
    analysed_engines: list[str] = Field(default_factory=list)
    engine_scope: list[str] = Field(default_factory=list)
    poor_fit_floor: float | None = None
    query_count: int = 0
    table_count: int = 0
    moved_query_count: int = 0
    close_call_count: int = 0
    settled_tie_count: int = 0
    has_owner_lines: bool = False
    tied_query_count: int = 0
    write_group_count: int = 0
    single_statement_write_group_count: int = 0
    co_dependency_group_count: int = 0
    tables: list[TableDetail] = Field(default_factory=list)
    queries: list[QueryDetail] = Field(default_factory=list)
    write_groups: list[GroupDetail] = Field(default_factory=list)
    co_dependency_groups: list[GroupDetail] = Field(default_factory=list)
