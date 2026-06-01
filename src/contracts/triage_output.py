"""
Triage Output Contract

Data models for the triage agent output: selected/skipped engines,
workload signals, and confidence scoring.

Version History:
- 1.0 (2026-04-16): Initial version — extracted from triage_handler raw dict
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TriageSignalRecord(BaseModel):
    """A detected workload signal with evidence and traceability."""

    signal: str = Field(..., description="Signal name (e.g., text_search, key_value_lookups)")
    targets: list[str] = Field(..., description="Target engines this signal suggests")
    evidence: str = Field(..., description="Human-readable evidence for the signal")
    query_ids: list[str] = Field(
        default_factory=list, description="Query IDs that triggered this signal"
    )
    table_ids: list[str] = Field(
        default_factory=list, description="Table IDs related to this signal"
    )
    query_count: int = Field(default=0, ge=0, description="Number of queries matching this signal")


class SelectedAgent(BaseModel):
    """An analysis agent selected for dispatch."""

    agent_type: str = Field(..., description="Engine name (e.g., dynamodb, documentdb)")
    reasons: list[str] = Field(..., description="Why this engine was selected")


class SkippedAgent(BaseModel):
    """An analysis agent that was not dispatched."""

    agent_type: str = Field(..., description="Engine name")
    reason: str = Field(..., description="Why this engine was skipped")


class DeferredAgent(BaseModel):
    """An analysis agent deferred to a later phase."""

    agent_type: str = Field(..., description="Engine name")
    reasons: list[str] = Field(..., description="Why this engine was deferred")


class TriageOutputContract(BaseModel):
    """Output contract for the triage agent.

    Captures which engines were selected for analysis, which were skipped,
    what workload signals were detected, and the overall triage confidence.
    """

    contract_version: str = Field(default="1.1", description="Contract version")
    job_id: str = Field(..., description="Job identifier")
    database_name: str = Field(..., description="Source database name")
    agent_type: str = Field(default="referee-triage", description="Agent identifier")
    selected_agents: list[SelectedAgent] = Field(..., description="Engines selected for analysis")
    skipped_agents: list[SkippedAgent] = Field(..., description="Engines not selected")
    baseline: dict[str, list[str]] = Field(
        default_factory=dict, description="Baseline signals (e.g., aurora)"
    )
    deferred_agents: list[DeferredAgent] = Field(
        default_factory=list, description="Engines deferred to later phase"
    )
    signals: list[TriageSignalRecord] = Field(..., description="All detected workload signals")
    query_capabilities: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-query hard capability requirements: query_id -> [capability_names]",
    )
    confidence_score: int = Field(..., ge=0, le=100, description="Triage confidence (0-100)")
    timestamp: datetime = Field(..., description="When triage was run")
