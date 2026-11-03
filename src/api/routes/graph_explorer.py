# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Experimental graph explorer routes (#308), off unless a flag is set.

The UI's probe (``GET /api/v1/experimental/graph-explorer``) always answers
200: ``{"enabled": false}`` unless ``MODERNIZER_EXPERIMENTAL_GRAPH_UI`` is
``1``, so the results page asks without a failed request. With the flag unset
(or anything but ``1``) every data route under
``/api/v1/assessments/{job_id}/graph/explore/`` answers, for every method, like
a path that does not exist (404 ``Not Found``), before the job's graph is
read. None of these routes is ever listed in the OpenAPI document. Set to
``1``, the probe reports the explorer on with its bounds and the bounded,
read-only GET views answer; another path under ``.../graph/explore`` answers
404 and another method 405.

Handlers are plain ``def`` so FastAPI runs the synchronous graph calls in its
threadpool (as in ``graph.py``).
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.dependencies import job_id_param
from src.api.models.graph_explorer_responses import (
    ExplorerBelowTraffic,
    ExplorerLinks,
    ExplorerOptions,
    ExplorerOverlays,
    ExplorerQueryBundles,
    ExplorerQueryDetail,
    ExplorerSearch,
    ExplorerStatus,
    ExplorerWalkthroughs,
    ExplorerWave,
    StartKind,
)
from src.api.routes import graph as graph_route
from src.graph import explorer
from src.graph.explorer_overlays import EMPTY, Overlays, read_overlays
from src.shared.engine_names import display_engine

logger = logging.getLogger(__name__)

EXPLORER_ENV_FLAG = "MODERNIZER_EXPERIMENTAL_GRAPH_UI"


def explorer_enabled() -> bool:
    return os.environ.get(EXPLORER_ENV_FLAG) == "1"


def require_explorer_enabled() -> None:
    """Dependency: answer like an unknown path unless the flag is on."""
    if not explorer_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


# The probe has its own router: it answers whatever the flag says.
status_router = APIRouter(prefix="/api/v1", tags=["experimental"], include_in_schema=False)

# Router-level dependencies run before any endpoint parameter, so with the flag
# off the graph is never leased (and never built) for these paths.
router = APIRouter(
    prefix="/api/v1",
    tags=["experimental"],
    include_in_schema=False,
    dependencies=[Depends(require_explorer_enabled)],
)


# The overlays of a job (read from its assignment and synthesis report) are
# cached per (database, job, effective assignment version) for this long, so a
# page of expansions reads the report once.
OVERLAY_TTL_S = 30.0
MAX_CACHED_OVERLAYS = 16
_overlay_cache: OrderedDict[tuple[str, str, int], tuple[float, Overlays]] = OrderedDict()
_overlay_lock = threading.Lock()


def load_overlays(job_id: str) -> Overlays:
    """The job's overlays; EMPTY when its artifacts cannot be read."""
    try:
        _, store, _ = graph_route._services()
        db_name = graph_route._resolve_db_name(job_id)
        version = graph_route._effective_assignment_version(db_name, job_id) or 0
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - overlays are optional
        logger.warning("graph explorer overlays unavailable for %s: %s", job_id, exc)
        return EMPTY
    key = (db_name, job_id, version)
    now = time.monotonic()
    with _overlay_lock:
        hit = _overlay_cache.get(key)
        if hit and 0 <= now - hit[0] < OVERLAY_TTL_S:
            return hit[1]
    overlays = read_overlays(store, db_name, job_id, version)
    with _overlay_lock:
        _overlay_cache[key] = (now, overlays)
        _overlay_cache.move_to_end(key)
        while len(_overlay_cache) > MAX_CACHED_OVERLAYS:
            _overlay_cache.popitem(last=False)
    return overlays


def get_overlays_for_job(job_id: str = Depends(job_id_param)) -> Overlays:
    """FastAPI dependency (tests override it)."""
    return load_overlays(job_id)


@status_router.get(
    "/experimental/graph-explorer",
    response_model=ExplorerStatus,
    response_model_exclude_none=True,
)
def graph_explorer_status() -> ExplorerStatus:
    """The UI's probe: ``{"enabled": false}`` when off; on, with its bounds."""
    if not explorer_enabled():
        return ExplorerStatus(enabled=False)
    return ExplorerStatus(
        enabled=True, max_links=explorer.MAX_LINK_LIMIT, max_options=explorer.MAX_OPTION_LIMIT
    )


@router.get("/assessments/{job_id}/graph/explore/options", response_model=ExplorerOptions)
def graph_explore_options(
    job_id: str = Depends(job_id_param),
    limit: int = Query(default=explorer.DEFAULT_OPTION_LIMIT, ge=1, le=explorer.MAX_OPTION_LIMIT),
    graph: Any = Depends(graph_route.get_graph_for_job),
):
    """The pickers' choices: tables (bounded) and engines."""
    store, _ = graph
    with graph_route._curated_errors():
        return explorer.explorer_options(store, limit=limit)


@router.get("/assessments/{job_id}/graph/explore/links", response_model=ExplorerLinks)
def graph_explore_links(
    kind: StartKind,
    id: str = Query(min_length=1, max_length=512),  # noqa: A002 - the start point's id
    job_id: str = Depends(job_id_param),
    limit: int = Query(default=explorer.DEFAULT_LINK_LIMIT, ge=1, le=explorer.MAX_LINK_LIMIT),
    graph: Any = Depends(graph_route.get_graph_for_job),
    overlays: Overlays = Depends(get_overlays_for_job),
):
    """The bundles (table to engine) a table, an engine or a wave links to,
    busiest first, each with its busiest queries for fanning out."""
    store, _ = graph
    with graph_route._curated_errors():
        out = explorer.links(store, overlays, kind, id, limit=limit)
    if out is None:
        raise HTTPException(status_code=404, detail="Not found in this assessment")
    return out


