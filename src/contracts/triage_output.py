# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Triage Output Contract

Data models for the triage agent output: selected/skipped engines,
workload signals, and confidence scoring.

Version History:
- 1.0 (2026-04-16): Initial version — extracted from triage_handler raw dict
- 1.1 (by 2026-06-01): The version the contract shipped at in this repository; the
  change from 1.0 predates it and was not recorded.
- 1.2 (2026-10-17, #1006): ``contract_version`` declares the MAJOR.MINOR pattern
  every published version already matched (ADR-008 Decision 1).
- 1.3 (2026-10-18): ``query_capabilities`` may name ``ranking_window`` and
  ``offset_window`` (#968); ``scan_engine`` now means a recursive CTE only. A triage.json
  written before lists ``scan_engine`` for a window function too and still loads: it gates
  as strictly as it did.
- 1.4 (2026-10-22, #1327): Added optional ``engine_scope``: the customer's engine scope,
  sorted engine ids, or None for every engine (ADR-031 Decision 2 Amendment 14). A
  triage.json written before reads as None.

Note (#493, no schema change, version unchanged): the capability id
``inverted_index`` in ``query_capabilities`` is now ``text_pattern_match``. A
triage.json written before that still says ``inverted_index``; every reader maps
it through ``capability_registry.triage_query_capabilities``
(``LEGACY_CAPABILITY_IDS``), so an old assessment gates as a new one.
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


class ExcludedStatementRule(BaseModel):
    """The statements one rule left out of the workload (#2375)."""

    rule: str = Field(..., description="The rule (dms_object, dms_replication_slot, dms_cursor)")
    reasons: list[str] = Field(..., description="Why its statements were left out")
    statement_count: int = Field(..., ge=0, description="How many statements it left out")
    execution_count: int = Field(..., ge=0, description="Their executions in the collection")
    query_ids: list[str] = Field(..., description="The ids the statements would have had")


class ExcludedStatements(BaseModel):
    """The statements the collector left out of the workload because AWS DMS ran them
    (#2375, ``queries.excluded_statements`` in the collector output)."""

    statement_count: int = Field(..., ge=0, description="How many statements were left out")
    execution_count: int = Field(..., ge=0, description="Their executions in the collection")
    summary: str = Field(..., description="One sentence: how many were left out and why")
    rules: list[ExcludedStatementRule] = Field(..., description="Per rule, in a fixed order")


class TriageOutputContract(BaseModel):
    """Output contract for the triage agent.

    Captures which engines were selected for analysis, which were skipped,
    what workload signals were detected, and the overall triage confidence.
    """

    contract_version: str = Field(
        default="1.4", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
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
    engine_scope: list[str] | None = Field(
        default=None,
        description=(
            "The customer's engine scope, sorted engine ids (#1327, ADR-031 Decision 2 "
            "Amendment 14); None when every engine is in scope. Every engine it leaves out "
            "is in skipped_agents with the reason 'Not in the customer's engine scope'."
        ),
    )
    excluded_statements: ExcludedStatements | None = Field(
        default=None,
        description=(
            "The statements left out of the workload because AWS DMS ran them (#2375); "
            "None when none were, and for a triage.json written before."
        ),
    )
