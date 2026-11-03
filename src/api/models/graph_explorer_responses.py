# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Response models of the experimental graph explorer (#308, behind a flag).

Exploratory and unversioned: these shapes may change or go away when the
explorer moves onto the graph query catalog (#307).

The explorer draws source tables and destination engines as nodes, and each
query as an edge from every table it touches to its assigned engine. The
queries between one table and one engine form a bundle, drawn as one edge
until the user fans it out.
"""

from typing import Literal

from pydantic import BaseModel

NodeKind = Literal["table", "engine"]
# "group" is a table group of the wave plan ("<wave>:<index>"); "query" one query.
StartKind = Literal["table", "engine", "wave", "group", "query"]
# "read_or_replica": a read the report, built from another assignment version than
# the graph's, cannot yet place (report pending, #306).
Access = Literal["write", "replica_read", "read", "read_or_replica"]


class ExplorerOption(BaseModel):
    """One choice of a picker: a table or an engine."""

    id: str
    label: str
    # Queries that touch the table, or are assigned to the engine.
    query_count: int | None = None


class ExplorerOptions(BaseModel):
    limit: int
    tables: list[ExplorerOption]
    engines: list[ExplorerOption]
    tables_truncated: bool


class ExplorerNode(BaseModel):
    key: str  # "<kind>:<id>", unique across kinds
    kind: NodeKind
    id: str
    label: str
    # The wave an engine's queries move in; a table's source of truth and
    # replica engines; an engine's monthly cost (USD, the report's figure).
    wave: int | None = None
    source_of_truth: str | None = None
    replica_engines: list[str] = []
    monthly_cost_usd: float | None = None
    # True for the collector's placeholder table: a statement whose tables it
    # could not name (no table at all, such as SET SESSION or SELECT
    # FOUND_ROWS()), not a table of the source database.
    placeholder: bool = False


class ExplorerPathStep(BaseModel):
    """One step of a query's wave path (from the wave plan)."""

    wave: int
    engine: str | None = None
    engine_label: str | None = None
    # "cutover": it runs on this engine from this wave (the homogeneous first
    # move); "move": the wave it reaches its final engine in.
    kind: Literal["cutover", "move"]


class ExplorerQuery(BaseModel):
    """One query: drawn as an edge from each of its tables to its engine."""

    query_id: str
    # The id as the results page shows it (first 12 characters and an ellipsis).
    short_id: str
    label: str
    operation: str | None = None
    sql_preview: str | None = None
    calls_per_second: float | None = None
    engine: str | None = None
    engine_label: str | None = None
    # The tables it touches (at most MAX_QUERY_TABLES).
    tables: list[str] = []
    # The one wave it moves in; the cache wave in front of it (a cached read);
    # its path to its final engine.
    wave: int | None = None
    cached_in_wave: int | None = None
    path: list[ExplorerPathStep] = []
    # Its placement unit ("moves together") and the unit's size.
    unit: str | None = None
    unit_size: int | None = None
    # How it uses the bundle's table, when the report says who writes it.
    access: Access | None = None


class ExplorerBundle(BaseModel):
    """The queries between one table and one engine."""

    key: str
    table: str
    engine: str
    query_count: int
    # The bundle's calls per second: the sum over its queries (computed here).
    calls_per_second: float
    # The wave its queries move in; the cache wave in front of some of its
    # reads ("has cached reads"); the cutover wave it passes through first.
    wave: int | None = None
    cached_in_wave: int | None = None
    has_cached_reads: bool = False
    cutover_wave: int | None = None
    # "write" when one of its queries writes the table, "replica_read" when
    # every one reads a replica copy, else "read"; None without a report.
    access: Access | None = None
    # Its busiest queries (at most the bundle query limit), for fanning out.
    queries: list[ExplorerQuery]
    queries_truncated: bool


class ExplorerLinks(BaseModel):
    """What one start point (a table, an engine or a wave) links to."""

    anchor_kind: StartKind
    anchor_id: str
    nodes: list[ExplorerNode]
    bundles: list[ExplorerBundle]
    limit: int
    # Bundles the graph holds for this start point, and whether some were left out.
    total_bundles: int
    truncated: bool


class ExplorerSearch(BaseModel):
    text: str
    results: list[ExplorerQuery]
    nodes: list[ExplorerNode]
    truncated: bool


class ExplorerTableAccess(BaseModel):
    table: str
    access: Access | None = None


class ExplorerQueryDetail(ExplorerQuery):
    """A query's details panel."""

    access_pattern_class: str | None = None
    access_pattern_reason: str | None = None
    assignment_reason: str | None = None
    confidence: float | None = None
    accesses: list[ExplorerTableAccess] = []


class ExplorerScale(BaseModel):
    """A measure's range over the job and its legend samples (lowest first)."""

    min: float
    max: float
    samples: list[float]


class ExplorerWave(BaseModel):
    wave: int
    engines: list[str]
    engine_labels: list[str]
    migrating: bool


class ExplorerOverlays(BaseModel):
    """The views this job's data supports, and the figures their legends show."""

    views: list[str]
    waves: list[ExplorerWave]
    # Calls per second of one query, and of one bundle.
    traffic: ExplorerScale | None = None
    bundle_traffic: ExplorerScale | None = None
    cost: ExplorerScale | None = None
    # The collector's placeholder table names (statements with no table).
    placeholder_tables: list[str] = []


class ExplorerBundleRef(BaseModel):
    table: str
    engine: str


class ExplorerBelowTraffic(BaseModel):
    """Of the edges asked about, those under ``min_calls_per_second``."""

    min_calls_per_second: float
    hidden_query_ids: list[str]
    hidden_bundles: list[ExplorerBundleRef]
    # Edges hidden: the queries plus the bundles above.
    hidden_count: int


class ExplorerTableTraffic(BaseModel):
    table: str
    query_count: int
    calls_per_second: float


class ExplorerWalkthroughStep(BaseModel):
    """One step: what the graph shows (a start point, the view, the wave
    filter, whether bundles fan out, the query to select) and the job's own
    figures and text the step's explanation is made of."""

    kind: Literal["wave", "group", "query", "engine"]
    start_kind: StartKind
    start_id: str
    view: str = "traffic"
    wave_filter: int | None = None
    fan_out: bool = False
    select_query: str | None = None
    # The wave plan (wave and group steps).
    wave: int | None = None
    title: str | None = None
    rationale: str | None = None
    gate: str | None = None
    engine_labels: list[str] = []
    group_index: int | None = None
    group_total: int | None = None
    group_kind: str | None = None
    table_count: int | None = None
    query_count: int | None = None
    tables: list[str] = []
    # A query step.
    query: ExplorerQuery | None = None
    assignment_reason: str | None = None
    # An engine step (facts.engines and the engine's busiest tables).
    engine: str | None = None
    engine_label: str | None = None
    role: str | None = None
    workload_percent: float | None = None
    monthly_cost_usd: float | None = None
    busiest_tables: list[ExplorerTableTraffic] = []


class ExplorerWalkthrough(BaseModel):
    id: Literal["waves", "busiest", "engines"]
    steps: list[ExplorerWalkthroughStep]


class ExplorerWalkthroughs(BaseModel):
    """The walkthroughs this job's data supports (one left out has no data)."""

    walkthroughs: list[ExplorerWalkthrough]


class ExplorerStatus(BaseModel):
    enabled: bool
    # The bounds, only when the explorer is on.
    max_links: int | None = None
    max_options: int | None = None
