"""Schema design revision handler — core orchestration functions for the revision loop.

Responsibilities:
- build_revision_context: translates a SchemaRevisionRequest into plain-text instruction
  dicts that the schema design LLM prompt can consume directly.
- resolve_table_drops: cascades TableModification(action="drop") entries into the set of
  access pattern IDs that must be excluded because their source table is being removed.
- determine_redesign_scope: given a groups manifest and a set of affected query/pattern IDs,
  returns the set of group_index values whose schema must be regenerated.
- generate_changelog: diffs two schema_output dicts (previous vs current) and produces a
  list[ChangelogEntry] describing what was added, removed, or modified.
- execute_revision: end-to-end revision pipeline — resolve, apply, verify, save.

Pure helper functions have no I/O. execute_revision does I/O via ArtifactStore.
"""

from __future__ import annotations

import logging

from src.contracts.schema_revision_models import (
    ChangelogEntry,
    PatternAction,
    SchemaRevisionRequest,
    SchemaVersionMeta,
    TableModification,
    VerificationResult,
)
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_all_pattern_ids(schema_output: dict) -> dict[str, dict]:
    """Return a mapping of pattern_id → pattern dict across all design collections.

    Checks access_patterns, index_designs, and collection_designs.
    """
    patterns: dict[str, dict] = {}
    for ap in schema_output.get("access_patterns", []):
        pid = ap.get("pattern_id")
        if pid:
            patterns[pid] = ap
    for idx in schema_output.get("index_designs", []):
        pid = idx.get("pattern_id")
        if pid:
            patterns[pid] = idx
    for col in schema_output.get("collection_designs", []):
        pid = col.get("pattern_id")
        if pid:
            patterns[pid] = col
    return patterns


def _all_designs(schema_output: dict) -> list[dict]:
    """Flatten access_patterns, index_designs, and collection_designs into one list."""
    result: list[dict] = (
        schema_output.get("access_patterns", [])
        + schema_output.get("index_designs", [])
        + schema_output.get("collection_designs", [])
    )
    return result


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def build_revision_context(
    request: SchemaRevisionRequest,
    current_schema: dict,
) -> dict:
    """Translate a SchemaRevisionRequest into LLM-ready instruction strings.

    Returns a dict with four string keys:

    - exclusion_instructions: patterns the schema agent must NOT include
      (DROP actions and the source-engine side of REASSIGN actions).
    - customer_instructions: annotations/notes the agent should respect
      (NOTE actions with their note text).
    - new_patterns_instructions: descriptions and metadata for brand-new
      patterns the agent should design from scratch.
    - reassignment_instructions: patterns that are moving to another engine,
      describing where they went and why.

    Args:
        request: The customer's revision request.
        current_schema: The current schema_output dict (available for context
            if the caller wants to enrich instructions; not mutated).
    """
    exclusion_lines: list[str] = []
    customer_lines: list[str] = []
    reassignment_lines: list[str] = []

    from src.api.services.input_sanitizer import wrap_customer_text

    for mod in request.pattern_modifications:
        if mod.action == PatternAction.DROP:
            exclusion_lines.append(
                f"Pattern '{mod.pattern_id}' has been removed by the customer and must not "
                "appear in the redesigned schema."
            )

        elif mod.action == PatternAction.NOTE:
            note_text = mod.note or "(no additional note provided)"
            customer_lines.append(f"Pattern '{mod.pattern_id}': {wrap_customer_text(note_text)}")

        elif mod.action == PatternAction.REASSIGN:
            target = mod.target_engine or "unknown"
            exclusion_lines.append(
                f"Pattern '{mod.pattern_id}' has been reassigned to '{target}' and must be "
                "removed from this engine's schema (it will be handled by the target engine)."
            )
            note_context = f" Note: {wrap_customer_text(mod.note)}" if mod.note else ""
            reassignment_lines.append(
                f"Pattern '{mod.pattern_id}' → engine '{target}'.{note_context}"
            )

    # New patterns instructions
    new_pattern_lines: list[str] = []
    for np in request.new_patterns:
        tables_str = ", ".join(np.source_tables) if np.source_tables else "(none specified)"
        rps_info = ""
        if np.estimated_reads_per_second is not None:
            rps_info += f", reads/s: {np.estimated_reads_per_second}"
        if np.estimated_writes_per_second is not None:
            rps_info += f", writes/s: {np.estimated_writes_per_second}"
        context_line = f" Context: {wrap_customer_text(np.context)}" if np.context else ""
        new_pattern_lines.append(
            f"- Description: {wrap_customer_text(np.description)}\n"
            f"  Target engine: {np.target_engine}\n"
            f"  Source tables: {tables_str}{rps_info}{context_line}"
        )

    return {
        "exclusion_instructions": "\n".join(exclusion_lines),
        "customer_instructions": "\n".join(customer_lines),
        "new_patterns_instructions": "\n".join(new_pattern_lines),
        "reassignment_instructions": "\n".join(reassignment_lines),
    }


