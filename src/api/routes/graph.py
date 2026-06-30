"""Graph query routes — Cypher execution and rebuild trigger."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.graph import GraphStoreCache
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.storage.artifact_store import ArtifactStore

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


@router.post("/{job_id}/graph/query")
async def query_graph(job_id: str, request: CypherRequest):
    """Execute a Cypher query against the assessment's graph."""
    store, _ = _get_graph(job_id)

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