@router.get("/assessments/{job_id}/graph/explore/search", response_model=ExplorerSearch)
def graph_explore_search(
    text: str = Query(min_length=explorer.MIN_SEARCH_CHARS, max_length=explorer.MAX_SEARCH_CHARS),
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
    overlays: Overlays = Depends(get_overlays_for_job),
):
    """Queries whose id or SQL contains ``text`` (at most 20, busiest first)."""
    store, _ = graph
    try:
        with graph_route._curated_errors():
            return explorer.search(store, overlays, text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assessments/{job_id}/graph/explore/query", response_model=ExplorerQueryDetail)
def graph_explore_query(
    id: str = Query(min_length=1, max_length=512),  # noqa: A002 - the query's id
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
    overlays: Overlays = Depends(get_overlays_for_job),
):
    """One query's details panel."""
    store, _ = graph
    with graph_route._curated_errors():
        out = explorer.query_detail(store, overlays, id)
    if out is None:
        raise HTTPException(status_code=404, detail="Query not found in this assessment")
    return out


@router.get("/assessments/{job_id}/graph/explore/overlays", response_model=ExplorerOverlays)
def graph_explore_overlays(
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
    overlays: Overlays = Depends(get_overlays_for_job),
):
    """The views this job's data supports, its waves, and the traffic and cost
    ranges the legends show."""
    store, _ = graph
    with graph_route._curated_errors():
        traffic = explorer.traffic_scale(store)
        bundle_traffic = explorer.bundle_traffic_scale(store)
        placeholders = sorted(explorer.placeholder_tables(store))
    return ExplorerOverlays(
        views=overlays.views(),
        waves=[
            ExplorerWave(
                wave=w["wave"],
                engines=w["engines"],
                engine_labels=[display_engine(e) for e in w["engines"]],
                migrating=w["migrating"],
            )
            for w in overlays.waves
        ],
        traffic=traffic,
        bundle_traffic=bundle_traffic,
        cost=explorer.cost_scale(overlays.engine_costs),
        placeholder_tables=placeholders,
    )


@router.get(
    "/assessments/{job_id}/graph/explore/below-traffic", response_model=ExplorerBelowTraffic
)
def graph_explore_below_traffic(
    min_calls_per_second: float = Query(ge=0, le=1e12),
    query_id: list[str] = Query(default=[], max_length=explorer.MAX_VIEW_QUERIES),
    bundle: list[str] = Query(default=[], max_length=explorer.MAX_VIEW_BUNDLES),
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
):
    """Of the named query edges and bundles (``<table>|<engine>``) in the UI's
    view, those under the threshold, and how many."""
    store, _ = graph
    if any(len(v) > 1024 for v in [*query_id, *bundle]):
        raise HTTPException(status_code=422, detail="id too long")
    try:
        pairs = [explorer.parse_bundle_ref(b) for b in bundle]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with graph_route._curated_errors():
        return explorer.below_traffic(store, query_id, pairs, min_calls_per_second)


@router.get(
    "/assessments/{job_id}/graph/explore/query-bundles", response_model=ExplorerQueryBundles
)
def graph_explore_query_bundles(
    query_id: list[str] = Query(default=[], max_length=explorer.MAX_VIEW_QUERIES),
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
):
    """Every table-to-engine bundle each named query is in (at most 150 ids)."""
    store, _ = graph
    if any(len(v) > 1024 for v in query_id):
        raise HTTPException(status_code=422, detail="id too long")
    try:
        with graph_route._curated_errors():
            return explorer.query_bundles(store, query_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assessments/{job_id}/graph/explore/walkthroughs", response_model=ExplorerWalkthroughs)
def graph_explore_walkthroughs(
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(graph_route.get_graph_for_job),
    overlays: Overlays = Depends(get_overlays_for_job),
):
    """The guided walkthroughs this job's data supports: the migration wave by
    wave (the wave plan and its table groups), the busiest queries, and why
    each engine. Each step names what the graph shows and the job's own text
    and figures for it."""
    store, _ = graph
    with graph_route._curated_errors():
        return explorer.walkthroughs(store, overlays)


# Every other request under an explorer path: a non-GET method on a view, or
# any method on a path that is no view (`/graph/explore`, `/graph/explore/`,
# `/graph/explore/<not-a-view>`). Without this route Starlette answers 405 for
# a path a GET view matches with another method. The views are registered
# above, so a GET to one of them matches it first. With the flag off the
# router's dependency answers 404 like an unknown path, for every method; with
# it on, GET answers 404 and the other methods 405 (the explorer is read-only).
_ANY_METHOD = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/assessments/{job_id}/graph/explore", methods=_ANY_METHOD)
@router.api_route("/assessments/{job_id}/graph/explore/{rest:path}", methods=_ANY_METHOD)
def graph_explore_other_requests(
    request: Request, job_id: str = Depends(job_id_param), rest: str = ""
) -> None:
    """Reached only with the flag on: no such view, or not a GET."""
    if request.method == "GET":
        raise HTTPException(status_code=404, detail="Not Found")
    raise HTTPException(status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"})