def resolve_table_drops(
    table_modifications: list[TableModification],
    schema_output: dict,
) -> set[str]:
    """Return the set of pattern IDs affected by table DROP modifications.

    For each TableModification with action="drop", every design item
    (access_patterns, index_designs, collection_designs) whose table_name
    matches the dropped table_id is included in the returned set.

    Args:
        table_modifications: List of table-level modification instructions.
        schema_output: The current schema_output dict to cascade against.

    Returns:
        Set of pattern_id strings that belong to dropped tables.
    """
    if not table_modifications:
        return set()

    dropped_tables: set[str] = {mod.table_id for mod in table_modifications if mod.action == "drop"}

    if not dropped_tables:
        return set()

    affected: set[str] = set()
    for design in _all_designs(schema_output):
        table_name = design.get("table_name")
        pid = design.get("pattern_id")
        if table_name in dropped_tables and pid:
            affected.add(pid)

    return affected


def determine_redesign_scope(
    groups_manifest: dict,
    affected_query_ids: set[str],
) -> set[int]:
    """Return the set of group_index values whose schema must be regenerated.

    A group is in scope when:
    1. Its query_ids overlap with affected_query_ids (directly affected), OR
    2. It shares one or more tables with a directly-affected group (dependency).

    Args:
        groups_manifest: Dict with a "groups" list, each item containing:
            group_index (int), group_name (str), query_ids (list[str]),
            tables (list[str]).
        affected_query_ids: Set of query IDs known to be affected by the revision.

    Returns:
        Set of group_index integers that must be re-run.
    """
    groups: list[dict] = groups_manifest.get("groups", [])

    if not groups or not affected_query_ids:
        return set()

    # Pass 1: find directly-affected groups and their tables
    directly_affected: set[int] = set()
    affected_tables: set[str] = set()

    for group in groups:
        query_ids = set(group.get("query_ids", []))
        if query_ids & affected_query_ids:
            idx = group["group_index"]
            directly_affected.add(idx)
            affected_tables.update(group.get("tables", []))

    if not directly_affected:
        return set()

    # Pass 2: pull in groups that share tables with directly-affected groups
    in_scope = set(directly_affected)
    for group in groups:
        idx = group["group_index"]
        if idx in in_scope:
            continue
        group_tables = set(group.get("tables", []))
        if group_tables & affected_tables:
            in_scope.add(idx)

    return in_scope


def generate_changelog(
    previous_output: dict,
    current_output: dict,
    engine: str,
) -> list[ChangelogEntry]:
    """Diff two schema_output dicts and return a list of ChangelogEntry objects.

    Compares access_patterns (identified by pattern_id) between the two outputs:
    - Pattern in previous but not current → change_type="removed"
    - Pattern in current but not previous → change_type="added"
    - Pattern in both but with different content → change_type="modified"
    - Same content → no entry

    Args:
        previous_output: The prior schema_output dict.
        current_output: The new schema_output dict.
        engine: Engine identifier (e.g. "dynamodb") included in descriptions.

    Returns:
        List of ChangelogEntry instances, one per changed pattern.
    """
    prev_patterns = _collect_all_pattern_ids(previous_output)
    curr_patterns = _collect_all_pattern_ids(current_output)

    prev_ids = set(prev_patterns.keys())
    curr_ids = set(curr_patterns.keys())

    entries: list[ChangelogEntry] = []

    # Removed patterns
    for pid in sorted(prev_ids - curr_ids):
        entries.append(
            ChangelogEntry(
                change_type="removed",
                entity_type="access_pattern",
                entity_id=pid,
                description=(
                    f"Pattern '{pid}' was present in the previous {engine} schema "
                    "but is absent from the current version — it was dropped or reassigned."
                ),
                from_engine=engine,
                to_engine=None,
            )
        )

    # Added patterns
    for pid in sorted(curr_ids - prev_ids):
        entries.append(
            ChangelogEntry(
                change_type="added",
                entity_type="access_pattern",
                entity_id=pid,
                description=(f"Pattern '{pid}' is new in the current {engine} schema."),
                from_engine=None,
                to_engine=engine,
            )
        )

    # Modified patterns (present in both but content differs)
    for pid in sorted(prev_ids & curr_ids):
        if prev_patterns[pid] != curr_patterns[pid]:
            entries.append(
                ChangelogEntry(
                    change_type="modified",
                    entity_type="access_pattern",
                    entity_id=pid,
                    description=(
                        f"Pattern '{pid}' was updated in the {engine} schema — "
                        "one or more fields changed between versions."
                    ),
                    from_engine=engine,
                    to_engine=engine,
                )
            )

    return entries


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VerificationError(Exception):
    """Raised when revision verification produces hard errors."""

    def __init__(self, result: VerificationResult):
        self.result = result
        super().__init__(f"Verification failed: {len(result.hard_errors)} hard error(s)")


