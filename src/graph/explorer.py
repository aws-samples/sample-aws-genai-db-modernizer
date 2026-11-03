# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounded views for the experimental graph explorer (#308).

The explorer draws source tables and destination engines as nodes, and each
query as an edge from every table it touches to its assigned engine. The
queries between one table and one engine form a bundle: one edge, with the
bundle's query count and calls per second (computed here, so the UI only
draws them), that fans out into its busiest queries.

Fixed, parameterised, read-only traversals; every statement is bounded (a
LIMIT, or a bounded id list), and no caller-supplied Cypher reaches the graph.
These are a stop-gap until the graph query catalog (#307) supplies them.
"""

from __future__ import annotations

import math
from typing import Any

from src.api.models.graph_explorer_responses import (
    ExplorerBelowTraffic,
    ExplorerBundle,
    ExplorerBundleRef,
    ExplorerLinks,
    ExplorerNode,
    ExplorerOption,
    ExplorerOptions,
    ExplorerPathStep,
    ExplorerQuery,
    ExplorerQueryDetail,
    ExplorerScale,
    ExplorerSearch,
    ExplorerTableAccess,
    ExplorerTableTraffic,
    ExplorerWalkthrough,
    ExplorerWalkthroughs,
    ExplorerWalkthroughStep,
)
from src.graph.explorer_overlays import (
    Overlays,
    access_of,
    bundle_access,
    bundle_cached_in,
    bundle_wave,
    query_path,
    replicas_pending,
    wave_query_ids,
)
from src.graph.store import GraphStore
from src.shared.engine_names import display_engine

DEFAULT_LINK_LIMIT = 25
MAX_LINK_LIMIT = 60
# Queries a bundle carries for fanning out (its busiest).
BUNDLE_QUERY_LIMIT = 40
MAX_QUERY_TABLES = 20
SEARCH_LIMIT = 20
MIN_SEARCH_CHARS = 2
MAX_SEARCH_CHARS = 200
DEFAULT_OPTION_LIMIT = 500
MAX_OPTION_LIMIT = 1000
# The most query edges and bundles one below-traffic request may name: the
# view's element bound.
MAX_VIEW_QUERIES = 150
MAX_VIEW_BUNDLES = 150

# Characters of a query's SQL shown as its preview. Query ids are long (a
# 64-character digest, or a signed 64-bit integer such as PostgreSQL's
# queryid), so a query is labelled with its operation and its short id: the
# first SHORT_ID_CHARS characters and an ellipsis, the form the results page's
# query routing table shows (QueryRoutingTable.js), so the two can be matched.
SQL_PREVIEW_CHARS = 160
SHORT_ID_CHARS = 12

_CPS = "coalesce(q.calls_per_second, 0.0)"


def _limit(value: int, maximum: int) -> int:
    """A validated integer for a LIMIT literal (the engine binds a LIMIT
    parameter once per connection, see ``queries.query_journeys_page``)."""
    n = int(value)
    if n < 1 or n > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}, got {n}")
    return n


def _bounded(
    store: GraphStore, cypher: str, params: dict[str, Any], limit: int
) -> tuple[list[dict], bool]:
    """Rows of ``cypher`` (which must end before its LIMIT) cut at ``limit``."""
    return store.query_bounded(f"{cypher} LIMIT {limit + 1}", params, max_rows=limit)


def sql_preview(sql: object) -> str | None:
    """The SQL text on one line, cut to ``SQL_PREVIEW_CHARS`` (display only)."""
    if not sql:
        return None
    text = " ".join(str(sql).split())
    if len(text) > SQL_PREVIEW_CHARS:
        text = text[: SQL_PREVIEW_CHARS - 1] + "…"
    return text


def short_query_id(query_id: str) -> str:
    """The query's id cut to SHORT_ID_CHARS with an ellipsis (whole when short)."""
    text = str(query_id)
    return f"{text[:SHORT_ID_CHARS]}…" if len(text) > SHORT_ID_CHARS else text


def query_label(query_id: str, operation: object) -> str:
    short = short_query_id(query_id)
    return f"{operation} {short}" if operation else short


def bundle_key(table: str, engine: str) -> str:
    return f"bundle:{table}|{engine}"


def placeholder_tables(store: GraphStore, tables: list[str] | None = None) -> set[str]:
    """The tables the graph flags as a collector placeholder (``SourceTable.placeholder``,
    #306): of ``tables``, or of the whole graph when None."""
    if tables is None:
        rows = store.query("MATCH (st:SourceTable) WHERE st.placeholder RETURN st.id AS id")
    elif tables:
        rows = store.query(
            "MATCH (st:SourceTable) WHERE st.id IN $ids AND st.placeholder RETURN st.id AS id",
            {"ids": list(tables)},
        )
    else:
        return set()
    return {r["id"] for r in rows}


def _writes(store: GraphStore, query_ids: list[str]) -> set[tuple[str, str]]:
    """``(query, table)`` for each WRITES edge of ``query_ids`` (#306)."""
    if not query_ids:
        return set()
    rows = store.query(
        "MATCH (q:Query)-[:WRITES]->(st:SourceTable) WHERE q.id IN $ids "
        "RETURN q.id AS q, st.id AS t",
        {"ids": list(query_ids)},
    )
    return {(r["q"], r["t"]) for r in rows}


def _table_node(table: str, overlays: Overlays, placeholder: bool) -> ExplorerNode:
    ts = overlays.table_sources.get(table) or {}
    return ExplorerNode(
        key=f"table:{table}",
        kind="table",
        id=table,
        label=table,
        source_of_truth=ts.get("source_of_truth"),
        replica_engines=ts.get("replica_engines") or [],
        placeholder=placeholder,
    )


def _engine_node(engine: str, overlays: Overlays) -> ExplorerNode:
    return ExplorerNode(
        key=f"engine:{engine}",
        kind="engine",
        id=engine,
        label=display_engine(engine),
        wave=overlays.engine_wave.get(engine),
        monthly_cost_usd=overlays.engine_costs.get(engine),
    )


def explorer_options(store: GraphStore, limit: int = DEFAULT_OPTION_LIMIT) -> ExplorerOptions:
    """The pickers' choices: up to ``limit`` tables (by id), and every engine."""
    n = _limit(limit, MAX_OPTION_LIMIT)
    tables, tables_cut = _bounded(
        store,
        "MATCH (st:SourceTable) OPTIONAL MATCH (st)<-[:READS_FROM]-(q:Query) "
        "RETURN st.id AS id, count(DISTINCT q) AS c ORDER BY id",
        {},
        n,
    )
    # A plan has a handful of engines; the statement is bounded all the same.
    engines, _ = _bounded(
        store,
        "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination) "
        "RETURN d.engine AS id, count(DISTINCT q) AS c ORDER BY id",
        {},
        MAX_OPTION_LIMIT,
    )
    return ExplorerOptions(
        limit=n,
        tables=[ExplorerOption(id=r["id"], label=r["id"], query_count=r["c"]) for r in tables],
        engines=[
            ExplorerOption(id=r["id"], label=display_engine(r["id"]), query_count=r["c"])
            for r in engines
            if r.get("id")
        ],
        tables_truncated=tables_cut,
    )


def _pending(store: GraphStore, overlays: Overlays) -> bool:
    """The report's replica reads may be stale (``explorer_overlays.replicas_pending``)."""
    if not overlays.table_sources or overlays.report_version is None:
        return False
    return replicas_pending(overlays, store.graph_meta().get("effective_version"))


def _queries(
    store: GraphStore,
    query_ids: list[str],
    overlays: Overlays,
    table: str | None = None,
    writes: set[tuple[str, str]] | None = None,
    pending: bool = False,
) -> list[ExplorerQuery]:
    """The queries named (a bounded list), busiest first."""
    if not query_ids:
        return []
    rows = store.query(
        "MATCH (q:Query) WHERE q.id IN $ids "
        "OPTIONAL MATCH (q)-[:MIGRATES_TO]->(d:Destination) "
        "OPTIONAL MATCH (q)-[:READS_FROM]->(st:SourceTable) "
        "WITH q, min(d.engine) AS engine, collect(DISTINCT st.id) AS tables "
        "RETURN q.id AS id, q.operation_type AS op, q.sql_text AS sql, "
        f"  {_CPS} AS cps, engine, tables",
        {"ids": list(query_ids)},
    )
    rows.sort(key=lambda r: (-(r.get("cps") or 0.0), r["id"]))
    if writes is None:
        writes = _writes(store, [r["id"] for r in rows]) if table else set()
    out = []
    for r in rows:
        qid = r["id"]
        unit = overlays.query_units.get(qid)
        engine = r.get("engine")
        out.append(
            ExplorerQuery(
                query_id=qid,
                short_id=short_query_id(qid),
                label=query_label(qid, r.get("op")),
                operation=r.get("op"),
                sql_preview=sql_preview(r.get("sql")),
                calls_per_second=r.get("cps"),
                engine=engine,
                engine_label=display_engine(engine) if engine else None,
                tables=sorted(t for t in (r.get("tables") or []) if t)[:MAX_QUERY_TABLES],
                wave=overlays.query_move_wave.get(qid),
                cached_in_wave=overlays.query_cached_in.get(qid),
                path=[
                    ExplorerPathStep(
                        **step,
                        engine_label=display_engine(step["engine"]) if step["engine"] else None,
                    )
                    for step in query_path(overlays, qid)
                ],
                unit=unit[0] if unit else None,
                unit_size=unit[1] if unit else None,
                access=(
                    access_of(overlays, qid, table, (qid, table) in writes, pending)
                    if table
                    else None
                ),
            )
        )
    return out


# One row per (table, engine, query) first: the engine returns no sum when it
# is mixed with DISTINCT aggregates in one RETURN.
_PAIR_STATS = (
    f"WITH DISTINCT st.id AS t, d.engine AS e, q.id AS qid, {_CPS} AS c "
    "RETURN t, e, count(qid) AS n, sum(c) AS cps, collect(qid) AS ids "
    "ORDER BY cps DESC, t, e"
)


def _bundles(store: GraphStore, rows: list[dict], overlays: Overlays) -> list[ExplorerBundle]:
    """Bundles from pair rows (t, e, n, cps, ids), each with its busiest queries.
    One WRITES lookup serves every bundle."""
    bundles = []
    writes = _writes(
        store, sorted({str(q) for r in rows if r.get("e") for q in (r.get("ids") or [])})
    )
    pending = _pending(store, overlays)
    for r in rows:
        ids = [str(q) for q in (r.get("ids") or [])]
        if not r.get("e"):
            continue
        queries = _queries(
            store, _busiest(store, ids), overlays, table=r["t"], writes=writes, pending=pending
        )
        wave = bundle_wave(overlays, ids)
        cached = bundle_cached_in(overlays, ids)
        cut = overlays.cutover
        bundles.append(
            ExplorerBundle(
                key=bundle_key(r["t"], r["e"]),
                table=r["t"],
                engine=r["e"],
                query_count=int(r.get("n") or 0),
                calls_per_second=float(r.get("cps") or 0.0),
                wave=wave,
                cached_in_wave=cached,
                has_cached_reads=cached is not None,
                cutover_wave=(
                    cut["wave"] if cut and wave is not None and wave > cut["wave"] else None
                ),
                access=bundle_access(
                    overlays, r["t"], ids, {q for q, t in writes if t == r["t"]}, pending
                ),
                queries=queries,
                queries_truncated=len(ids) > len(queries),
            )
        )
    return bundles


def _busiest(store: GraphStore, ids: list[str]) -> list[str]:
    """The BUNDLE_QUERY_LIMIT busiest of ``ids``."""
    if len(ids) <= BUNDLE_QUERY_LIMIT:
        return ids
    rows = store.query(
        "MATCH (q:Query) WHERE q.id IN $ids "
        f"RETURN q.id AS id ORDER BY {_CPS} DESC, id LIMIT {BUNDLE_QUERY_LIMIT}",
        {"ids": ids},
    )
    return [r["id"] for r in rows]


def links(
    store: GraphStore, overlays: Overlays, kind: str, anchor: str, limit: int = DEFAULT_LINK_LIMIT
) -> ExplorerLinks | None:
    """The bundles a start point links to (busiest first, at most ``limit``),
    and their tables and engines; None when the start point is unknown."""
    n = _limit(limit, MAX_LINK_LIMIT)
    if kind == "table":
        if not store.query("MATCH (st:SourceTable {id: $id}) RETURN st.id AS id", {"id": anchor}):
            return None
        match = (
            "MATCH (st:SourceTable {id: $id})<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination) "
        )
        params: dict[str, Any] = {"id": anchor}
    elif kind == "engine":
        match = (
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination {engine: $id}) "
        )
        params = {"id": anchor}
    elif kind == "wave":
        try:
            wave = int(anchor)
        except ValueError:
            return None
        ids = wave_query_ids(overlays, wave)
        if not ids:
            return None
        match = (
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination) WHERE q.id IN $ids "
        )
        params = {"ids": ids}
    elif kind == "group":
        group = plan_group(overlays, anchor)
        if group is None:
            return None
        tables, engines = group
        match = (
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination) WHERE st.id IN $tables AND d.engine IN $engines "
        )
        params = {"tables": tables, "engines": engines}
    elif kind == "query":
        if not store.query("MATCH (q:Query {id: $id}) RETURN q.id AS id", {"id": anchor}):
            return None
        match = (
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query {id: $id})"
            "-[:MIGRATES_TO]->(d:Destination) "
        )
        params = {"id": anchor}
    else:
        raise ValueError(f"unknown start kind: {kind}")

    total_rows = store.query(
        match + "WITH DISTINCT st.id AS t, d.engine AS e RETURN count(*) AS c", params
    )
    total = int(total_rows[0]["c"] or 0) if total_rows else 0
    if kind == "engine" and total == 0:
        return None
    ranked, cut = _bounded(store, match + _PAIR_STATS, params, n)
    if kind in ("wave", "query") and ranked:
        # A wave (or one query) ranks the pairs by its own queries, then shows
        # each bundle whole, the same bundle a table or engine start shows.
        pairs = [{"t": r["t"], "e": r["e"]} for r in ranked]
        whole = store.query(
            "UNWIND $pairs AS p MATCH (st:SourceTable {id: p.t})<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination {engine: p.e}) " + _PAIR_STATS,
            {"pairs": pairs},
        )
        order = {(p["t"], p["e"]): i for i, p in enumerate(pairs)}
        ranked = sorted(whole, key=lambda r: order.get((r["t"], r["e"]), len(order)))
    bundles = _bundles(store, ranked, overlays)

    nodes: dict[str, ExplorerNode] = {}
    flagged = placeholder_tables(
        store, sorted({b.table for b in bundles} | ({anchor} if kind == "table" else set()))
    )
    if kind == "table":
        nodes[f"table:{anchor}"] = _table_node(anchor, overlays, anchor in flagged)
    if kind == "engine":
        nodes[f"engine:{anchor}"] = _engine_node(anchor, overlays)
    for b in bundles:
        nodes.setdefault(f"table:{b.table}", _table_node(b.table, overlays, b.table in flagged))
        nodes.setdefault(f"engine:{b.engine}", _engine_node(b.engine, overlays))
    return ExplorerLinks(
        anchor_kind=kind,  # type: ignore[arg-type]
        anchor_id=anchor,
        nodes=list(nodes.values()),
        bundles=bundles,
        limit=n,
        total_bundles=total,
        truncated=cut or total > len(bundles),
    )


