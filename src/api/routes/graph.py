"""Graph query routes — Cypher execution and rebuild trigger."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.graph import GraphStoreCache
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.storage.artifact_store import ArtifactStore

# Latency percentiles are flattened into source_{p}/target_{p} columns on
# LoadTestRun (LadybugDB cannot round-trip JSON strings). The endpoint
# reassembles them into nested objects for the response.
_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99", "p999", "min", "max")

router = APIRouter(prefix="/api/v1/assessments", tags=["graph"])

artifact_store: ArtifactStore | None = None
graph_cache: GraphStoreCache | None = None
sfn_service = None


class CypherRequest(BaseModel):
    cypher: str
    params: dict | None = None


def _get_database_name(job_id: str) -> str:
    """Resolve database_name from Step Functions execution input."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    execution = sfn_service.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return execution.get("input", {}).get("database_name", "")


def _get_graph(job_id: str):
    """Get a populated graph store for this job, building lazily if needed."""
    if not graph_cache or not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    store = graph_cache.get(db_name, job_id)

    if not store.is_populated():
        initialize_schema(store)
        rebuild_graph(db_name, job_id, artifact_store, store)

    return store, db_name


def get_graph_for_job(job_id: str):
    """FastAPI dependency wrapper around _get_graph.

    Exposed as a dependency so tests can override it via
    app.dependency_overrides without patching module globals.
    """
    return _get_graph(job_id)


@router.post("/{job_id}/graph/query")
async def query_graph(job_id: str, request: CypherRequest, graph: Any = Depends(get_graph_for_job)):
    """Execute a Cypher query against the assessment's graph."""
    store, _ = graph

    try:
        results = store.query(request.cypher, request.params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cypher error: {exc}") from exc

    columns = list(results[0].keys()) if results else []
    return {
        "columns": columns,
        "rows": results,
        "row_count": len(results),
    }


@router.post("/{job_id}/graph/rebuild")
async def rebuild_assessment_graph(job_id: str):
    """Force rebuild the graph from S3 artifacts."""
    if not graph_cache or not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    store = graph_cache.get(db_name, job_id)
    initialize_schema(store)
    stats = rebuild_graph(db_name, job_id, artifact_store, store)

    return {"status": "rebuilt", **stats}


def _nest_latency(query_row: dict, prefix: str) -> dict[str, float]:
    """Collect flattened {prefix}_{p} fields back into a percentile object."""
    return {p: query_row.get(f"{prefix}_{p}", 0.0) for p in _LATENCY_PERCENTILES}


@router.get("/{job_id}/load-test-results")
async def load_test_results(
    job_id: str,
    engine: str | None = Query(default=None),
    version: int | None = Query(default=None),
    prefix: str | None = Query(default=None),
    graph: Any = Depends(get_graph_for_job),
):
    """Load test results grouped by the solution-generated access-pattern id.

    Each pattern lists the queries it consolidates, with their source and
    target latency percentiles. When version is omitted, every populated
    version is returned (the graph holds only the latest per engine).
    """
    store, _ = graph

    rows = store.query(
        "MATCH (ap:AccessPattern)<-[:PART_OF]-(q:Query)-[:TESTED_IN]-(lt:LoadTestRun) "
        "WHERE ($engine IS NULL OR ap.engine = $engine) "
        "  AND ($version IS NULL OR ap.schema_version = $version) "
        "  AND ($prefix IS NULL OR starts_with(ap.id, $prefix)) "
        "RETURN ap.id AS pattern_id, ap.engine AS engine, "
        "  ap.schema_version AS schema_version, ap.description AS description, "
        "  ap.pattern_group AS pattern_group, ap.design_rps AS design_rps, "
        "  COLLECT({"
        "    query_id: q.id, improvement_factor: lt.improvement_factor, "
        "    throughput_rps: lt.throughput_rps, error_rate_pct: lt.error_rate_pct, "
        "    source_p50: lt.source_p50, source_p90: lt.source_p90, "
        "    source_p95: lt.source_p95, source_p99: lt.source_p99, "
        "    source_p999: lt.source_p999, source_min: lt.source_min, "
        "    source_max: lt.source_max, "
        "    target_p50: lt.target_p50, target_p90: lt.target_p90, "
        "    target_p95: lt.target_p95, target_p99: lt.target_p99, "
        "    target_p999: lt.target_p999, target_min: lt.target_min, "
        "    target_max: lt.target_max"
        "  }) AS queries "
        "ORDER BY pattern_id",
        {"engine": engine, "version": version, "prefix": prefix},
    )

    results = []
    for row in rows:
        queries = []
        for q in row.get("queries", []):
            queries.append(
                {
                    "query_id": q["query_id"],
                    "source_latency": _nest_latency(q, "source"),
                    "target_latency": _nest_latency(q, "target"),
                    "improvement_factor": q.get("improvement_factor"),
                    "throughput_rps": q.get("throughput_rps"),
                    "error_rate_pct": q.get("error_rate_pct"),
                }
            )
        results.append(
            {
                "pattern_id": row["pattern_id"],
                "engine": row["engine"],
                "schema_version": row["schema_version"],
                "description": row["description"],
                "pattern_group": row["pattern_group"],
                "design_rps": row["design_rps"],
                "queries": queries,
            }
        )

    return {"job_id": job_id, "results": results}