# ---------------------------------------------------------------------------
# End-to-end revision execution
# ---------------------------------------------------------------------------


def _dispatch_llm_redesign(
    job_id: str,
    database_name: str,
    engine: str,
    current_schema: dict,
    request: SchemaRevisionRequest,
    store: ArtifactStore,
) -> dict:
    """Dispatch the schema design agent to apply NOTE and new_pattern modifications.

    Writes the revision context to a temp file and passes it to the agent alongside
    the collector and analysis inputs. The agent uses revision_context to apply
    customer annotations and design new patterns.

    Returns the updated schema_output dict from the agent.
    """
    import json
    import tempfile

    from src.agents.schema_design.handler import _dispatch_schema_agent

    # Build revision context
    context = build_revision_context(request, current_schema)

    # Write revision context to temp file
    context_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(context, context_tmp)
    context_tmp.close()

    # Find collector and analysis paths from the store
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    analysis_key = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"

    collector_data = store.read_json(collector_key)
    analysis_data = store.read_json(analysis_key)

    collector_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(collector_data, collector_tmp)
    collector_tmp.close()

    analysis_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(analysis_data, analysis_tmp)
    analysis_tmp.close()

    import os

    try:
        output_json, _ = _dispatch_schema_agent(
            engine,
            collector_path=collector_tmp.name,
            analysis_path=analysis_tmp.name,
            revision_context_path=context_tmp.name,
        )
        result: dict = json.loads(output_json)
        return result
    finally:
        os.unlink(context_tmp.name)
        os.unlink(collector_tmp.name)
        os.unlink(analysis_tmp.name)


def _apply_drops_to_schema(schema_output: dict, dropped_pattern_ids: set[str]) -> dict:
    """Return a new schema_output dict with dropped patterns removed.

    Filters access_patterns, index_designs, and collection_designs.
    """
    result = dict(schema_output)
    result["access_patterns"] = [
        ap
        for ap in schema_output.get("access_patterns", [])
        if ap.get("pattern_id") not in dropped_pattern_ids
    ]
    result["index_designs"] = [
        idx
        for idx in schema_output.get("index_designs", [])
        if idx.get("pattern_id") not in dropped_pattern_ids
    ]
    result["collection_designs"] = [
        col
        for col in schema_output.get("collection_designs", [])
        if col.get("pattern_id") not in dropped_pattern_ids
    ]
    return result