# ---------------------------------------------------------------------------
# Walkthroughs (#308 round 6): guided starting points that follow the
# deliverables' own rationale. Every figure and text is the job's (the wave
# plan, facts.engines, the assignment's reasons) or a bounded graph read; the
# UI only wraps them in fixed templates.
# ---------------------------------------------------------------------------

BUSIEST_QUERY_STEPS = 5
MAX_STEP_TABLES = 8
ENGINE_TOP_TABLES = 3


def plan_group(overlays: Overlays, anchor: str) -> tuple[list[str], list[str]] | None:
    """The tables and engines of the wave plan's group ``"<wave>:<index>"``."""
    wave_s, sep, index_s = str(anchor).partition(":")
    if not sep:
        return None
    try:
        wave, index = int(wave_s), int(index_s)
    except ValueError:
        return None
    for w in overlays.plan:
        if w["wave"] == wave and 0 <= index < len(w["table_groups"]):
            tables = w["table_groups"][index]["tables"]
            return (tables, w["engines"]) if tables and w["engines"] else None
    return None


def _wave_steps(overlays: Overlays) -> list[ExplorerWalkthroughStep]:
    """One step per wave of the plan, then one per table group of the wave
    (most queries first). No sub-wave stages: the plan's groups only. No step
    fans out: each starts with every table and engine pair as one bundle."""
    steps: list[ExplorerWalkthroughStep] = []
    starts = set(overlays.query_move_wave.values()) | set(overlays.query_cached_in.values())
    for w in overlays.plan:
        if w["wave"] not in starts:
            continue
        steps.append(
            ExplorerWalkthroughStep(
                kind="wave",
                start_kind="wave",
                start_id=str(w["wave"]),
                view="moves-together" if overlays.query_units else "traffic",
                wave_filter=w["wave"],
                wave=w["wave"],
                title=w["title"],
                rationale=w["rationale"],
                gate=w["gate"],
                engine_labels=[display_engine(e) for e in w["engines"]],
            )
        )
        groups = sorted(
            enumerate(w["table_groups"]),
            key=lambda ig: (-(ig[1].get("query_count") or 0), ig[0]),
        )
        for position, (index, g) in enumerate(groups, start=1):
            if not g["tables"]:
                continue
            steps.append(
                ExplorerWalkthroughStep(
                    kind="group",
                    start_kind="group",
                    start_id=f"{w['wave']}:{index}",
                    view="moves-together" if overlays.query_units else "traffic",
                    wave_filter=w["wave"],
                    wave=w["wave"],
                    title=w["title"],
                    engine_labels=[display_engine(e) for e in w["engines"]],
                    group_index=position,
                    group_total=len(groups),
                    group_kind=g.get("kind"),
                    table_count=len(g["tables"]),
                    query_count=g.get("query_count"),
                    tables=g["tables"][:MAX_STEP_TABLES],
                )
            )
    return steps


