"""
Group Merger — Merges per-group schema drafts into a single schema output.

After the schema design pipeline produces one draft per group, this module
merges them back into a single combined schema output. Handles engine-specific
list fields, deduplication of table/collection/index definitions, and
trade-off deduplication.

DynamoDB drafts are reconciled by :mod:`src.agents.schema_design.dynamodb_merge`
(one home per source table, unique pattern IDs; the merge rule is documented
there).

Works with ArtifactStore for both local and S3 backends.
"""

from __future__ import annotations

import logging
import re

from src.agents.schema_design import dynamodb_merge
from src.agents.schema_design.design_trace import write_design_trace
from src.agents.schema_design.dynamodb_merge import (
    MERGE_FAILURE_PREFIX,
    OVERLAP_PREFIX,
    merge_failures,
    merge_warnings,
    pattern_id_maps,
    rewrite_pattern_ids,
)
from src.agents.schema_design.output_contract import write_schema_output
from src.agents.schema_design.scope import normalize_table_name
from src.contracts.schema_design_input import ExcludedQuery
from src.contracts.skipped_placeholders import SkippedSchemaOutput
from src.shared.contract_versioning import ContractVersionError
from src.storage.artifact_store import ArtifactStore
from src.storage.collector_for_models import read_collector_for_models

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_LIST_FIELDS",
    "MERGE_FAILURE_PREFIX",
    "OVERLAP_PREFIX",
    "merge_failures",
    "merge_group_drafts",
    "merge_schema_groups",
    "merge_warnings",
]

# Fields that are concatenated (list merge) across groups, per engine
# Field names below are taken from each engine's output contract
# (src/contracts/<engine>_model_output.py). Grouping applies only to the
# remodeling engines; Aurora stays single-pass (its output is a single
# generated_ddl script that does not merge), so it has no entry here (ADR-027
# amendment).
DYNAMODB_LIST_FIELDS = [
    "access_patterns",
    "table_definitions",
    "unsupported_patterns",
    "migration_notes",
    "hot_partition_analysis",
    "trade_offs",
    "validation_failures",
]

OPENSEARCH_LIST_FIELDS = [
    "index_designs",
    "data_stream_designs",
    "access_patterns",
    "unsupported_patterns",
    "trade_offs",
    "validation_failures",
]

DOCUMENTDB_LIST_FIELDS = [
    "collections",
    "access_patterns",
    "unsupported_patterns",
    "migration_notes",
    "trade_offs",
    "validation_failures",
]

ELASTICACHE_LIST_FIELDS = [
    "key_designs",
    "access_patterns",
    "cache_invalidation",
    "unsupported_patterns",
    "migration_notes",
    "trade_offs",
    "validation_failures",
]

ENGINE_LIST_FIELDS: dict[str, list[str]] = {
    "dynamodb": DYNAMODB_LIST_FIELDS,
    "opensearch": OPENSEARCH_LIST_FIELDS,
    "documentdb": DOCUMENTDB_LIST_FIELDS,
    "elasticache": ELASTICACHE_LIST_FIELDS,
}


