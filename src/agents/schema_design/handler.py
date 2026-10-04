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
from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES, is_write_query
from src.agents.schema_design.scope import (
    EMPTY_REPORT,
    ScopeReport,
    apply_scope_violations,
    assess_schema_scope,
)
from src.contracts.aurora_design_delta import is_design_delta
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import engine_scope, read_assignment

logger = logging.getLogger(__name__)

_DEFAULT_GROUP_CONCURRENCY = 5


def _group_concurrency() -> int:
    """Max concurrent group designs, overridable via SCHEMA_GROUP_CONCURRENCY.

    Bounded below at 1. The effective ceiling is the account's Bedrock quota
    shared across all engines running in parallel, not the thread count, so this
    is a tuning knob rather than a pure speedup.
    """
    raw = os.environ.get("SCHEMA_GROUP_CONCURRENCY")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_GROUP_CONCURRENCY


# Schema design has no deterministic designer: every engine's agent (including
# the Aurora ones, whose deterministic draft is only an input to the designer)
# needs a model. With llm_mode="none" the phase is therefore skipped, exactly as
# the deterministic pipeline does (it stops before schema design and synthesis
# runs without schema outputs). Nothing is written, so a skipped engine is never
# mistaken for a designed one (issue #281).
SCHEMA_DESIGN_SKIPPED_NONE_MODE = (
    "schema design skipped (llm_mode=none): every schema designer needs a model; "
    "rerun with --llm-mode bedrock or external to design target schemas"
)


def _skip_without_model(target_type: str) -> ScopeReport:
    print(f"[schema-design/{target_type}] {SCHEMA_DESIGN_SKIPPED_NONE_MODE}")
    return EMPTY_REPORT


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
    scope = engine_scope(assignment, target_engine)
    in_scope_query_ids: set[str] = set(scope.query_ids)
    in_scope_tables: set[str] = set(scope.source_tables)

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

    if target_engine in CACHE_OVERLAY_ENGINES:
        writes = _invalidation_writes(original_queries, assignment, in_scope_tables)
        if writes:
            filtered["cache_invalidation_context"] = {
                "note": INVALIDATION_CONTEXT_NOTE,
                "write_queries": writes,
            }

    return filtered


INVALIDATION_CONTEXT_NOTE = (
    "Read-only context, not design scope: the owner engines' write queries on the "
    "cached tables. Use their query_id values only in "
    "cache_invalidation[].source_write_query_ids; never design keys or access "
    "patterns for them."
)


