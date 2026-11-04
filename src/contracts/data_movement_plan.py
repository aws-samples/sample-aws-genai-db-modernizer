# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data Movement Plan — Contract Model

One plan per migration step (#1413, part of #1412; per wave before #1797): what the step moves, from which
source, through which change-data-capture tool and hub topics, into which targets,
with which gates and stacks. Written by ``src.tools.data_movement.plan`` from a
finished job (the synthesis report's ``migration_waves`` and ``table_sources``, the
assignment, the collector schema and the schema designs) at
``<db>/<job>/data-movement/step-<k>/plan.json`` (per migration wave at ``wave-<n>``
before #1797), plus one hub plan per job at ``data-movement/hub/plan.json``. The stacks and the runtime read it;
it is also what a customer reviews before anything is deployed. No AWS call builds it.

Wave 1 (the move to the source-compatible Aurora engine) is a DMS move; every later
wave reads its source through Debezium into one compacted topic per source table.
The cache wave's target has the ``cache`` role: a replica of the engine it fronts,
fed from the same topics. The DMS step carries its ``replication`` section: the instance
class from the rule catalog's sizing rows with its reason, the LOB mode per table and the
Aurora version line the cluster needs (#1798).

Version History:
- 1.0 (2026-10-23, #1413): Initial version: source, cdc (with topic_prefix and
  signal_table, #1422), hub, initial_load, targets, gates, stacks, monthly_cost_usd with
  its assumptions, and unsupported.
- 1.1 (2026-11-04, #1797): Plan per step: step, contract reference, hub_topics. The new
  HubPlan model starts at its own 1.0.
- 1.2 (2026-11-04, #1798): Step 1 replication section: instance sizing, LOB settings,
  Aurora version line
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SourceTable(BaseModel):
    """One source table the wave reads."""

    table: str = Field(..., description="Source table id (schema.table), as the collector saw it")
    primary_key: list[str] = Field(
        default_factory=list, description="Its primary-key columns, in key order; empty if none"
    )
    estimated_rows: int | None = Field(
        None, ge=0, description="The collector's row count; None if it recorded none"
    )


class SourceSpec(BaseModel):
    """The database the wave reads from."""

    engine: str = Field(
        ...,
        description=(
            "The engine the wave reads (id): the legacy source for wave 1, the engine it "
            "moves from or fronts for later waves"
        ),
    )
    dialect: str | None = Field(
        None, description="The SQL dialect of that engine (mysql, postgresql, ...); None if unknown"
    )
    database: str = Field(..., description="The source database name")
    tables: list[SourceTable] = Field(
        default_factory=list, description="The tables the wave's targets read, sorted"
    )
    views: list[str] = Field(
        default_factory=list,
        description="Views in the wave: no rows to move, recreated from the schema, sorted",
    )


class SourceSetting(BaseModel):
    """A setting the source must have before capture starts."""

    name: str = Field(..., description="Parameter or feature name, e.g. binlog_format")
    value: str = Field(..., description="The value it needs, e.g. ROW")
    note: str = Field("", description="Where it is set or why it is needed")


class CdcSpec(BaseModel):
    """How changes are captured from the source."""

    tool: Literal["dms", "debezium"] = Field(
        ..., description="DMS for wave 1's move to Aurora, Debezium for every later wave"
    )
    connector_class: str | None = Field(
        None,
        description=(
            "Debezium source connector class for the source dialect; None for DMS and for "
            "a dialect with no Debezium connector (listed under unsupported)"
        ),
    )
    topic_prefix: str | None = Field(
        None,
        description=(
            "Debezium topic.prefix, dm-<job_id>-w<wave>: unique per job and wave, so "
            "replication slots, publications and schema-history topics never collide across "
            "waves, and every hub topic of the wave starts with it; None for DMS"
        ),
    )
    signal_table: str | None = Field(
        None,
        description=(
            "The source table Debezium reads incremental-snapshot signals from "
            "(<database>.debezium_signal); its creation is a required source setting. None "
            "for DMS"
        ),
    )
    snapshot_mode: str | None = Field(
        None,
        description=(
            "Debezium snapshot.mode: no_data when the initial load hands over a captured "
            "position, initial when the connector snapshots the tables itself; None for DMS"
        ),
    )
    heartbeat_interval_ms: int | None = Field(
        None, ge=0, description="Debezium heartbeat.interval.ms; None for DMS"
    )
    required_source_settings: list[SourceSetting] = Field(
        default_factory=list, description="Settings the source needs before capture starts"
    )


class HubTopic(BaseModel):
    """One compacted topic of the hub: the change stream of one source table."""

    table: str = Field(..., description="The source table the topic carries")
    topic: str = Field(..., description="Topic name: <cdc.topic_prefix>.<table id>")
    key_columns: list[str] = Field(..., min_length=1, description="Message key columns")
    key_source: Literal["primary_key", "unique_index"] = Field(
        ...,
        description=(
            "Where the key comes from: the primary key, or (a table without one) a unique "
            "index on NOT NULL columns, which the connector must name as its message key"
        ),
    )
    cleanup_policy: Literal["compact"] = Field(
        "compact", description="cleanup.policy: compacted, so the topic holds each key's latest row"
    )
    partitions: int = Field(..., ge=1, description="Partition count (see the plan's notes)")


class InitialLoad(BaseModel):
    """How existing rows reach the targets before the change stream takes over."""

    strategy: Literal["dms_full_load", "bulk_load_with_position_handoff", "connector_snapshot"] = (
        Field(
            ...,
            description=(
                "dms_full_load: DMS full load then ongoing replication (wave 1); "
                "bulk_load_with_position_handoff: a parallel bulk load from a captured log "
                "position the connector then starts from (MySQL and PostgreSQL); "
                "connector_snapshot: the connector snapshots the tables itself"
            ),
        )
    )
    position_capture: (
        Literal["dms_managed", "binlog_position", "replication_slot_lsn", "connector_offsets"]
        | None
    ) = Field(None, description="How the hand-over position is captured")


class TransformationRef(BaseModel):
    """The schema design a writer applies to the hub's rows."""

    artifact: str = Field(
        ...,
        description="The design's path under the job, e.g. schema-dynamodb/v2/schema_output.json",
    )
    design_version: str | None = Field(None, description="The design's own contract_version")


class Target(BaseModel):
    """One engine the wave writes, in one role."""

    engine: str = Field(..., description="The target engine (id)")
    role: Literal["owner", "replica", "cache"] = Field(
        ...,
        description=(
            "owner: takes the tables' writes once the wave cuts over; replica: serves reads "
            "of tables another engine owns, fed by CDC; cache: a CDC-fed replica of the "
            "engine it fronts"
        ),
    )
    tables: list[str] = Field(default_factory=list, description="The tables it receives, sorted")
    source_of_truth: str | None = Field(
        None,
        description=(
            "The engine that holds these tables' writes once the wave is done: the target "
            "itself for an owner, the owner (or the fronted engine) otherwise; None when "
            "no engine is named (a wave 1 with no Aurora target)"
        ),
    )
    transformation: TransformationRef | None = Field(
        None,
        description=(
            "The schema design the writer applies; None for wave 1 (carried over by DMS) "
            "and when the job has no design for the engine (listed under unsupported)"
        ),
    )


class LaggingRead(BaseModel):
    """Reads that run on a replica or cache and may see data behind the source of truth."""

    table: str = Field(..., description="The source table")
    engine: str = Field(..., description="The engine serving the reads (id)")
    read_query_ids: list[str] = Field(default_factory=list, description="The reads, sorted")


class ParityGate(BaseModel):
    """A per-table check that the target holds what the source holds."""

    table: str = Field(..., description="The source table")
    check: Literal["row_count_and_key_checksum", "sampled_key_compare"] = Field(
        ...,
        description=(
            "row_count_and_key_checksum for an owner or replica; sampled_key_compare for "
            "a cache, which holds only the keys it was asked for"
        ),
    )


class Gates(BaseModel):
    """What must hold before the wave cuts over."""

    wave_gate: str = Field(..., description="The wave's own gate, as the migration plan states it")
    parity: list[ParityGate] = Field(default_factory=list, description="Per table, sorted")
    max_lag_seconds: int = Field(
        ..., ge=0, description="Capture lag every target must stay under (see the plan's notes)"
    )
    lagging_reads: list[LaggingRead] = Field(
        default_factory=list,
        description=(
            "The reads that may lag: each replica target's reads of its tables (from the "
            "assignment and table_sources), and the cache's reads"
        ),
    )


class Stack(BaseModel):
    """One stack the wave deploys, in deploy order."""

    order: int = Field(..., ge=1, description="1-based deploy position")
    name: str = Field(..., description="Stack name, without the job prefix")
    purpose: str = Field(..., description="What it deploys")
    template: str | None = Field(
        None, description="The template's repository path; None until the template exists"
    )


class UnsupportedItem(BaseModel):
    """Something the wave needs that no writer, connector or price can provide yet."""

    kind: Literal[
        "no_key",
        "view",
        "key_design",
        "attribute",
        "unmapped_table",
        "no_schema_design",
        "reverse_replication",
        "source_dialect",
        "no_target",
        "unknown_table",
        "cost",
    ] = Field(..., description="What is unsupported")
    table: str | None = Field(None, description="The source table, when it is about one")
    engine: str | None = Field(None, description="The engine, when it is about one (id)")
    item: str | None = Field(
        None, description="The key pattern, attribute or component it is about"
    )
    reason: str = Field(..., description="Why it is unsupported, in one sentence")


class ContractRef(BaseModel):
    """The step's transformation contract (#1764), by path under the job and its hash."""

    artifact: str = Field(
        ..., description="Path under the job, e.g. transformation-contract/v2/step-1.json"
    )
    sha256: str | None = Field(None, description="The contract file's SHA-256, when written")


class DmsSizing(BaseModel):
    """Step 1's replication instance, chosen from the rule catalog (#1798)."""

    row: str = Field(..., description="The src/catalog/dms_sizing.toml row id")
    instance_class: str = Field(
        ..., description="The stack's InstanceClass default, e.g. dms.t3.medium"
    )
    allocated_storage_gb: int = Field(..., ge=5, description="The stack's AllocatedStorage default")
    included_storage_gb: int = Field(
        0,
        ge=0,
        description="The storage the instance's hourly price includes; only the rest is billed",
    )
    database_gb: float | None = Field(
        None, ge=0, description="The collector's database size; None if unknown"
    )
    writes_per_second: float | None = Field(
        None,
        ge=0,
        description="The summed calls_per_second of the collector's write statements; None if unknown",
    )
    reason: str = Field(
        ..., description="Why this row: the inputs, its bounds and the catalog's reason"
    )
    note: str | None = Field(
        None, description="The row's note (the largest row: size it with the customer)"
    )


class LobTable(BaseModel):
    """A step 1 table with LOB columns, and the LOB mode it needs (#1798).

    The reason each table needs full LOB mode; the task runs in full LOB mode when any does."""

    table: str = Field(..., description="Source table id (schema.table)")
    mode: Literal["limited", "full"] = Field(
        ...,
        description="limited: every LOB column declares a maximum within the catalog's limit; full otherwise",
    )
    columns: list[str] = Field(default_factory=list, description="Its LOB columns, sorted")
    max_size_kb: int | None = Field(
        None, ge=1, description="The largest declared LOB size in KB (limited mode only)"
    )
    reason: str = Field("", description="Why this mode: the sizes it was judged on, in bytes")


class UnboundedVarchar(BaseModel):
    """The PostgreSQL source endpoint's UnboundedVarcharMaxSize for step 1 (#1798)."""

    max_bytes: int | None = Field(
        None, ge=1, description="The setting's value in bytes; None when no step 1 column needs it"
    )
    columns: int = Field(
        0, ge=0, description="How many step 1 columns are a varchar without a length"
    )
    column_names: list[str] = Field(
        default_factory=list, description="Those columns (schema.table.column), sorted"
    )
    unsupported: list[str] = Field(
        default_factory=list,
        description=(
            "Step 1 columns that are a bpchar without a length (schema.table.column): the "
            "setting covers varchar only, so DMS may truncate them; sorted"
        ),
    )
    reason: str = Field(
        "", description="Why the setting is raised and what it leaves out; empty if neither"
    )


class DmsReplication(BaseModel):
    """How step 1's DMS task is sized and set (#1798): read by the DMS template and the deliverables."""

    target_engine: str | None = Field(
        None, description="The Aurora engine step 1 moves into (id); None when wave 1 names none"
    )
    target_engine_line: str | None = Field(
        None,
        description=(
            "The oldest Aurora major line at or above the source's (aurora_versions.toml); "
            "None if unknown"
        ),
    )
    sizing: DmsSizing = Field(..., description="The replication instance chosen from the catalog")
    multi_az_default: bool = Field(
        True, description="The stack's MultiAZ default (the cost prices it)"
    )
    lob_max_size_kb: int = Field(
        ..., ge=1, description="The task's LobMaxSize (limited LOB mode), in KB"
    )
    lob_tables: list[LobTable] = Field(
        default_factory=list,
        description="The tables with LOB columns and their LOB mode, sorted by table; the task runs in full LOB mode when any is full",
    )
    unbounded_varchar: UnboundedVarchar = Field(
        default_factory=UnboundedVarchar,
        description="The PostgreSQL source endpoint's UnboundedVarcharMaxSize for step 1",
    )

    @model_validator(mode="after")
    def _limited_tables_have_a_size(self) -> "DmsReplication":
        for t in self.lob_tables:
            if t.mode == "limited" and t.max_size_kb is None:
                raise ValueError(f"{t.table}: a limited LOB table needs max_size_kb")
        return self


class DataMovementPlan(BaseModel):
    """One migration step's data movement plan (#1797). Files written before #1797 are one
    wave's plan (``step`` is None, ``hub`` holds the wave's own topics).

    ``source`` is the step's logical source (the engine it moves from or fronts) and
    ``cdc`` the physical capture it reads: after step 1 that is the hub plan's."""

    contract_version: str = Field(
        default="1.2", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    job_id: str = Field(..., description="The job the plan is built from")
    database_name: str = Field(..., description="The job's database name")
    assignment_version: int = Field(
        ..., ge=0, description="The assignment version the synthesis report was built on"
    )
    wave: int = Field(..., ge=1, description="The wave, as migration_waves numbers it")
    wave_count: int = Field(..., ge=1, description="How many waves the job's roadmap has")
    title: str = Field(..., description="The wave's title, as migration_waves states it")
    step: int | None = Field(
        None,
        ge=1,
        description="The migration step (#1797); None in a plan written per wave before it",
    )
    contract: ContractRef | None = Field(
        None, description="The step's transformation contract, when it exists"
    )
    hub_topics: list[str] = Field(
        default_factory=list,
        description="The hub topics this step reads (steps after step 1), sorted",
    )
    source: SourceSpec = Field(
        ...,
        description=(
            "The step's logical source: the engine it moves from or fronts. The capture it "
            "reads is in cdc"
        ),
    )
    cdc: CdcSpec = Field(
        ...,
        description=(
            "The physical capture the step reads: DMS for step 1, the hub plan's Debezium "
            "capture for every later step"
        ),
    )
    hub: list[HubTopic] = Field(
        default_factory=list,
        description=(
            "Empty in a step plan (the topics are in the hub plan, named by hub_topics); "
            "filled only in an older per-wave plan, one compacted topic per source table "
            "the wave's targets read"
        ),
    )
    initial_load: InitialLoad = Field(
        ..., description="How existing rows reach the targets: DMS full load, or the hub's snapshot"
    )
    replication: DmsReplication | None = Field(
        None,
        description=(
            "Step 1's DMS instance sizing, LOB settings and Aurora line (#1798); None for a "
            "Debezium step and in a plan written before it"
        ),
    )
    targets: list[Target] = Field(default_factory=list, description="By engine, then role")
    gates: Gates = Field(..., description="What must hold before the step cuts over")
    stacks: list[Stack] = Field(default_factory=list, description="In deploy order")
    monthly_cost_usd: float | None = Field(
        None,
        ge=0,
        description=(
            "The wave's data movement cost per month (capture, hub, connectors); None when "
            "a component has no price in the price table (listed under unsupported)"
        ),
    )
    cost_assumptions: list[str] = Field(
        default_factory=list, description="What the cost figure assumes, or would assume"
    )
    unsupported: list[UnsupportedItem] = Field(
        default_factory=list, description="By kind, then table, engine and item"
    )
    notes: list[str] = Field(
        default_factory=list, description="The defaults the plan chose (partitions, lag gate)"
    )


class HubPlan(BaseModel):
    """The job's one change-data capture into the hub (#1797, ADR-033 Decision 10):
    one Debezium connector and one compacted topic per table, shared by every step after step 1."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    job_id: str = Field(..., description="The job the plan is built from")
    database_name: str = Field(..., description="The job's database name")
    assignment_version: int = Field(..., ge=1, description="The assignment version the plan reads")
    source: SourceSpec = Field(
        ..., description="The engine the hub captures: Aurora after step 1, else the source"
    )
    cdc: CdcSpec = Field(..., description="The Debezium capture")
    initial_load: InitialLoad = Field(..., description="Debezium's own snapshot")
    topics: list[HubTopic] = Field(
        default_factory=list, description="One compacted topic per table, sorted"
    )
    stacks: list[Stack] = Field(default_factory=list, description="In deploy order")
    monthly_cost_usd: float | None = Field(
        None, ge=0, description="The hub's monthly cost; None when unpriced"
    )
    cost_assumptions: list[str] = Field(
        default_factory=list, description="What the hub's cost figure assumes, or would assume"
    )
    unsupported: list[UnsupportedItem] = Field(
        default_factory=list,
        description="What the hub needs that no connector or price provides yet, sorted",
    )
    notes: list[str] = Field(
        default_factory=list, description="The defaults the hub plan chose (partitions)"
    )