def _busiest_steps(store: GraphStore, overlays: Overlays) -> list[ExplorerWalkthroughStep]:
    """The busiest queries, one per step, with their path and why that engine.
    ``fan_out`` here means the step draws its one query's edges, not its bundles."""
    rows = store.query(
        "MATCH (q:Query)-[:MIGRATES_TO]->(:Destination) WHERE q.calls_per_second > 0 "
        f"RETURN DISTINCT q.id AS id, q.calls_per_second AS cps ORDER BY cps DESC, id "
        f"LIMIT {BUSIEST_QUERY_STEPS}"
    )
    steps = []
    for r in rows:
        detail = query_detail(store, overlays, r["id"])
        if detail is None:
            continue
        steps.append(
            ExplorerWalkthroughStep(
                kind="query",
                start_kind="query",
                start_id=r["id"],
                fan_out=True,
                select_query=r["id"],
                query=ExplorerQuery(**detail.model_dump(include=set(ExplorerQuery.model_fields))),
                assignment_reason=detail.assignment_reason,
            )
        )
    return steps


def _engine_steps(store: GraphStore, overlays: Overlays) -> list[ExplorerWalkthroughStep]:
    """One step per destination engine: the report's rationale and figures for
    it, and its busiest tables (by the calls/s of their queries on it)."""
    engines = [
        r["e"]
        for r in store.query(
            "MATCH (q:Query)-[:MIGRATES_TO]->(d:Destination) "
            "RETURN d.engine AS e, count(DISTINCT q) AS n ORDER BY n DESC, e LIMIT 20"
        )
        if r.get("e")
    ]
    steps = []
    for engine in engines:
        fact = overlays.engine_facts.get(engine)
        if not fact:
            continue
        rows = store.query(
            "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination {engine: $e}) "
            + _PAIR_STATS
            + f" LIMIT {ENGINE_TOP_TABLES}",
            {"e": engine},
        )
        steps.append(
            ExplorerWalkthroughStep(
                kind="engine",
                start_kind="engine",
                start_id=engine,
                engine=engine,
                engine_label=display_engine(engine),
                role=fact.get("role"),
                rationale=fact.get("rationale"),
                workload_percent=fact.get("workload_percent"),
                monthly_cost_usd=fact.get("monthly_cost_usd"),
                busiest_tables=[
                    ExplorerTableTraffic(
                        table=r["t"],
                        query_count=int(r.get("n") or 0),
                        calls_per_second=float(r.get("cps") or 0.0),
                    )
                    for r in rows
                ],
            )
        )
    return steps


