"""
Group Merger — Merges per-group schema drafts into a single schema output.

After the schema design pipeline produces one draft per group, this module
merges them back into a single combined schema output. Handles engine-specific
list fields, deduplication of table/collection/index definitions, and
trade-off deduplication.

Works with ArtifactStore for both local and S3 backends.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore

# Fields that are concatenated (list merge) across groups, per engine
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
    "unsupported_patterns",
    "migration_notes",
    "trade_offs",
    "validation_failures",
]

DOCUMENTDB_LIST_FIELDS = [
    "collection_designs",
    "unsupported_patterns",
    "migration_notes",
    "trade_offs",
    "validation_failures",
]

ENGINE_LIST_FIELDS: dict[str, list[str]] = {
    "dynamodb": DYNAMODB_LIST_FIELDS,
    "opensearch": OPENSEARCH_LIST_FIELDS,
    "documentdb": DOCUMENTDB_LIST_FIELDS,
}


def _dedupe_table_definitions(tables: list[dict]) -> list[dict]:
    """Deduplicate table definitions by table_name, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[dict] = []
    for t in tables:
        name = t.get("table_name")
        if name and name not in seen:
            seen.add(name)
            result.append(t)
    return result


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


def merge_group_drafts(drafts: list[dict], engine: str) -> dict:
    """Merge multiple group drafts into a single schema draft.

    Args:
        drafts: List of per-group schema draft dicts.
        engine: Target engine name (dynamodb, documentdb, opensearch).

    Returns:
        Merged schema draft dict.

    Raises:
        ValueError: If no drafts provided.
    """
    if not drafts:
        raise ValueError("No group drafts to merge")

    if len(drafts) == 1:
        return drafts[0]

    # Use first draft as base for scalar fields
    merged = dict(drafts[0])
    list_fields = ENGINE_LIST_FIELDS.get(engine, [])

    # Concatenate list fields from all drafts
    for field in list_fields:
        combined: list = []
        for draft in drafts:
            combined.extend(draft.get(field, []))
        merged[field] = combined

    # Deduplicate table/collection/index definitions
    if engine == "dynamodb" and "table_definitions" in merged:
        merged["table_definitions"] = _dedupe_table_definitions(merged["table_definitions"])
    elif engine == "opensearch":
        if "index_designs" in merged:
            merged["index_designs"] = _dedupe_by_name(merged["index_designs"], "index_name")
        if "data_stream_designs" in merged:
            merged["data_stream_designs"] = _dedupe_by_name(
                merged["data_stream_designs"], "stream_name"
            )
    elif engine == "documentdb" and "collection_designs" in merged:
        merged["collection_designs"] = _dedupe_by_name(
            merged["collection_designs"], "collection_name"
        )

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

    return merged


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

    # Read all group drafts
    drafts: list[dict] = []
    missing: list[int] = []

    for group in manifest["groups"]:
        idx = group["group_index"]
        draft_key = f"{base_key}/schema_draft_group_{idx}.json"
        try:
            drafts.append(store.read_json(draft_key))
        except Exception:
            missing.append(idx)

    if missing:
        print(f"[schema-merge/{engine}] Warning: missing drafts for groups: {missing}")

    if not drafts:
        raise ValueError(f"No group drafts found to merge for {engine}")

    # Merge
    merged = merge_group_drafts(drafts, engine)

    # Write merged draft
    store.write_json(f"{base_key}/schema_output.json", merged)

    print(
        f"[schema-merge/{engine}] Merged {len(drafts)} group drafts"
        + (f" (skipped {len(missing)} missing)" if missing else "")
    )

    return merged
