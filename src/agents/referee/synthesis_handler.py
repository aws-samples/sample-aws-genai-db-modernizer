"""Referee-Synthesis agent handler — produces the modernization report.

Reads all pipeline artifacts (triage, collector, analysis, schema design)
via ArtifactStore, builds a comprehensive report with architecture
recommendations, table mappings, query groups, TCO analysis, and risk
assessment.

The synthesis report is consumed by:
- The UI (results page — architecture view, query groups, table mappings)
- Step Functions (needs_deeper_analysis flag for the analysis loop)

LLM seam functions (for Skill Sync / external LLM integration):
- run_synthesis_deterministic — full report without any LLM call
- prepare_synthesis_llm_input — formats the LLM request payload
- apply_synthesis_llm_output  — merges LLM output into deterministic result
"""

from datetime import UTC, datetime

from src.agents.referee.synthesis_data import load_synthesis_data
from src.agents.referee.synthesis_report import (
    build_architecture_recommendation,
    build_query_groups,
    build_ranking,
    build_risk_assessment,
    build_summary,
    build_table_mappings,
    build_tco_analysis,
    generate_executive_summary,
)
from src.contracts.synthesis_output import SynthesisOutputContract
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Seam 1: Deterministic — all builders, no LLM
# ---------------------------------------------------------------------------