def walkthroughs(store: GraphStore, overlays: Overlays) -> ExplorerWalkthroughs:
    """The walkthroughs whose data the job has; one with no steps is left out."""
    found = [
        ExplorerWalkthrough(id="waves", steps=_wave_steps(overlays)),
        ExplorerWalkthrough(id="busiest", steps=_busiest_steps(store, overlays)),
        ExplorerWalkthrough(id="engines", steps=_engine_steps(store, overlays)),
    ]
    return ExplorerWalkthroughs(walkthroughs=[w for w in found if w.steps])


def search(store: GraphStore, overlays: Overlays, text: str) -> ExplorerSearch:
    """Queries whose id or SQL text contains ``text`` (case-insensitive), busiest
    first, at most SEARCH_LIMIT, with the nodes their edges need."""
    needle = " ".join(str(text).split()).lower()
    if not (MIN_SEARCH_CHARS <= len(needle) <= MAX_SEARCH_CHARS):
        raise ValueError(f"search text must be {MIN_SEARCH_CHARS} to {MAX_SEARCH_CHARS} characters")
    rows, cut = _bounded(
        store,
        "MATCH (q:Query) WHERE contains(lower(q.id), $s) "
        "  OR contains(lower(coalesce(q.sql_text, '')), $s) "
        f"RETURN q.id AS id ORDER BY {_CPS} DESC, id",
        {"s": needle},
        SEARCH_LIMIT,
    )
    results = _queries(store, [r["id"] for r in rows], overlays)
    nodes: dict[str, ExplorerNode] = {}
    flagged = placeholder_tables(store, sorted({t for q in results for t in q.tables}))
    for q in results:
        for t in q.tables:
            nodes.setdefault(f"table:{t}", _table_node(t, overlays, t in flagged))
        if q.engine:
            nodes.setdefault(f"engine:{q.engine}", _engine_node(q.engine, overlays))
    return ExplorerSearch(text=needle, results=results, nodes=list(nodes.values()), truncated=cut)