def execute_revision(
    job_id: str,
    database_name: str,
    engine: str,
    request: SchemaRevisionRequest,
    store: ArtifactStore,
) -> tuple[dict, SchemaVersionMeta]:
    """Execute a full revision cycle: resolve → apply → verify → save.

    Pipeline:
    1. Read current schema from store
    2. Resolve table drops into affected pattern IDs
    3. Collect all patterns to remove (drops + reassigns)
    4. Apply removals to produce the revised schema
    5. Determine in-scope query_ids for verification
    6. Run verification — raises VerificationError on hard errors
    7. Generate changelog between previous and new schema
    8. Write new version artifacts to store
    9. Return (new_schema_output, version_meta)

    Note: Full LLM-based redesign (adding new patterns via agent) requires
    the env-var-to-parameter refactor (Task 4). Until then, this applies
    structural modifications (drops, reassigns) deterministically.
    """
    from datetime import UTC, datetime

    from src.agents.schema_design.revision_verifier import verify_revision
    from src.contracts.schema_revision_models import SchemaVersionMeta

    current_version = request.base_version
    new_version = current_version + 1

    # 1. Read current schema
    schema_path = f"{database_name}/{job_id}/schema-{engine}/v{current_version}/schema_output.json"
    current_schema = store.read_json(schema_path)

    # 2. Resolve table drops → affected pattern IDs
    all_dropped_patterns = resolve_table_drops(request.table_modifications, current_schema)

    # 3. Add explicit pattern drops and reassigns (both remove from this engine)
    for mod in request.pattern_modifications:
        if mod.action in (PatternAction.DROP, PatternAction.REASSIGN):
            all_dropped_patterns.add(mod.pattern_id)

    # 4. Apply removals to produce revised schema
    new_schema = _apply_drops_to_schema(current_schema, all_dropped_patterns)

    # 4b. If revision includes NOTE actions or new_patterns, dispatch LLM redesign
    has_notes = any(mod.action == PatternAction.NOTE for mod in request.pattern_modifications)
    has_new_patterns = len(request.new_patterns) > 0

    if has_notes or has_new_patterns:
        new_schema = _dispatch_llm_redesign(
            job_id=job_id,
            database_name=database_name,
            engine=engine,
            current_schema=new_schema,
            request=request,
            store=store,
        )

    # 5. Determine in-scope query_ids (from remaining patterns)
    in_scope_query_ids: set[str] = set()
    for ap in new_schema.get("access_patterns", []):
        in_scope_query_ids.update(ap.get("query_ids", []))
    for idx in new_schema.get("index_designs", []):
        in_scope_query_ids.update(idx.get("query_ids", []))
    for col in new_schema.get("collection_designs", []):
        in_scope_query_ids.update(col.get("query_ids", []))

    # 6. Run verification (including cross-engine conflict checks for reassignments)
    reassignments = [
        {"pattern_id": mod.pattern_id, "target_engine": mod.target_engine}
        for mod in request.pattern_modifications
        if mod.action == PatternAction.REASSIGN and mod.target_engine
    ]

    # Load target engine schemas for conflict verification
    target_outputs: dict[str, dict] = {}
    if reassignments:
        target_engines = {r["target_engine"] for r in reassignments}
        for target_engine in target_engines:
            try:
                # Find latest version of target engine schema
                prefix = f"{database_name}/{job_id}/schema-{target_engine}/"
                keys = store.list_prefix(prefix)
                versions = []
                for key in keys:
                    relative = key.replace(prefix, "")
                    parts = relative.split("/")
                    if parts and parts[0].startswith("v"):
                        try:
                            versions.append(int(parts[0][1:]))
                        except ValueError:
                            continue
                if versions:
                    latest_v = max(versions)
                    target_path = f"{prefix}v{latest_v}/schema_output.json"
                    target_outputs[target_engine] = store.read_json(target_path)
            except Exception:
                logger.debug(
                    "Target schema not available for %s — conflict check will flag it",
                    target_engine,
                )

    verification = verify_revision(
        schema_output=new_schema,
        in_scope_query_ids=list(in_scope_query_ids),
        engine=engine,
        reassignments=reassignments,
        target_outputs=target_outputs,
        previous_cost=None,
        current_cost=None,
    )

    if not verification.passed:
        raise VerificationError(verification)

    # 7. Generate changelog
    changelog = generate_changelog(current_schema, new_schema, engine)

    # 8. Build version meta
    meta = SchemaVersionMeta(
        version=new_version,
        base_version=current_version,
        initiated_by="customer",
        timestamp=datetime.now(UTC),
        modifications=request,
        redesigned_groups=[engine],
        verification=verification,
        changelog=changelog,
    )

    # 9. Write artifacts
    base_path = f"{database_name}/{job_id}/schema-{engine}/v{new_version}"
    store.write_json(f"{base_path}/schema_output.json", new_schema)

    # Materialize query journey files (design section) — ADR-019
    from src.agents.query_journey_materializer import materialize_design

    materialize_design(new_schema, engine, new_version, database_name, job_id, store)
    store.write_json(f"{base_path}/version_meta.json", meta.model_dump(mode="json"))
    store.write_json(f"{base_path}/revision_request.json", request.model_dump(mode="json"))
    store.write_json(
        f"{base_path}/changelog.json",
        {"entries": [e.model_dump(mode="json") for e in changelog]},
    )

    # Build revision context for traceability (not used by agent yet)
    context = build_revision_context(request, current_schema)
    store.write_json(f"{base_path}/revision_context.json", context)

    return new_schema, meta