def _invalidation_writes(
    queries: list[dict], assignment: dict, cached_tables: set[str]
) -> list[dict]:
    """The owners' in-scope writes on the tables the cache fronts (#296).

    A cache-aside design must say which writes invalidate each key, but the cache's
    scope is reads only. These writes are passed as marked context so
    ``source_write_query_ids`` can be filled; the scope check never treats them as
    the cache's design scope.
    """
    in_scope = {
        qa.get("query_id")
        for qa in assignment.get("query_assignments") or []
        if isinstance(qa, dict) and qa.get("in_scope", True)
    }
    out = []
    for q in queries:
        if q.get("query_id") not in in_scope or not is_write_query(q):
            continue
        if not cached_tables & set(q.get("tables_accessed") or []):
            continue
        out.append(
            {
                "query_id": q.get("query_id"),
                "query_type": q.get("query_type"),
                "query_text": q.get("query_text"),
                "tables_accessed": q.get("tables_accessed") or [],
                "calls_per_second": q.get("calls_per_second"),
                "context_only": True,
            }
        )
    return out


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
    from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
    from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract

    _ENGINE_CONTRACTS: dict[str, type] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
        "aurora_postgresql": AuroraPostgresqlModelOutputContract,
        "aurora_mysql": AuroraMySQLModelOutputContract,
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

    A contract-valid design that references source tables or query IDs outside
    the engine's scope in the assignment (issue #203) is still written, with
    ``validation_passed=false`` and the scope messages appended to
    ``validation_failures``, and reported as ``validation_failed``.

    Aurora (#273): the response is a design delta, merged into the rebuilt
    deterministic draft. A legacy full contract is converted into a delta
    first, so its ``generated_ddl`` / ``foreign_keys`` text is never written:
    the DDL always comes from the draft plus validated changes, and anything
    that does not convert fails validation.

    Returns:
      ``{"status": "complete", "output_path": <key>}`` on success,
      ``{"status": "validation_failed", "errors": [...]}`` on contract validation
      failure (nothing written), or
      ``{"status": "validation_failed", "errors": [...], "output_path": <key>}``
      when the design is out of scope. Scope ``warnings`` (out-of-scope IDs
      only in ``unsupported_patterns``) are added when present.
    """
    prefix = f"{database_name}/{job_id}"
    version = assignment_version if assignment_version > 0 else 1

    llm_response_key = f"{prefix}/llm_responses/schema_design_{target_type}.json"
    output = store.read_json(llm_response_key)

    delta_summary: dict | None = None
    merge_warnings: list[str] = []
    if target_type in _NON_GROUPED_ENGINES:
        # Aurora: whatever the response shape, the written DDL is regenerated
        # from the draft; a full contract is first converted into a delta, so
        # none of its DDL text is used (#273).
        full_contract = not is_design_delta(output)
        if full_contract:
            logger.warning("%s (%s, job %s)", FULL_CONTRACT_DEPRECATION, target_type, job_id)
        merged = _merge_aurora_delta(
            store,
            database_name,
            job_id,
            target_type,
            output,
            assignment_version,
            full_contract=full_contract,
        )
        merge_warnings = ([FULL_CONTRACT_DEPRECATION] if full_contract else []) + merged.warnings
        if merged.output is None:
            result: dict = {"status": "validation_failed", "errors": merged.errors}
            if merge_warnings:
                result["warnings"] = merge_warnings
            return result
        output, delta_summary = merged.output, merged.summary

    validation = validate_schema_design_output(output, target_type)
    if not validation["valid"]:
        return {"status": "validation_failed", "errors": validation["errors"]}

    output, report = apply_schema_scope(
        store, database_name, job_id, target_type, output, assignment_version
    )

    output_key = f"{prefix}/schema-{target_type}/v{version}/schema_output.json"
    store.write_json(output_key, output)

    status = scope_status(report, output_key)
    if delta_summary is not None:
        status["delta_summary"] = delta_summary
    if merge_warnings:
        status["warnings"] = [*status.get("warnings", []), *merge_warnings]
    return status


FULL_CONTRACT_DEPRECATION = (
    "Deprecated: the Aurora response is a full output contract. Write an "
    'AuroraDesignDeltaContract (delta_version "1.0") instead; full-contract '
    "responses will stop being accepted in a future release."
)

DRAFT_CHANGED = (
    "The draft changed since the request was prepared (draft_fingerprint mismatch): "
    "re-run step 1 (--llm-mode external), re-read the request and rewrite the delta."
)


def _merge_aurora_delta(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    target_type: str,
    delta: dict,
    assignment_version: int,
    *,
    full_contract: bool = False,
):
    """Rebuild the deterministic Aurora draft and merge an LLM delta into it (#273).

    ``full_contract``: ``delta`` is a legacy full output contract; it is
    converted into a delta first (``full_contract_to_delta``) and anything that
    does not convert fails validation.

    The draft is rebuilt from the same assignment-filtered input the external
    request was prepared from. The request's ``draft_fingerprint`` must match
    the rebuilt base, so a delta is never merged into a different draft than
    the one its view described.
    """
    from src.tools.schema.aurora_common.delta_merge import (
        MergeResult,
        base_from_outputs,
        full_contract_to_delta,
        merge_design_delta,
    )

    inputs = prepare_schema_design_input(
        job_id=job_id,
        database_name=database_name,
        target_type=target_type,
        store=store,
        assignment_version=assignment_version,
    )
    base, _, _ = base_from_outputs(
        target_type, inputs["collector_output"], inputs["analysis_output"]
    )
    request_key = f"{database_name}/{job_id}/llm_requests/schema_design_{target_type}.json"
    if store.exists(request_key):
        try:
            expected = store.read_json(request_key).get("draft_fingerprint")
        except (OSError, ValueError):
            expected = None
        if expected is not None and expected != base.fingerprint():
            return MergeResult(None, [DRAFT_CHANGED], {})
    if full_contract:
        validation = validate_schema_design_output(delta, target_type)
        if not validation["valid"]:
            return MergeResult(None, validation["errors"], {})
        conversion_warnings: list[str] = []
        converted, errors = full_contract_to_delta(base, delta, conversion_warnings)
        if converted is None:
            return MergeResult(None, errors, {})
        merged = merge_design_delta(base, converted)
        merged.warnings = [*conversion_warnings, *merged.warnings]
        return merged
    return merge_design_delta(base, delta)


def scope_status(report: ScopeReport, output_key: str) -> dict:
    """Status dict for a written output: ``validation_failed`` on scope violations."""
    result: dict = {"status": "complete", "output_path": output_key}
    if report.violations:
        result = {
            "status": "validation_failed",
            "errors": report.violations,
            "output_path": output_key,
        }
    if report.warnings:
        result["warnings"] = report.warnings
    return result


def check_schema_scope(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    target_type: str,
    schema_output: dict,
    assignment_version: int,
) -> ScopeReport:
    """Check ``schema_output`` against the scope assignment ``v{N}`` gives the engine.

    Empty report when there is no assignment to check against (legacy version
    0, or the artifact is absent) or the check itself fails unexpectedly (an
    unreadable artifact, a shape nobody anticipated): that is logged and the
    design is not failed on it. Never raises.
    """
    try:
        assignment = read_assignment(store, database_name, job_id, assignment_version)
        if assignment is None:
            return EMPTY_REPORT
        report = assess_schema_scope(target_type, schema_output, assignment)
    except Exception as exc:  # never fail a design because the check broke
        logger.warning(
            "Scope check skipped for %s (assignment v%s): %s", target_type, assignment_version, exc
        )
        return EMPTY_REPORT
    for warning in report.warnings:
        logger.warning(warning)
    return report


def apply_schema_scope(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    target_type: str,
    schema_output: dict,
    assignment_version: int,
) -> tuple[dict, ScopeReport]:
    """Run :func:`check_schema_scope` and return the output with its verdict applied.

    Violations set ``validation_passed=false`` and are appended to
    ``validation_failures``; stale scope messages from an earlier check are
    dropped first (see :func:`apply_scope_violations`).
    """
    report = check_schema_scope(
        store, database_name, job_id, target_type, schema_output, assignment_version
    )
    if not isinstance(schema_output, dict):
        return schema_output, report
    return apply_scope_violations(schema_output, report.violations), report


def run_schema_design(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> ScopeReport:
    """Run a schema design agent for the given target type.

    When assignment_version > 0, reads the assignment artifact and filters
    collector output to only include queries assigned to target_type with
    in_scope=True. When assignment_version == 0, passes all queries (legacy).

    When llm_mode == "external": prepares the LLM input payload, writes it to
    the store, and returns early (no Bedrock call). Default is "bedrock" which
    preserves the original behaviour unchanged. When llm_mode == "none": no
    model is available, so nothing is designed or written (issue #281).

    The output is checked against the assignment's scope before it is written
    (issue #203); the returned :class:`ScopeReport` carries any violations,
    which are also recorded in the output's ``validation_failures``.

    Requirements: 6.1, 6.2, 6.3, 10.1
    """
    if llm_mode == "none":
        return _skip_without_model(target_type)

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
        return EMPTY_REPORT

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
        return EMPTY_REPORT

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
    output_data, report = apply_schema_scope(
        store, database_name, job_id, target_type, output_data, assignment_version
    )
    store.write_json(output_key, output_data)

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
    if report.violations:
        print(
            f"[schema-design/{target_type}] ❌ {len(report.violations)} scope violation(s) "
            f"in {elapsed:.1f}s — output written to {output_key}:"
        )
        for message in report.violations:
            print(f"  - {message}")
    else:
        print(
            f"[schema-design/{target_type}] ✅ Complete in {elapsed:.1f}s — "
            f"output written to {output_key}"
        )
    return report


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
    co_dependency_groups: list[list[str]] | None = None
    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        assignment = store.read_json(assignment_key)
        # Carry the assignment's co-dependency groups into clustering so
        # JOIN-related queries are designed together (ADR-027 amendment).
        co_dependency_groups = assignment.get("co_dependency_groups") or None
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
        co_dependency_groups=co_dependency_groups,
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
) -> ScopeReport:
    """Merge per-group schema drafts into a single schema output.

    This is the ECS entrypoint for the group-merging step. Step Functions
    calls this after all per-group schema design tasks have completed.

    The merged output is checked against the assignment's scope for the engine
    (issue #203). Violations set ``validation_passed=false``, are appended to
    ``validation_failures`` in the written output, and are returned in the
    :class:`ScopeReport` (empty when the merged design stays in scope).

    DynamoDB merge failures (true conflicts: one table name for two designs,
    contradictory key templates for the same entity; issue #223, see
    ``dynamodb_merge``) are returned in the report's ``violations`` after the
    scope violations, so callers report ``validation_failed`` for either. The
    merge's review notes (a source table several groups modelled independently)
    are returned as ``warnings`` and never fail the merge.

    Requirements: 6.3
    """
    import time

    from src.agents.schema_design.group_merger import (
        ENGINE_LIST_FIELDS,
        merge_failures,
        merge_schema_groups,
        merge_warnings,
    )

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

    # merge_schema_groups also consolidates the per-group design traces into
    # design_trace.json (with DynamoDB pattern IDs renumbered like the drafts).
    base_key = f"{database_name}/{job_id}/schema-{target_type}/v{artifact_version}"

    checked, report = apply_schema_scope(
        store, database_name, job_id, target_type, merged, assignment_version
    )
    if checked != merged:
        merged = checked
        store.write_json(f"{base_key}/schema_output.json", merged)
    violations = report.violations + merge_failures(merged)
    report = ScopeReport(violations, report.warnings + merge_warnings(merged))

    elapsed = time.time() - start_time

    # Print summary counts
    for field in ENGINE_LIST_FIELDS.get(target_type, []):
        items = merged.get(field, [])
        if items:
            print(f"[schema-merge/{target_type}]   {field}: {len(items)}")

    if violations:
        print(
            f"[schema-merge/{target_type}] ❌ {len(violations)} validation failure(s) "
            f"in {elapsed:.1f}s:"
        )
        for message in violations:
            print(f"  - {message}")
    else:
        print(f"[schema-merge/{target_type}] ✅ Complete in {elapsed:.1f}s")
    return report


# Engines that always design single-pass and never split into groups. Aurora's
# output is a single ``generated_ddl`` script plus table definitions that do not
# merge from independently designed groups, and a relational engine does not gain
# from query grouping the way a remodeling target does (ADR-027 amendment).
_NON_GROUPED_ENGINES: frozenset[str] = frozenset({"aurora_postgresql", "aurora_mysql"})


def run_schema_design_auto(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> ScopeReport:
    """Run schema design with automatic group splitting for large workloads.

    If the number of in-scope queries exceeds MAX_GROUP_SIZE, splits into groups,
    runs schema design per group (in parallel), then merges. Otherwise falls back
    to the standard single-call path. Aurora engines always run single-pass (see
    ``_NON_GROUPED_ENGINES``).

    This is the recommended entry point for local and orchestrator usage.

    Returns the scope report of the written output (issue #203): violations
    mean the design was written with ``validation_passed=false``.

    ``llm_mode`` (issue #281): ``"bedrock"`` (default) designs as described
    above; ``"none"`` skips the engine without a model call or any artifact;
    ``"external"`` hands off to :func:`run_schema_design`, which writes the
    prepared LLM input and returns (group splitting for external runs is the
    explicit ``scripts/run_schema_design.py --split`` step).
    """
    from src.agents.schema_design.group_splitter import MAX_GROUP_SIZE

    if llm_mode == "none":
        return _skip_without_model(target_type)
    if llm_mode == "external":
        return run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
            llm_mode=llm_mode,
        )

    # Aurora stays single-pass (ADR-027 amendment): its generated_ddl script does
    # not merge across groups and a relational engine gains nothing from grouping.
    if target_type in _NON_GROUPED_ENGINES:
        print(f"[schema-design/{target_type}] Aurora engine — running single-pass (no grouping)")
        return run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
            llm_mode=llm_mode,
        )

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
        return run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
            llm_mode=llm_mode,
        )

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
        f"{database_name}/{job_id}/schema-{target_type}/v{artifact_version}/groups_manifest.json"
    )
    manifest = store.read_json(manifest_key)
    groups = manifest.get("groups", [])

    if not groups:
        print(f"[schema-design/{target_type}] No groups produced — falling back to single-pass")
        return run_schema_design(
            job_id,
            database_name,
            target_type,
            store,
            assignment_version=assignment_version,
            llm_mode=llm_mode,
        )

    # Run schema design per group (parallel)
    import time

    print(f"[schema-design/{target_type}] Designing {len(groups)} groups in parallel")
    start = time.time()

    def _design_group(group: dict) -> None:
        idx = group["group_index"]
        draft_key = (
            f"{database_name}/{job_id}/schema-{target_type}"
            f"/v{artifact_version}/schema_draft_group_{idx}.json"
        )
        # Group-level resume: if this group's draft already exists, it was designed
        # on a prior (possibly recycled) run — skip it. The final schema_output.json
        # is only merged once every group finishes, so without this a recycle
        # mid-run would redo all groups from scratch (the heaviest engines split
        # into dozens), and under a tight Bedrock quota that re-work is exactly what
        # we cannot afford. Draft writes are per-group, so completed groups persist.
        if store.exists(draft_key):
            print(f"[schema-design/{target_type}] Group {idx} already designed — reusing draft")
            return
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

        # Write group draft (draft_key computed at the top for the resume check).
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
            f"[schema-design/{target_type}] Group {idx} done ({group['group_name']}, {n_q} queries)"
        )

    # Run groups in parallel — paths are passed as params so there's no
    # process-global env var contention between threads. The cap is tunable via
    # SCHEMA_GROUP_CONCURRENCY (default 5): under a small shared Bedrock quota,
    # every engine's group workers compete for the same rate limit, so the useful
    # ceiling is bounded by quota, not threads. Lower it to reduce throttling;
    # raise it only if the account quota can absorb more concurrent calls.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = min(_group_concurrency(), len(groups))
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
    return run_schema_merge(
        job_id, database_name, target_type, store, assignment_version=assignment_version
    )


def run_schema_design_with_injected(
    job_id: str,
    database_name: str,
    target_type: str,
    store: ArtifactStore,
    injected_query_ids: set[str],
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> None:
    """Run schema design for an engine with additional injected query IDs.

    Used by the post-schema router cascade to design schemas for queries that
    were rerouted from another engine. The injected queries bypass the normal
    assignment filter and are included alongside any already-assigned queries.

    With ``llm_mode="none"`` the cascade is skipped without a model call
    (issue #281).
    """
    if llm_mode == "none":
        _skip_without_model(target_type)
        return
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
    # The cases below are the implemented designers; keep them in sync with
    # core.IMPLEMENTED_SCHEMA_DESIGNERS (the orchestrator skips engines absent
    # from that set before dispatch). Anything else falls through to the
    # not_implemented placeholder.
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

        case "aurora_postgresql":
            from src.tools.schema.aurora_postgresql_schema_agent import (
                run_aurora_postgresql_schema_agent,
            )

            pg_result, pg_trace = run_aurora_postgresql_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return pg_result.model_dump_json(indent=2), json.dumps(pg_trace, indent=2)

        case "aurora_mysql":
            from src.tools.schema.aurora_mysql_schema_agent import run_aurora_mysql_schema_agent

            mysql_result, mysql_trace = run_aurora_mysql_schema_agent(
                collector_path=collector_path,
                analysis_path=analysis_path,
                revision_context_path=revision_context_path,
            )
            return mysql_result.model_dump_json(indent=2), json.dumps(mysql_trace, indent=2)

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