def query_detail(
    store: GraphStore, overlays: Overlays, query_id: str
) -> ExplorerQueryDetail | None:
    """A query's details panel: its edge facts, access-pattern class and why it
    is on its engine (the assignment's reason)."""
    found = _queries(store, [query_id], overlays)
    if not found:
        return None
    q = found[0]
    head = store.query(
        "MATCH (q:Query {id: $id})-[m:MIGRATES_TO]->(:Destination) "
        "RETURN m.assignment_reason AS reason, m.confidence AS confidence LIMIT 1",
        {"id": query_id},
    )
    cls = overlays.query_classes.get(query_id)
    written = _writes(store, [query_id])
    pending = _pending(store, overlays)
    return ExplorerQueryDetail(
        **q.model_dump(),
        access_pattern_class=cls[0] if cls else None,
        access_pattern_reason=cls[1] if cls else None,
        assignment_reason=head[0].get("reason") if head else None,
        confidence=head[0].get("confidence") if head else None,
        accesses=[
            ExplorerTableAccess(
                table=t,
                access=access_of(overlays, query_id, t, (query_id, t) in written, pending),
            )
            for t in q.tables
        ],
    )


# ---------------------------------------------------------------------------
# Traffic: the job's calls-per-second ranges, and which edges in the UI's view
# are below a threshold. The UI only scales these.
# ---------------------------------------------------------------------------