def run_synthesis_deterministic(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> dict:
    """Run all deterministic synthesis logic without invoking any LLM.

    Loads all pipeline artifacts, calls all deterministic builders, and
    sets ``executive_summary`` to the deterministic summary (fallback value).

    Returns a dict with keys:
        job_id, database_name, timestamp, needs_deeper_analysis,
        ranking, table_mappings, query_groups, tco_analysis, risk_assessment,
        architecture, trade_offs, assignment_summary, reality_check_summary,
        summary (deterministic), executive_summary (deterministic fallback),
        data (internal SynthesisData — for use by apply_synthesis_llm_output)
    """
    print("[synthesis] Loading all pipeline artifacts...")
    data = load_synthesis_data(store, job_id, database_name, assignment_version)
    print(
        f"[synthesis] Loaded: {len(data.engines)} engines, "
        f"{len(data.source_tables)} source tables, "
        f"{len(data.source_queries)} queries"
    )

    # Load reality check output if available (optional — may not exist)
    reality_check_key = f"{database_name}/{job_id}/reality-check/output.json"
    reality_check_output: dict | None = None
    if store.exists(reality_check_key):
        reality_check_output = store.read_json(reality_check_key)
        print(
            f"[synthesis] Reality check loaded: "
            f"{len(reality_check_output.get('consolidations', []))} consolidations, "
            f"{len(reality_check_output.get('architectural_patterns', []))} patterns"
        )

    print("[synthesis] Building ranking...")
    ranking = build_ranking(data)
    print("[synthesis] Building table mappings...")
    table_mappings = build_table_mappings(data)
    print("[synthesis] Building query groups...")
    query_groups = build_query_groups(data)
    print("[synthesis] Building TCO analysis...")
    tco = build_tco_analysis(data)
    print("[synthesis] Building risk assessment...")
    risk_assessment = build_risk_assessment(data)
    print("[synthesis] Building architecture recommendation...")
    architecture = build_architecture_recommendation(data, ranking, table_mappings)
    needs_deeper = any(
        40 <= r["confidence_score"] < 70 and r.get("migration_complexity_avg") == "HIGH"
        for r in ranking
    )
    deterministic_summary = build_summary(
        data, ranking, table_mappings, tco, risk_assessment, query_groups
    )
    trade_offs = _collect_trade_offs(data)

    assignment_summary = None
    if data.assignment:
        assignment_summary = {
            "version": data.assignment.get("version"),
            "status": data.assignment.get("status"),
            "query_count": len(data.assignment.get("query_assignments", [])),
            "in_scope_count": sum(
                1 for qa in data.assignment.get("query_assignments", []) if qa.get("in_scope", True)
            ),
            "co_dependency_groups": len(data.assignment.get("co_dependency_groups", [])),
        }

    # Merge reality check data into trade_offs for visibility
    reality_check_summary = None
    if reality_check_output:
        reality_check_summary = {
            "consolidations": reality_check_output.get("consolidations", []),
            "architectural_patterns": reality_check_output.get("architectural_patterns", []),
            "recommendations": reality_check_output.get("recommendations", []),
            "before_distribution": reality_check_output.get("before_distribution", {}),
            "after_distribution": reality_check_output.get("after_distribution", {}),
        }
        for rec in reality_check_output.get("recommendations", []):
            rec_desc = rec if isinstance(rec, str) else str(rec)
            if not any(t.get("description") == rec_desc for t in trade_offs):
                trade_offs.append(
                    {
                        "description": rec_desc,
                        "impact": rec_desc,
                        "source_tables": [],
                        "target_tables": [],
                        "query_ids": [],
                        "engine": "reality-check",
                    }
                )

    return {
        "job_id": job_id,
        "database_name": database_name,
        "timestamp": datetime.now(UTC).isoformat(),
        "needs_deeper_analysis": needs_deeper,
        "ranking": ranking,
        "table_mappings": table_mappings,
        "query_groups": query_groups,
        "tco_analysis": tco,
        "risk_assessment": risk_assessment,
        "architecture": architecture,
        "trade_offs": trade_offs,
        "assignment_summary": assignment_summary,
        "reality_check_summary": reality_check_summary,
        "summary": deterministic_summary,
        "executive_summary": deterministic_summary,  # fallback; overwritten by LLM
        # Internal — holds the SynthesisData object for schema summaries in the writer
        "data": data,
    }


# ---------------------------------------------------------------------------
# Seam 2: Format LLM input payload
# ---------------------------------------------------------------------------


def prepare_synthesis_llm_input(deterministic_result: dict) -> dict:
    """Return the payload that should be sent to the executive-summary LLM.

    Keys returned:
        deterministic_summary, ranking, query_groups, tco_analysis,
        risk_assessment, table_mappings, trade_offs
    """
    return {
        "deterministic_summary": deterministic_result["summary"],
        "ranking": deterministic_result["ranking"],
        "query_groups": deterministic_result["query_groups"],
        "tco_analysis": deterministic_result["tco_analysis"],
        "risk_assessment": deterministic_result["risk_assessment"],
        "table_mappings": deterministic_result["table_mappings"],
        "trade_offs": deterministic_result["trade_offs"],
    }


# ---------------------------------------------------------------------------
# Seam 3: Apply LLM output back onto the deterministic result
# ---------------------------------------------------------------------------


def apply_synthesis_llm_output(deterministic_result: dict, llm_output: dict) -> dict:
    """Merge the LLM-generated executive summary into the deterministic result.

    If ``llm_output`` contains an ``executive_summary`` key its value replaces
    the deterministic fallback.  All other keys in the result are unchanged.

    Returns the updated result dict (mutates and returns the same dict).
    """
    if "executive_summary" in llm_output:
        deterministic_result["executive_summary"] = llm_output["executive_summary"]
    return deterministic_result


# ---------------------------------------------------------------------------
# Writer helper — shared by run_synthesis
# ---------------------------------------------------------------------------


def _write_synthesis_report(
    store: ArtifactStore,
    result: dict,
    assignment_version: int,
) -> None:
    """Validate against SynthesisOutputContract and write the report JSON."""
    data = result["data"]
    output_data: dict = {
        "job_id": result["job_id"],
        "database_name": result["database_name"],
        "timestamp": result["timestamp"],
        "needs_deeper_analysis": result["needs_deeper_analysis"],
        "ranking": result["ranking"],
        "summary": result["executive_summary"],
        "summary_deterministic": result["summary"],
        "recommended_architecture": result["architecture"],
        "table_mappings": result["table_mappings"],
        "query_groups": result["query_groups"],
        "tco_analysis": result["tco_analysis"],
        "risk_assessment": result["risk_assessment"],
        "schema_designs": _build_schema_summaries(data),
        "trade_offs": result["trade_offs"],
        "assignment_summary": result["assignment_summary"],
    }
    if result.get("reality_check_summary"):
        output_data["reality_check"] = result["reality_check_summary"]

    output = SynthesisOutputContract.model_validate(output_data)
    job_id = result["job_id"]
    database_name = result["database_name"]
    if assignment_version > 0:
        key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    store.write_json(key, output.model_dump(mode="json"))
    print(f"[synthesis] Report written to {key}")

    ranking = result["ranking"]
    ranking_str = ", ".join(f"{r['target']}={r['confidence_score']}%" for r in ranking)
    print(f"[synthesis] Ranking: [{ranking_str}]")
    if data.assignment:
        workload_parts = []
        for r in ranking:
            aq = r.get("assigned_queries", 0)
            wp = r.get("workload_percent", 0)
            workload_parts.append(f"{r['target']}={aq} queries ({wp}%)")
        workload_str = ", ".join(workload_parts)
        print(f"[synthesis] Workload: {workload_str}")
    architecture = result["architecture"]
    print(f"[synthesis] Architecture: {architecture['architecture_type']}")
    print(f"[synthesis] Table mappings: {len(result['table_mappings'])}")
    print(f"[synthesis] Query groups: {len(result['query_groups'])}")
    risk_count = len(result["risk_assessment"]["risks"])
    risk_level = result["risk_assessment"]["overall_risk_level"]
    print(f"[synthesis] Risks: {risk_count} ({risk_level})")


# ---------------------------------------------------------------------------
# Top-level handler
# ---------------------------------------------------------------------------


def run_synthesis(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> None:
    """Run the synthesis agent. Reads all artifacts, writes report.

    Args:
        job_id: Pipeline job identifier.
        database_name: Source database name.
        store: ArtifactStore instance.
        assignment_version: Assignment version to load (0 = unversioned).
        llm_mode: Controls how the executive summary is generated.
            "bedrock"  — call generate_executive_summary() via Bedrock (default).
            "external" — write LLM input to store and skip the LLM call.
            "none"     — use the deterministic summary; no LLM call.
    """
    result = run_synthesis_deterministic(job_id, database_name, store, assignment_version)

    if not result["data"].engines:
        print("[synthesis] ERROR: No engine artifacts found")
        _write_empty_report(store, database_name, job_id, assignment_version)
        return

    if llm_mode == "bedrock":
        trade_offs = result["trade_offs"]
        executive_summary = generate_executive_summary(
            result["summary"],
            result["ranking"],
            result["query_groups"],
            result["tco_analysis"],
            result["risk_assessment"],
            result["table_mappings"],
            trade_offs,
        )
        result = apply_synthesis_llm_output(result, {"executive_summary": executive_summary})

    elif llm_mode == "external":
        llm_input = prepare_synthesis_llm_input(result)
        llm_input_key = (
            f"{database_name}/{job_id}/synthesis/llm_input.json"
            if assignment_version == 0
            else f"{database_name}/{job_id}/synthesis/v{assignment_version}/llm_input.json"
        )
        store.write_json(llm_input_key, llm_input)
        print(f"[synthesis] LLM input written to {llm_input_key}")

    # llm_mode == "none" — keep the deterministic summary already set

    _write_synthesis_report(store, result, assignment_version)


def _build_schema_summaries(data):
    summaries = {}
    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        if engine == "opensearch":
            summaries[engine] = _build_opensearch_schema_summary(schema)
        elif engine == "elasticache" and schema.get("key_designs"):
            summaries[engine] = _build_elasticache_schema_summary(schema)
        elif engine == "documentdb" and schema.get("collections"):
            summaries[engine] = _build_documentdb_schema_summary(schema)
        elif schema.get("table_definitions"):
            summaries[engine] = _build_standard_schema_summary(schema)
        else:
            summaries[engine] = {"status": "not_available"}
    return summaries


def _build_standard_schema_summary(schema):
    tables = schema["table_definitions"]
    access_patterns = schema.get("access_patterns", [])
    hot_partition = schema.get("hot_partition_analysis", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": t["table_name"],
                "aggregate_pattern": t.get("aggregate_pattern"),
                "source_tables": t.get("source_tables", []),
                "gsi_count": len(t.get("gsis", [])),
                "item_count": t.get("item_count", 0),
                "item_size_bytes": t.get("item_size_bytes", 0),
            }
            for t in tables
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": sum(1 for hp in hot_partition if hp.get("at_risk")),
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_documentdb_schema_summary(schema):
    """Build schema summary for DocumentDB (collections format)."""
    collections = schema.get("collections", [])
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": c.get("collection_name", ""),
                "aggregate_pattern": "document_collection",
                "source_tables": c.get("source_tables", []),
                "gsi_count": len(c.get("indexes", [])),
                "item_count": 0,
                "item_size_bytes": 0,
            }
            for c in collections
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_elasticache_schema_summary(schema):
    """Build schema summary for ElastiCache/Redis (key_designs format)."""
    key_designs = schema.get("key_designs", [])
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": kd.get("key_pattern", ""),
                "aggregate_pattern": kd.get("data_type", "unknown"),
                "source_tables": kd.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "ttl_seconds": kd.get("ttl_seconds"),
            }
            for kd in key_designs
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_opensearch_schema_summary(schema):
    index_designs = schema.get("index_designs", [])
    data_stream_designs = schema.get("data_stream_designs", [])
    if not index_designs and not data_stream_designs:
        return {"status": "not_available"}
    tables = []
    for idx in index_designs:
        settings = idx.get("settings", {})
        tables.append(
            {
                "table_name": idx.get("index_name", ""),
                "aggregate_pattern": "search_index",
                "source_tables": idx.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": settings.get("number_of_shards", 0),
                "replicas": settings.get("number_of_replicas", 1),
                "field_count": len(idx.get("field_mappings", [])),
            }
        )
    for ds in data_stream_designs:
        ism = ds.get("ism_policy", {})
        ts = ds.get("index_template", {}).get("settings", {})
        tables.append(
            {
                "table_name": ds.get("data_stream_name", ""),
                "aggregate_pattern": "data_stream",
                "source_tables": ds.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": ts.get("number_of_shards", 0),
                "replicas": ts.get("number_of_replicas", 1),
                "ism_hot_days": ism.get("hot_phase_days"),
                "ism_delete_days": ism.get("delete_after_days"),
            }
        )
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": tables,
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": [],
    }


def _collect_trade_offs(data):
    trade_offs: list[dict] = []
    seen: set[str] = set()
    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        for t in schema.get("trade_offs", []):
            if isinstance(t, dict):
                # Structured TradeOff — ensure engine is set
                entry = {**t, "engine": t.get("engine") or engine}
                dedup_key = entry.get("description", str(entry))
            else:
                # Legacy string format — wrap into structured form
                entry = {
                    "description": str(t),
                    "impact": str(t),
                    "source_tables": [],
                    "target_tables": [],
                    "query_ids": [],
                    "engine": engine,
                }
                dedup_key = str(t)
            if dedup_key not in seen:
                seen.add(dedup_key)
                trade_offs.append(entry)
    return trade_offs


def _write_empty_report(store, database_name, job_id, assignment_version=0):
    output = {
        "job_id": job_id,
        "database_name": database_name,
        "agent_type": "referee-synthesis",
        "status": "completed",
        "ranking": [],
        "needs_deeper_analysis": False,
        "summary": "No analysis outputs were available for synthesis.",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if assignment_version > 0:
        key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    store.write_json(key, output)
