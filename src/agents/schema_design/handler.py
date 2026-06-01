"""Schema Design agent handler — dispatches to target-specific schema design agents.

Reads collector output and analysis output via ArtifactStore, writes them to local
temp files, sets COLLECTOR_OUTPUT_PATH and ANALYSIS_OUTPUT_PATH env vars, invokes
the target-specific Strands agent, and writes the output contract back via ArtifactStore.

Artifact paths (versioned when assignment_version > 0):
  Collector input: {database_name}/{job_id}/collector/output.json
  Analysis input:  {database_name}/{job_id}/analysis-{target_type}/analysis.json
  Assignment:      {database_name}/{job_id}/assignment/v{N}/assignment.json
  Schema output:   {database_name}/{job_id}/schema-{target_type}/v{N}/schema_output.json  (versioned)
                   {database_name}/{job_id}/schema-{target_type}/schema_output.json        (legacy)

Requirements: 6.1, 6.2, 6.3, 7.1, 7.4, 10.1
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime

from src.agents.interaction import read_answers, read_partial_output
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


def filter_collector_for_assignment(
    collector_output: dict,
    assignment: dict,
    target_engine: str,
    injected_query_ids: set[str] | None = None,
) -> dict:
    """Filter collector output to only include in-scope queries assigned to target_engine.

    Returns a new collector_output dict with:
    - queries filtered to only those assigned to target_engine with in_scope=True
    - tables filtered to only those referenced by the filtered queries

    Args:
        injected_query_ids: Additional query IDs to include regardless of assignment.
            Used by the post-schema router to inject rerouted queries.

    This is a pure function suitable for property testing.
    """
    # Build set of in-scope query IDs assigned to this engine
    in_scope_query_ids: set[str] = set()
    in_scope_tables: set[str] = set()

    for qa in assignment.get("query_assignments", []):
        if qa.get("assigned_engine") == target_engine and qa.get("in_scope", True):
            in_scope_query_ids.add(qa["query_id"])
            for table in qa.get("source_tables", []):
                in_scope_tables.add(table)

    # Include injected queries (from post-schema router cascade)
    if injected_query_ids:
        in_scope_query_ids |= injected_query_ids
        # Also include tables referenced by injected queries
        all_queries = collector_output.get("queries", {}).get("query_patterns", [])
        for q in all_queries:
            if q.get("query_id") in injected_query_ids:
                for table in q.get("tables_accessed", []):
                    in_scope_tables.add(table)

    # Filter queries
    original_queries = collector_output.get("queries", {}).get("query_patterns", [])
    filtered_queries = [q for q in original_queries if q.get("query_id") in in_scope_query_ids]

    # Filter tables to only those referenced by filtered queries
    original_tables = collector_output.get("database_schema", {}).get("tables", [])
    filtered_tables = [t for t in original_tables if t.get("table_id") in in_scope_tables]

    # Build filtered collector output preserving structure
    filtered = dict(collector_output)
    filtered["queries"] = dict(collector_output.get("queries", {}))
    filtered["queries"]["query_patterns"] = filtered_queries
    filtered["database_schema"] = dict(collector_output.get("database_schema", {}))
    filtered["database_schema"]["tables"] = filtered_tables

    return filtered


def prepare_schema_design_input(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> dict:
    """Prepare the LLM input payload for schema design (external/seam mode).

    Reads collector output, analysis output, and (when assignment_version > 0)
    assignment artifact from the store, filters collector to in-scope queries,
    and returns a dict ready to be serialised as the LLM input.

    Returns dict with keys:
      target_type, collector_output (filtered), analysis_output,
      database_name, job_id
    """
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        collector_output = filter_collector_for_assignment(
            collector_output, assignment, target_type
        )

    analysis_key = f"{database_name}/{job_id}/analysis-{target_type}/analysis.json"
    analysis_output = store.read_json(analysis_key)

    return {
        "target_type": target_type,
        "collector_output": collector_output,
        "analysis_output": analysis_output,
        "database_name": database_name,
        "job_id": job_id,
    }


def validate_schema_design_output(output: dict, target_type: str) -> dict:
    """Validate LLM schema design output against the engine-specific Pydantic contract.

    Returns ``{"valid": True}`` on success, or
    ``{"valid": False, "errors": [str(e)]}`` on validation failure.
    """
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

    _ENGINE_CONTRACTS: dict[str, type] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
    }

    contract_cls = _ENGINE_CONTRACTS.get(target_type)
    if contract_cls is None:
        return {"valid": False, "errors": [f"No contract registered for engine: {target_type}"]}

    try:
        contract_cls.model_validate(output)  # type: ignore[attr-defined]
        return {"valid": True}
    except Exception as exc:  # pydantic ValidationError
        return {"valid": False, "errors": [str(exc)]}


def finalize_schema_design(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> dict:
    """Finalise schema design after external LLM processing.

    Reads the LLM response written at
    ``{prefix}/llm_responses/schema_design_{target_type}.json``,
    validates it, writes the validated output to the versioned schema path,
    materialises query journey files, and returns a status dict.

    Returns:
      ``{"status": "complete", "output_path": <key>}`` on success, or
      ``{"status": "validation_failed", "errors": [...]}`` on validation failure.
    """
    prefix = f"{database_name}/{job_id}"
    version = assignment_version if assignment_version > 0 else 1

    llm_response_key = f"{prefix}/llm_responses/schema_design_{target_type}.json"
    output = store.read_json(llm_response_key)

    validation = validate_schema_design_output(output, target_type)
    if not validation["valid"]:
        return {"status": "validation_failed", "errors": validation["errors"]}

    output_key = f"{prefix}/schema-{target_type}/v{version}/schema_output.json"
    store.write_json(output_key, output)

    from src.agents.query_journey_materializer import materialize_design

    materialize_design(output, target_type, version, database_name, job_id, store)

    return {"status": "complete", "output_path": output_key}


def run_schema_design(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> None:
    """Run a schema design agent for the given target type.

    When assignment_version > 0, reads the assignment artifact and filters
    collector output to only include queries assigned to target_type with
    in_scope=True. When assignment_version == 0, passes all queries (legacy).

    When llm_mode == "external": prepares the LLM input payload, writes it to
    the store, and returns early (no Bedrock call). Default is "bedrock" which
    preserves the original behaviour unchanged.

    Requirements: 6.1, 6.2, 6.3, 10.1
    """
    # --- External LLM mode: write prepared input and return early ---
    if llm_mode == "external":
        llm_input = prepare_schema_design_input(
            job_id, database_name, target_type, store, assignment_version
        )
        version = assignment_version if assignment_version > 0 else 1
        prefix = f"{database_name}/{job_id}"
        input_key = f"{prefix}/schema-{target_type}/v{version}/llm_input.json"
        store.write_json(input_key, llm_input)
        print(f"[schema-design/{target_type}] external mode — input written to {input_key}")
        return

    import time

    start_time = time.time()
    agent_name = f"schema-{target_type}"

    print(f"[schema-design/{target_type}] Starting for {database_name}")

    # --- Check for answers from a previous exit-code-2 run ---
    answers = read_answers(store, database_name, job_id, agent_name)
    partial = read_partial_output(store, database_name, job_id, agent_name)
    if answers is not None:
        print(f"[schema-design/{target_type}] Resuming with answers from previous run")
    if partial is not None:
        print(f"[schema-design/{target_type}] Loaded partial output from previous run")

    # --- Read collector output via ArtifactStore ---
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)
    print(f"[schema-design/{target_type}] Loaded collector output")

    # --- Filter by assignment when versioned ---
    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        collector_output = filter_collector_for_assignment(
            collector_output, assignment, target_type
        )
        n_queries = len(collector_output.get("queries", {}).get("query_patterns", []))
        n_tables = len(collector_output.get("database_schema", {}).get("tables", []))
        print(
            f"[schema-design/{target_type}] Filtered to {n_queries} queries, "
            f"{n_tables} tables (assignment v{assignment_version})"
        )
    else:
        print(f"[schema-design/{target_type}] Legacy mode — using all queries")

    # --- Check if there's anything to design ---
    filtered_tables = collector_output.get("database_schema", {}).get("tables", [])
    if not filtered_tables and assignment_version > 0:
        print(f"[schema-design/{target_type}] No tables assigned — skipping schema design")
        placeholder = {
            "target_type": target_type,
            "status": "skipped",
            "reason": "No queries or tables assigned to this engine",
            "assignment_version": assignment_version,
        }
        if assignment_version > 0:
            out_key = (
                f"{database_name}/{job_id}/schema-{target_type}"
                f"/v{assignment_version}/schema_output.json"
            )
        else:
            out_key = f"{database_name}/{job_id}/schema-{target_type}/schema_output.json"
        store.write_json(out_key, placeholder)
        print(f"[schema-design/{target_type}] Placeholder written to {out_key}")
        return

    # --- Read analysis output via ArtifactStore ---
    analysis_key = f"{database_name}/{job_id}/analysis-{target_type}/analysis.json"
    analysis_output = store.read_json(analysis_key)
    print(f"[schema-design/{target_type}] Loaded analysis output")

    # --- Write to temp files for the Strands agent ---
    collector_raw = json.dumps(collector_output, indent=2, default=str).encode()
    analysis_raw = json.dumps(analysis_output, indent=2, default=str).encode()

    collector_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
    collector_tmp.write(collector_raw)
    collector_tmp.close()

    analysis_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
    analysis_tmp.write(analysis_raw)
    analysis_tmp.close()

    # Load decision trace for agents that need it (DocumentDB, OpenSearch)
    trace_tmp_path: str | None = None
    if target_type in ("documentdb", "opensearch"):
        trace_key = f"{database_name}/{job_id}/analysis-documentdb/decision-trace.json"
        try:
            trace_data = store.read_json(trace_key)
            trace_raw = json.dumps(trace_data, indent=2, default=str).encode()
            trace_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
            trace_tmp.write(trace_raw)
            trace_tmp.close()
            trace_tmp_path = trace_tmp.name
            os.environ["DECISION_TRACE_PATH"] = trace_tmp_path
            print(f"[schema-design/{target_type}] Loaded decision trace")
        except Exception as exc:
            print(f"[schema-design/{target_type}] Decision trace not found: {exc}")

    try:
        output_json, trace_json = _dispatch_schema_agent(
            target_type,
            collector_path=collector_tmp.name,
            analysis_path=analysis_tmp.name,
        )
    finally:
        os.unlink(collector_tmp.name)
        os.unlink(analysis_tmp.name)
        if trace_tmp_path:
            os.unlink(trace_tmp_path)
            os.environ.pop("DECISION_TRACE_PATH", None)

    # --- Write output via ArtifactStore ---
    output_data = json.loads(output_json)
    if assignment_version > 0:
        output_key = (
            f"{database_name}/{job_id}/schema-{target_type}"
            f"/v{assignment_version}/schema_output.json"
        )
    else:
        output_key = f"{database_name}/{job_id}/schema-{target_type}/schema_output.json"
    store.write_json(output_key, output_data)

    # Materialize query journey files (design section) — ADR-019
    from src.agents.query_journey_materializer import materialize_design

    schema_version = assignment_version if assignment_version > 0 else 1
    materialize_design(output_data, target_type, schema_version, database_name, job_id, store)

    # Write design trace via ArtifactStore
    if trace_json:
        trace_data_out = json.loads(trace_json)
        if assignment_version > 0:
            trace_out_key = (
                f"{database_name}/{job_id}/schema-{target_type}"
                f"/v{assignment_version}/design_trace.json"
            )
        else:
            trace_out_key = f"{database_name}/{job_id}/schema-{target_type}/design_trace.json"
        store.write_json(trace_out_key, trace_data_out)

    elapsed = time.time() - start_time
    print(
        f"[schema-design/{target_type}] ✅ Complete in {elapsed:.1f}s — "
        f"output written to {output_key}"
    )


def run_schema_split(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> None:
    """Split schema design input into groups and write per-group input files.

    This is the ECS entrypoint for the group-splitting step. Step Functions
    calls this before dispatching per-group schema design tasks.

    Requirements: 6.1
    """
    import time

    from src.agents.schema_design.group_splitter import split_schema_input

    start_time = time.time()
    print(f"[schema-split/{target_type}] Starting for {database_name}")

    # Read collector output
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    # Filter by assignment
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        collector_output = filter_collector_for_assignment(
            collector_output, assignment, target_type
        )
        queries = collector_output.get("queries", {}).get("query_patterns", [])
        print(
            f"[schema-split/{target_type}] Filtered to {len(queries)} queries "
            f"(assignment v{assignment_version})"
        )

    if not queries:
        print(f"[schema-split/{target_type}] No queries assigned — skipping split")
        return

    # Read analysis output
    analysis_key = f"{database_name}/{job_id}/analysis-{target_type}/analysis.json"
    analysis_output = store.read_json(analysis_key)

    # Derive artifact version: use assignment_version when set, else 1
    artifact_version = assignment_version if assignment_version > 0 else 1

    # Split into groups
    manifest = split_schema_input(
        job_id=job_id,
        database_name=database_name,
        engine=target_type,
        collector_output=collector_output,
        analysis_output=analysis_output,
        queries=queries,
        store=store,
        schema_version=artifact_version,
    )

    elapsed = time.time() - start_time
    print(
        f"[schema-split/{target_type}] ✅ Split into {manifest.total_groups} groups "
        f"({manifest.total_queries} queries) in {elapsed:.1f}s"
    )
    for g in manifest.groups:
        print(
            f"  Group {g.group_index:2d}: {g.query_count:4d} queries, "
            f"{g.table_count:3d} tables  [{g.group_name}]"
        )


def run_schema_merge(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> None:
    """Merge per-group schema drafts into a single schema output.

    This is the ECS entrypoint for the group-merging step. Step Functions
    calls this after all per-group schema design tasks have completed.

    Requirements: 6.3
    """
    import time

    from src.agents.schema_design.group_merger import ENGINE_LIST_FIELDS, merge_schema_groups

    start_time = time.time()
    print(f"[schema-merge/{target_type}] Starting for {database_name}")

    artifact_version = assignment_version if assignment_version > 0 else 1

    merged = merge_schema_groups(
        job_id=job_id,
        database_name=database_name,
        engine=target_type,
        store=store,
        schema_version=artifact_version,
    )

    # Consolidate per-group design traces into a single design_trace.json
    base_key = f"{database_name}/{job_id}/schema-{target_type}/v{artifact_version}"
    group_traces = []
    for key in store.list_prefix(f"{base_key}/design_trace_group_"):
        try:
            group_traces.append(store.read_json(key))
        except Exception:
            logger.debug("Skipping unreadable trace artifact: %s", key)
    if group_traces:
        combined_trace = {
            "total_groups": len(group_traces),
            "groups": group_traces,
        }
        store.write_json(f"{base_key}/design_trace.json", combined_trace)

    # Materialize query journey files (design section) — ADR-019
    from src.agents.query_journey_materializer import materialize_design

    materialize_design(merged, target_type, artifact_version, database_name, job_id, store)

    elapsed = time.time() - start_time

    # Print summary counts
    for field in ENGINE_LIST_FIELDS.get(target_type, []):
        items = merged.get(field, [])
        if items:
            print(f"[schema-merge/{target_type}]   {field}: {len(items)}")

    print(f"[schema-merge/{target_type}] ✅ Complete in {elapsed:.1f}s")


def run_schema_design_auto(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> None:
    """Run schema design with automatic group splitting for large workloads.

    If the number of in-scope queries exceeds MAX_GROUP_SIZE, splits into groups,
    runs schema design per group (in parallel), then merges. Otherwise falls back
    to the standard single-call path.

    This is the recommended entry point for local and orchestrator usage.
    """
    from src.agents.schema_design.group_splitter import MAX_GROUP_SIZE

    # Derive artifact version from assignment_version (synthesis reads v{N}/)
    artifact_version = assignment_version if assignment_version > 0 else 1

    # Count in-scope queries for this engine
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        filtered = filter_collector_for_assignment(collector_output, assignment, target_type)
        queries = filtered.get("queries", {}).get("query_patterns", [])
    else:
        queries = collector_output.get("queries", {}).get("query_patterns", [])

    if len(queries) <= MAX_GROUP_SIZE:
        print(
            f"[schema-design/{target_type}] {len(queries)} queries <= {MAX_GROUP_SIZE} "
            f"— running single-pass"
        )
        run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
        )
        return

    # Split into groups
    print(
        f"[schema-design/{target_type}] {len(queries)} queries > {MAX_GROUP_SIZE} "
        f"— splitting into groups"
    )
    run_schema_split(
        job_id,
        database_name,
        target_type,
        store,
        assignment_version=assignment_version,
    )

    # Read manifest to get group count
    manifest_key = (
        f"{database_name}/{job_id}/schema-{target_type}"
        f"/v{artifact_version}/groups_manifest.json"
    )
    manifest = store.read_json(manifest_key)
    groups = manifest.get("groups", [])

    if not groups:
        print(f"[schema-design/{target_type}] No groups produced — falling back to single-pass")
        run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
        )
        return

    # Run schema design per group (parallel)
    import time

    print(f"[schema-design/{target_type}] Designing {len(groups)} groups in parallel")
    start = time.time()

    def _design_group(group: dict) -> None:
        idx = group["group_index"]
        input_key = (
            f"{database_name}/{job_id}/schema-{target_type}"
            f"/v{artifact_version}/input_group_{idx}.json"
        )
        group_input = store.read_json(input_key)
        group_collector = group_input["collector_output"]
        group_analysis = group_input["analysis_output"]

        # Write group data to temp files for the agent
        import tempfile

        collector_raw = json.dumps(group_collector, indent=2, default=str).encode()
        analysis_raw = json.dumps(group_analysis, indent=2, default=str).encode()

        collector_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
        collector_tmp.write(collector_raw)
        collector_tmp.close()

        analysis_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
        analysis_tmp.write(analysis_raw)
        analysis_tmp.close()

        try:
            output_json, trace_json = _dispatch_schema_agent(
                target_type,
                collector_path=collector_tmp.name,
                analysis_path=analysis_tmp.name,
            )
        finally:
            os.unlink(collector_tmp.name)
            os.unlink(analysis_tmp.name)

        # Write group draft
        draft_key = (
            f"{database_name}/{job_id}/schema-{target_type}"
            f"/v{artifact_version}/schema_draft_group_{idx}.json"
        )
        store.write_json(draft_key, json.loads(output_json))

        # Write per-group trace
        if trace_json:
            trace_key = (
                f"{database_name}/{job_id}/schema-{target_type}"
                f"/v{artifact_version}/design_trace_group_{idx}.json"
            )
            store.write_json(trace_key, json.loads(trace_json))
        n_q = len(group_collector.get("queries", {}).get("query_patterns", []))
        print(
            f"[schema-design/{target_type}] Group {idx} done "
            f"({group['group_name']}, {n_q} queries)"
        )

    # Run groups in parallel (max 5 concurrent) — paths are passed as params
    # so there's no process-global env var contention between threads.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = min(5, len(groups))
    print(
        f"[schema-design/{target_type}] Running {len(groups)} groups (max {max_workers} parallel)"
    )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_design_group, g): g for g in groups}
        for future in as_completed(futures):
            group = futures[future]
            try:
                future.result()
            except Exception:
                logger.warning("Group %s failed", group.get("group_name", group.get("group_index")))
                raise

    elapsed = time.time() - start
    print(f"[schema-design/{target_type}] All {len(groups)} groups done in {elapsed:.1f}s")

    # Merge group drafts
    run_schema_merge(
        job_id, database_name, target_type, store, assignment_version=assignment_version
    )


def run_schema_design_with_injected(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    injected_query_ids: set[str],
    assignment_version: int = 0,
) -> None:
    """Run schema design for an engine with additional injected query IDs.

    Used by the post-schema router cascade to design schemas for queries that
    were rerouted from another engine. The injected queries bypass the normal
    assignment filter and are included alongside any already-assigned queries.
    """
    import time

    start_time = time.time()
    print(
        f"[schema-design/{target_type}] Starting CASCADE pass "
        f"with {len(injected_query_ids)} injected queries"
    )

    # Read collector output
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    # Filter by assignment + injected queries
    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        collector_output = filter_collector_for_assignment(
            collector_output,
            assignment,
            target_type,
            injected_query_ids=injected_query_ids,
        )
    else:
        # Legacy mode — include all queries (injected are already there)
        pass

    n_queries = len(collector_output.get("queries", {}).get("query_patterns", []))
    n_tables = len(collector_output.get("database_schema", {}).get("tables", []))
    print(
        f"[schema-design/{target_type}] Cascade input: {n_queries} queries, "
        f"{n_tables} tables ({len(injected_query_ids)} injected)"
    )

    if n_queries == 0:
        print(f"[schema-design/{target_type}] No queries for cascade — skipping")
        return

    # Read analysis output
    analysis_key = f"{database_name}/{job_id}/analysis-{target_type}/analysis.json"
    analysis_output = store.read_json(analysis_key)

    # Write to temp files and invoke agent
    collector_raw = json.dumps(collector_output, indent=2, default=str).encode()
    analysis_raw = json.dumps(analysis_output, indent=2, default=str).encode()

    collector_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
    collector_tmp.write(collector_raw)
    collector_tmp.close()

    analysis_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
    analysis_tmp.write(analysis_raw)
    analysis_tmp.close()

    trace_tmp_path: str | None = None
    if target_type in ("documentdb", "opensearch"):
        trace_key = f"{database_name}/{job_id}/analysis-documentdb/decision-trace.json"
        try:
            trace_data = store.read_json(trace_key)
            trace_raw = json.dumps(trace_data, indent=2, default=str).encode()
            trace_tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
            trace_tmp.write(trace_raw)
            trace_tmp.close()
            trace_tmp_path = trace_tmp.name
            os.environ["DECISION_TRACE_PATH"] = trace_tmp_path
        except Exception:  # noqa: B110
            pass  # nosec B110

    try:
        output_json, trace_json = _dispatch_schema_agent(
            target_type,
            collector_path=collector_tmp.name,
            analysis_path=analysis_tmp.name,
        )
    finally:
        os.unlink(collector_tmp.name)
        os.unlink(analysis_tmp.name)
        if trace_tmp_path:
            os.unlink(trace_tmp_path)
            os.environ.pop("DECISION_TRACE_PATH", None)

    # Write output — append to existing schema output (merge unsupported + access patterns)
    output_data = json.loads(output_json)
    if assignment_version > 0:
        output_key = (
            f"{database_name}/{job_id}/schema-{target_type}"
            f"/v{assignment_version}/schema_output.json"
        )
    else:
        output_key = f"{database_name}/{job_id}/schema-{target_type}/v1/schema_output.json"

    # Merge with existing schema output if present
    if store.exists(output_key):
        existing = store.read_json(output_key)
        output_data = _merge_cascade_output(existing, output_data, target_type)
        print(f"[schema-design/{target_type}] Merged cascade output with existing schema")

    store.write_json(output_key, output_data)

    elapsed = time.time() - start_time
    print(
        f"[schema-design/{target_type}] ✅ Cascade complete in {elapsed:.1f}s — "
        f"output written to {output_key}"
    )


def _merge_cascade_output(existing: dict, cascade: dict, engine: str) -> dict:
    """Merge cascade schema output into existing schema output.

    Concatenates list fields (access_patterns, unsupported_patterns, trade_offs)
    and deduplicates by pattern_id/query_ids.
    """
    from src.agents.schema_design.group_merger import ENGINE_LIST_FIELDS

    merged = dict(existing)
    list_fields = ENGINE_LIST_FIELDS.get(engine, [])

    for field in list_fields:
        existing_items = existing.get(field, [])
        cascade_items = cascade.get(field, [])
        if cascade_items:
            merged[field] = existing_items + cascade_items

    return merged


def _dispatch_schema_agent(
    target_type: str,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
) -> tuple[str, str | None]:
    """Route to the correct schema design agent. Returns (output_json, trace_json).

    Args:
        target_type: Engine type (dynamodb, opensearch, documentdb).
        collector_path: Path to collector output JSON (preferred over env var).
        analysis_path: Path to analysis output JSON (preferred over env var).
        revision_context_path: Optional path to revision context JSON.
    """
    match target_type:
        case "dynamodb":
            from src.tools.schema.dynamodb_schema_agent import run_dynamodb_schema_agent

            result, trace = run_dynamodb_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return result.model_dump_json(indent=2), json.dumps(trace, indent=2)

        case "documentdb":
            from src.tools.schema.documentdb_schema_agent import run_documentdb_schema_agent

            docdb_result, docdb_trace = run_documentdb_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return docdb_result.model_dump_json(indent=2), json.dumps(docdb_trace, indent=2)

        case "opensearch":
            from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

            os_result, os_trace = run_opensearch_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return os_result.model_dump_json(indent=2), json.dumps(os_trace, indent=2)

        case "elasticache":
            from src.tools.schema.elasticache_schema_agent import run_elasticache_schema_agent

            ec_result, ec_trace = run_elasticache_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return ec_result.model_dump_json(indent=2), json.dumps(ec_trace, indent=2)

        case _:
            print(
                f"WARNING: {target_type} schema design agent not yet "
                "implemented — writing placeholder"
            )
            placeholder = {
                "target_type": target_type,
                "status": "not_implemented",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return json.dumps(placeholder, indent=2), None