def scale_of(values: list[Any]) -> ExplorerScale | None:
    """The range of the positive ``values`` and three legend samples: the lowest,
    the geometric middle (the middle of a log scale) and the highest."""
    positive = [float(v) for v in values if isinstance(v, (int, float)) and v > 0]
    if not positive:
        return None
    low, high = min(positive), max(positive)
    middle = math.sqrt(low * high)
    return ExplorerScale(min=low, max=high, samples=[low, middle, high])


def traffic_scale(store: GraphStore) -> ExplorerScale | None:
    """The calls-per-second range of one query (those with traffic)."""
    rows = store.query(
        "MATCH (q:Query) WHERE q.calls_per_second > 0 "
        "RETURN min(q.calls_per_second) AS lo, max(q.calls_per_second) AS hi"
    )
    if not rows or rows[0].get("lo") is None:
        return None
    return scale_of([rows[0]["lo"], rows[0]["hi"]])


def bundle_traffic_scale(store: GraphStore) -> ExplorerScale | None:
    """The calls-per-second range of one bundle (a table and engine pair)."""
    rows = store.query(
        "MATCH (st:SourceTable)<-[:READS_FROM]-(q:Query)-[:MIGRATES_TO]->(d:Destination) "
        f"WITH DISTINCT st.id AS t, d.engine AS e, q.id AS qid, {_CPS} AS c "
        "WITH t, e, sum(c) AS s WHERE s > 0 "
        "RETURN min(s) AS lo, max(s) AS hi"
    )
    if not rows or rows[0].get("lo") is None:
        return None
    return scale_of([rows[0]["lo"], rows[0]["hi"]])