def _dedupe_by_name(items: list[dict], key: str) -> list[dict]:
    """Deduplicate dicts by a name key, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        name = item.get(key)
        if name and name not in seen:
            seen.add(name)
            result.append(item)
    return result


def table_owners(manifest: dict) -> dict[str, int]:
    """Normalised source table -> the group index that owns it (#1243).

    ``--split`` lists each table in exactly one group's ``primary_tables`` since
    manifest 1.2. A manifest written before (no ``contract_version``, or below 1.2)
    has no ownership: it gave a table to every chunk of its cluster, so this returns
    ``{}`` and the merge works as it did before ownership. A table listed by more
    than one group has no owner either.
    """
    version = str(manifest.get("contract_version") or "0.0")
    try:
        major, minor = (int(part) for part in version.split(".", 1))
    except ValueError:
        return {}
    if (major, minor) < (1, 2):
        return {}
    listed: dict[str, set[int]] = {}
    for group in manifest.get("groups") or []:
        for table in group.get("primary_tables") or []:
            listed.setdefault(normalize_table_name(str(table)), set()).add(
                int(group["group_index"])
            )
    return {t: next(iter(groups)) for t, groups in listed.items() if len(groups) == 1}


def merge_group_drafts(
    drafts: list[dict],
    engine: str,
    group_indices: list[int] | None = None,
    unique_keys: dict[str, list[frozenset[str]]] | None = None,
    notes: list[str] | None = None,
    column_types: dict[str, dict[str, str]] | None = None,
    owners: dict[str, int] | None = None,
) -> dict:
    """Merge multiple group drafts into a single schema draft.

    Args:
        drafts: List of per-group schema draft dicts.
        engine: Target engine name (dynamodb, documentdb, opensearch).
        group_indices: Group index of each draft, used in DynamoDB merge
            messages (defaults to the draft's position).
        unique_keys: DynamoDB only: the source tables' unique column sets
            (``dynamodb_key_reconcile.unique_keys_from_collector``), so a key
            holding one can be kept when groups disagree (#772).
        notes: DynamoDB only: receives the merge's mechanical reconciliation
            notes (names, labels, folds) for the merge trace.
        column_types: DynamoDB only: integer/text source columns
            (``dynamodb_key_reconcile.column_types_from_collector``), so an
            N-vs-S partition-key disagreement takes the source column's type.
        owners: DynamoDB only: normalised source table -> owning group index
            (:func:`table_owners`, #1243). A context group's placeholder for a
            table is pointed at the owner's table, and the owner's key schema is
            kept there.

    Returns:
        Merged schema draft dict.

    Raises:
        ValueError: If no drafts provided.
    """
    if not drafts:
        raise ValueError("No group drafts to merge")

    if len(drafts) == 1 and engine != "dynamodb":
        return drafts[0]
    if group_indices is None or len(group_indices) != len(drafts):
        group_indices = list(range(len(drafts)))

    if engine == "dynamodb":
        # Content order first, so draft order and group numbering never change
        # the design (#809). Then renumber pattern IDs and re-point absorbed
        # tables per draft, before the drafts are combined (see dynamodb_merge).
        drafts, group_indices = dynamodb_merge.canonical_draft_order(drafts, group_indices)
        drafts, reconciliation, drafts_passed = dynamodb_merge.prepare_drafts(
            drafts,
            group_indices,
            unique_keys=unique_keys,
            notes=notes,
            column_types=column_types,
            owners=owners,
        )

    # Use first draft as base for scalar fields
    merged = dict(drafts[0])
    list_fields = ENGINE_LIST_FIELDS.get(engine, [])

    # Concatenate list fields from all drafts (a single draft keeps its own)
    for field in list_fields if len(drafts) > 1 else []:
        combined: list = []
        for draft in drafts:
            combined.extend(draft.get(field, []))
        merged[field] = combined

    # Deduplicate table/collection/index/key definitions by their contract name field.
    # DynamoDB reconciles tables after trade-offs are deduplicated (below).
    if engine == "opensearch":
        if "index_designs" in merged:
            merged["index_designs"] = _dedupe_by_name(merged["index_designs"], "index_name")
        if "data_stream_designs" in merged:
            merged["data_stream_designs"] = _dedupe_by_name(
                merged["data_stream_designs"], "data_stream_name"
            )
    elif engine == "documentdb" and "collections" in merged:
        merged["collections"] = _dedupe_by_name(merged["collections"], "collection_name")
    elif engine == "elasticache" and "key_designs" in merged:
        merged["key_designs"] = _dedupe_by_name(merged["key_designs"], "key_pattern")

    # Deduplicate trade_offs by description
    if "trade_offs" in merged:
        seen_desc: set[str] = set()
        deduped: list = []
        for t in merged["trade_offs"]:
            desc = t.get("description", str(t)) if isinstance(t, dict) else str(t)
            if desc not in seen_desc:
                seen_desc.add(desc)
                deduped.append(t)
        merged["trade_offs"] = deduped

    # validation_passed is True only if all groups passed
    merged["validation_passed"] = all(d.get("validation_passed", True) for d in drafts)

    if engine == "dynamodb":
        merged = dynamodb_merge.finish_merge(merged, reconciliation, drafts_passed, notes)

    return merged


_TRACE_GROUP_RE = re.compile(r"design_trace_group_(\d+)\.json$")


def _sum_token_usage(group_traces: list[dict]) -> dict[str, int] | None:
    """Run total of the per-group ``token_usage`` (#626), or None when no group
    recorded any (a deterministic or headless run)."""
    usages = [t["token_usage"] for t in group_traces if isinstance(t.get("token_usage"), dict)]
    if not usages:
        return None
    total: dict[str, int] = {}
    for usage in usages:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + int(value)
    return total


def _consolidate_design_traces(
    store: ArtifactStore,
    base_key: str,
    pattern_maps: dict[int, dict[str, str]],
    merge_notes: list[str] | None = None,
) -> None:
    """Combine ``design_trace_group_<n>.json`` into ``design_trace.json``.

    Group traces are left as written; the combined trace gets each group's
    pattern-ID renumbering (DynamoDB, #229) so it matches the merged output.
    ``merge_notes`` (DynamoDB, #772): the merge's mechanical reconciliation
    notes, written as ``merge_reconciliation`` on every merge (an empty list
    when there are none, so an earlier attempt's notes never linger).
    """
    # List the version directory, not a file-name prefix: the local store only
    # lists directory prefixes, so ``design_trace_group_`` matched nothing there.
    keyed: list[tuple[int, str]] = []
    for key in store.list_prefix(base_key):
        match = _TRACE_GROUP_RE.search(key)
        if match and key.rsplit("/", 1)[0] == base_key.rstrip("/"):
            keyed.append((int(match.group(1)), key))
    group_traces = []
    for idx, key in sorted(keyed):
        try:
            trace = store.read_json(key)
        except ContractVersionError:
            raise  # a group trace this code cannot read fails the merge, not just its group
        except Exception:
            logger.debug("Skipping unreadable trace artifact: %s", key)
            continue
        renumbered = rewrite_pattern_ids(trace, pattern_maps.get(idx, {}))
        # The merged file's version covers its groups; a group's own is not repeated.
        if isinstance(renumbered, dict):
            renumbered = {k: v for k, v in renumbered.items() if k != "contract_version"}
        group_traces.append(renumbered)
    if group_traces or merge_notes:
        combined: dict = {"total_groups": len(group_traces), "groups": group_traces}
        total = _sum_token_usage(group_traces)
        if total is not None:
            combined["token_usage"] = total
            logger.info("schema-merge usage over %d groups | %s", len(group_traces), total)
        if merge_notes is not None:
            combined["merge_reconciliation"] = merge_notes
        write_design_trace(store, f"{base_key}/design_trace.json", combined)


def merge_schema_groups(
    job_id: str,
    database_name: str,
    engine: str,
    store: ArtifactStore,
    schema_version: int = 1,
) -> dict:
    """Read per-group schema drafts from the artifact store and merge them.

    Reads the groups manifest, loads each group's draft, merges them,
    and writes the combined schema draft back to the store.

    Args:
        job_id: Pipeline job ID.
        database_name: Source database name.
        engine: Target engine name.
        store: ArtifactStore for reading/writing artifacts.
        schema_version: Schema version number for artifact paths.

    Returns:
        Merged schema draft dict.
    """
    base_key = f"{database_name}/{job_id}/schema-{engine}/v{schema_version}"

    # Read manifest
    manifest = store.read_json(f"{base_key}/groups_manifest.json")

    # Every assigned query touched no source table (#276/#369): --split left
    # all of them out of grouping, so there is no group, and so no draft, to
    # merge -- not a failure, the same way an engine with no in-scope queries
    # at all gets a placeholder (schema_design/handler.py's "skipped" shape)
    # rather than an error. Checked before the draft-reading loop below: an
    # empty manifest["groups"] would otherwise fall through to "no drafts
    # found", indistinguishable from a real missing-draft failure.
    if not manifest["groups"]:
        excluded_queries = manifest.get("excluded_queries") or []
        if excluded_queries:
            # A placeholder of its own contract, not the design (#1023).
            placeholder = SkippedSchemaOutput(
                target_type=engine,
                reason="every assigned query touches no source table",
                excluded_queries=[ExcludedQuery.model_validate(q) for q in excluded_queries],
            )
            skipped = placeholder.model_dump(mode="json")
            skipped = write_schema_output(store, f"{base_key}/schema_output.json", skipped, engine)
            print(
                f"[schema-merge/{engine}] No groups to merge: all "
                f"{len(excluded_queries)} assigned queries touch no source table. "
                "Wrote a skipped schema_output.json."
            )
            return skipped
        raise ValueError(f"No group drafts found to merge for {engine}")

    # Read all group drafts
    drafts: list[dict] = []
    indices: list[int] = []
    missing: list[int] = []

    for group in manifest["groups"]:
        idx = group["group_index"]
        draft_key = f"{base_key}/schema_draft_group_{idx}.json"
        try:
            drafts.append(store.read_json(draft_key))
            indices.append(idx)
        except Exception:
            missing.append(idx)

    if missing:
        print(f"[schema-merge/{engine}] Warning: missing drafts for groups: {missing}")

    if not drafts:
        raise ValueError(f"No group drafts found to merge for {engine}")

    # Merge. DynamoDB reads the source tables' unique keys (#772) so the
    # reconciliation can keep a key that provably identifies every row; its
    # mechanical notes go into design_trace.json (merge_reconciliation).
    unique_keys = None
    column_types = None
    notes: list[str] = []
    if engine == "dynamodb":
        from src.agents.schema_design.dynamodb_key_reconcile import (
            column_types_from_collector,
            unique_keys_from_collector,
        )

        collector_key = f"{database_name}/{job_id}/collector/output.json"
        try:
            collector = read_collector_for_models(store, database_name, job_id)
        except ContractVersionError:
            raise  # an unreadable collector output fails the merge, not just its keys
        except Exception as exc:
            # Safe direction (only superset keys are kept, so more conflicts and
            # never collapsed rows), but say so: it explains surprise conflicts.
            logger.warning(
                "DynamoDB merge: no collector output at %s (%s); merging without the "
                "source tables' unique keys",
                collector_key,
                exc,
            )
            collector = None
        unique_keys = unique_keys_from_collector(collector)
        column_types = column_types_from_collector(collector)
    merged = merge_group_drafts(
        drafts,
        engine,
        group_indices=indices,
        unique_keys=unique_keys,
        notes=notes,
        column_types=column_types,
        owners=table_owners(manifest) if engine == "dynamodb" else None,
    )

    # Carry the manifest's excluded_queries (--split's "not designed: no source
    # table" list, #276/#369) into the final output, so a query that never
    # reached any group draft is still visible on the merged result rather
    # than silently missing from it.
    excluded_queries = manifest.get("excluded_queries") or []
    if excluded_queries:
        merged["excluded_queries"] = excluded_queries

    # Write merged draft (the version comes from the contract, not the drafts).
    # It must pass the engine's output contract first: an invalid design raises
    # SchemaContractError and is never written (#1186).
    output_key = f"{base_key}/schema_output.json"
    merged = write_schema_output(store, output_key, merged, engine)

    maps = pattern_id_maps(drafts) if engine == "dynamodb" else []
    _consolidate_design_traces(
        store,
        base_key,
        dict(zip(indices, maps, strict=False)),
        notes if engine == "dynamodb" else None,
    )

    print(
        f"[schema-merge/{engine}] Merged {len(drafts)} group drafts"
        + (f" (skipped {len(missing)} missing)" if missing else "")
    )

    return merged