def cost_scale(engine_costs: dict[str, float]) -> ExplorerScale | None:
    """The engines' monthly cost range; the legend samples are the report's own
    engine costs (lowest first), never a figure in between."""
    scale = scale_of(list(engine_costs.values()))
    if scale is None:
        return None
    return ExplorerScale(
        min=scale.min, max=scale.max, samples=sorted({c for c in engine_costs.values() if c > 0})
    )


def below_traffic(
    store: GraphStore,
    query_ids: list[str],
    bundles: list[tuple[str, str]],
    min_calls_per_second: float,
) -> ExplorerBelowTraffic:
    """Of the query edges and bundles named (at most MAX_VIEW_QUERIES and
    MAX_VIEW_BUNDLES), those under the threshold: a query by its own calls per
    second (none counts as zero), a bundle by its total."""
    ids = sorted({str(q) for q in query_ids})
    pairs = sorted({(str(t), str(e)) for t, e in bundles})
    if len(ids) > MAX_VIEW_QUERIES or len(pairs) > MAX_VIEW_BUNDLES:
        raise ValueError(
            f"at most {MAX_VIEW_QUERIES} queries and {MAX_VIEW_BUNDLES} bundles per request"
        )
    threshold = float(min_calls_per_second)
    hidden_ids: list[str] = []
    hidden_pairs: list[tuple[str, str]] = []
    if threshold > 0 and ids:
        hidden_ids = [
            r["id"]
            for r in store.query(
                f"MATCH (q:Query) WHERE q.id IN $ids AND {_CPS} < $min "
                "RETURN q.id AS id ORDER BY id",
                {"ids": ids, "min": threshold},
            )
        ]
    if threshold > 0 and pairs:
        rows = store.query(
            "UNWIND $pairs AS p MATCH (st:SourceTable {id: p.t})<-[:READS_FROM]-(q:Query)"
            "-[:MIGRATES_TO]->(d:Destination {engine: p.e}) "
            f"WITH DISTINCT st.id AS t, d.engine AS e, q.id AS qid, {_CPS} AS c "
            "RETURN t, e, sum(c) AS s",
            {"pairs": [{"t": t, "e": e} for t, e in pairs]},
        )
        totals = {(r["t"], r["e"]): float(r.get("s") or 0.0) for r in rows}
        hidden_pairs = [p for p in pairs if totals.get(p, 0.0) < threshold]
    return ExplorerBelowTraffic(
        min_calls_per_second=threshold,
        hidden_query_ids=hidden_ids,
        hidden_bundles=[ExplorerBundleRef(table=t, engine=e) for t, e in hidden_pairs],
        hidden_count=len(hidden_ids) + len(hidden_pairs),
    )


def parse_bundle_ref(value: str) -> tuple[str, str]:
    """``"<table>|<engine>"`` (engine ids never hold ``|``) as a pair."""
    table, sep, engine = str(value).rpartition("|")
    if not sep or not table or not engine:
        raise ValueError(f"bundle must be '<table>|<engine>': {value!r}")
    return table, engine
