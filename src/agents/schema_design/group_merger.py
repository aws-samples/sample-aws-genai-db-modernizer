"""
Group Merger — Merges per-group schema drafts into a single schema output.

After the schema design pipeline produces one draft per group, this module
merges them back into a single combined schema output. Handles engine-specific
list fields, deduplication of table/collection/index definitions, and
trade-off deduplication.

DynamoDB merge rule (issue #223). Groups are designed in parallel, so two
groups whose queries touch the same source table each design a home for it.
The merge gives every source table one DynamoDB home unless the design says
why it needs more:

1. Tables that share a source table (or a table name) and are *compatible*
   are merged into the first one: same partition/sort key names and types,
   the same shape (both item collections, or both single-entity tables), and
   no conflicting definitions. Item collections must share at least one
   identical entity (type, source table, PK/SK templates), may not reuse an
   entity type or a static SK prefix for different data, and may not define a
   same-named GSI differently. Single-entity tables must describe the same
   base entity (the source table of the partition-key attribute). Entities,
   attributes, GSIs and source tables are unioned; ``item_count`` and
   ``item_size_bytes`` take the larger estimate. Access patterns,
   ``hot_partition_analysis`` and trade-off ``target_tables`` that named the
   absorbed table are re-pointed to the surviving one, and a trade-off records
   the merge.
2. Any source table still designed in more than one table must be justified by
   a trade-off whose ``source_tables`` include it and whose ``target_tables``
   name *every* table it is designed in (why the access patterns need each
   copy, and how writes keep them in sync). Otherwise the merge adds a
   ``validation_failures`` entry (prefix :data:`MERGE_FAILURE_PREFIX`) and sets
   ``validation_passed=false`` so ``/design-schema-dynamodb`` reconciles the
   group drafts.
3. Two different designs under the same table name cannot both exist: the
   first is kept and the merge fails validation.

"Designed in" means ``table_definitions[].source_tables`` or
``entities[].source_table``; attribute-level ``source_table`` (a denormalized
column) does not count, matching the scope check in ``scope.py``.

Works with ArtifactStore for both local and S3 backends.
"""

from __future__ import annotations

from typing import Any

from src.agents.schema_design.scope import normalize_table_name
from src.storage.artifact_store import ArtifactStore

MERGE_FAILURE_PREFIX = "DynamoDB merge: "
"""Every DynamoDB merge failure starts with this, so a re-merge drops stale ones."""

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


# ---------------------------------------------------------------------------
# DynamoDB: reconcile tables that several groups designed (issue #223)
# ---------------------------------------------------------------------------


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _key_attr(key: Any) -> tuple[str, str] | None:
    if not isinstance(key, dict) or not key.get("attribute_name"):
        return None
    return str(key["attribute_name"]), str(key.get("attribute_type") or "")


def _key_schema(table: dict) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    return _key_attr(table.get("partition_key")), _key_attr(table.get("sort_key"))


def _describe_keys(table: dict) -> str:
    pk, sk = _key_schema(table)
    text = f"PK {pk[0]}" if pk else "no PK"
    return f"{text}, SK {sk[0]}" if sk else text


def _designed_sources(table: dict) -> list[str]:
    """Normalised source tables a table is a home for (table + entity level)."""
    names = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
    names += [
        e["source_table"]
        for e in _dicts(table.get("entities"))
        if isinstance(e.get("source_table"), str)
    ]
    keys = (normalize_table_name(n) for n in names)
    return list(dict.fromkeys(k for k in keys if k and k != "unknown"))


def _shape(table: dict) -> str:
    if _dicts(table.get("entities")):
        return "item_collection"
    if _dicts(table.get("attributes")):
        return "single"
    return "empty"


def _base_entity(table: dict) -> str:
    """Source table of the partition-key attribute (else the first source table)."""
    pk = _key_attr(table.get("partition_key"))
    for attr in _dicts(table.get("attributes")):
        if pk and attr.get("name") == pk[0] and isinstance(attr.get("source_table"), str):
            return normalize_table_name(attr["source_table"])
    sources = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
    return normalize_table_name(sources[0]) if sources else ""


def _entity_signature(entity: dict) -> tuple[str, str, str, str]:
    return (
        str(entity.get("entity_type") or ""),
        normalize_table_name(str(entity.get("source_table") or "")),
        str(entity.get("pk_template") or ""),
        str(entity.get("sk_template") or ""),
    )


def _sk_prefix(entity: dict) -> str:
    return str(entity.get("sk_template") or "").split("{", 1)[0]


def _gsi_keys(gsi: dict) -> tuple:
    def keys(value: Any) -> tuple:
        return tuple(_key_attr(k) for k in value) if isinstance(value, list) else ()

    return keys(gsi.get("partition_key")), keys(gsi.get("sort_key"))


def _merge_conflict(a: dict, b: dict) -> str | None:
    """Why ``b`` cannot be merged into ``a`` (``None`` when it can)."""
    if _key_schema(a) != _key_schema(b):
        return f"key schemas differ ({_describe_keys(a)} vs {_describe_keys(b)})"
    shape = _shape(a)
    if shape != _shape(b):
        return "one is an item collection, the other a single-entity table"
    if shape == "item_collection":
        a_entities = {e.get("entity_type"): e for e in _dicts(a.get("entities"))}
        b_entities = _dicts(b.get("entities"))
        a_sigs = {_entity_signature(e) for e in a_entities.values()}
        if not any(_entity_signature(e) in a_sigs for e in b_entities):
            return "the item collections share no identical entity"
        a_prefixes = {_sk_prefix(e): e for e in a_entities.values()}
        for entity in b_entities:
            same_type = a_entities.get(entity.get("entity_type"))
            if same_type is not None and _entity_signature(same_type) != _entity_signature(entity):
                return f"entity {entity.get('entity_type')} is defined differently"
            same_prefix = a_prefixes.get(_sk_prefix(entity))
            if same_prefix is not None and _entity_signature(same_prefix)[:2] != (
                _entity_signature(entity)[:2]
            ):
                return f"sort key prefix '{_sk_prefix(entity)}' is used by different entities"
    elif shape == "single" and _base_entity(a) != _base_entity(b):
        return "they store different base entities"
    a_gsis = {g.get("gsi_name"): g for g in _dicts(a.get("gsis"))}
    for gsi in _dicts(b.get("gsis")):
        same = a_gsis.get(gsi.get("gsi_name"))
        if same is not None and _gsi_keys(same) != _gsi_keys(gsi):
            return f"GSI {gsi.get('gsi_name')} is defined differently"
    return None


def _union_by(items: list[dict], extra: list[dict], key: str) -> list[dict]:
    seen = {i.get(key) for i in items}
    return items + [i for i in extra if i.get(key) not in seen]


def _absorb(a: dict, b: dict) -> dict:
    """Return ``a`` with ``b``'s entities, attributes, GSIs and sources added."""
    merged = dict(a)
    sources = [s for s in a.get("source_tables") or [] if isinstance(s, str)]
    known = {normalize_table_name(s) for s in sources}
    for s in b.get("source_tables") or []:
        if isinstance(s, str) and normalize_table_name(s) not in known:
            known.add(normalize_table_name(s))
            sources.append(s)
    merged["source_tables"] = sources
    if _shape(a) == "item_collection":
        by_type = {e.get("entity_type"): dict(e) for e in _dicts(a.get("entities"))}
        for entity in _dicts(b.get("entities")):
            same = by_type.get(entity.get("entity_type"))
            if same is None:
                by_type[entity.get("entity_type")] = dict(entity)
            else:
                same["attributes"] = _union_by(
                    _dicts(same.get("attributes")), _dicts(entity.get("attributes")), "name"
                )
        merged["entities"] = list(by_type.values())
    elif _shape(a) == "single":
        merged["attributes"] = _union_by(
            _dicts(a.get("attributes")), _dicts(b.get("attributes")), "name"
        )
    merged["gsis"] = _union_by(_dicts(a.get("gsis")), _dicts(b.get("gsis")), "gsi_name")
    for field in ("item_count", "item_size_bytes"):
        values: list[int] = [t[field] for t in (a, b) if isinstance(t.get(field), int)]
        if values:
            merged[field] = max(values)
    return merged


def _groups_label(groups: list[int]) -> str:
    return "group " + "/".join(str(g) for g in groups)


def _renamed(name: Any, renames: dict[str, str]) -> Any:
    """Map a table (or ``Table.GSI``) reference through ``renames``."""
    if not isinstance(name, str):
        return name
    if name in renames:
        return renames[name]
    table, dot, rest = name.partition(".")
    return f"{renames[table]}.{rest}" if dot and table in renames else name


def _covers(trade_off: dict, source: str, targets: list[str]) -> bool:
    sources = {
        normalize_table_name(s) for s in trade_off.get("source_tables") or [] if isinstance(s, str)
    }
    if source not in sources:
        return False
    named = {str(t).lower() for t in trade_off.get("target_tables") or [] if isinstance(t, str)}
    return all(
        t.lower() in named or any(n.startswith(t.lower() + ".") for n in named) for t in targets
    )


def _reconcile_dynamodb(merged: dict, origins: list[int]) -> list[str]:
    """Apply the module's DynamoDB merge rule to ``merged`` in place.

    ``origins[i]`` is the group index of ``merged["table_definitions"][i]``.
    Returns the merge failures.
    """
    failures: list[str] = []
    kept: list[dict] = []
    groups: list[list[int]] = []
    renames: dict[str, str] = {}
    merge_trade_offs: list[dict] = []
    ap_queries: dict[str, list[str]] = {}
    for ap in _dicts(merged.get("access_patterns")):
        ap_queries.setdefault(str(ap.get("table_name")), []).extend(
            q for q in ap.get("query_ids") or [] if isinstance(q, str)
        )

    for table, group in zip(merged.get("table_definitions") or [], origins, strict=True):
        if not isinstance(table, dict):
            continue
        name = table.get("table_name")
        sources = set(_designed_sources(table))
        target = None
        for i, other in enumerate(kept):
            same_name = other.get("table_name") == name
            if not same_name and not sources & set(_designed_sources(other)):
                continue
            conflict = _merge_conflict(other, table)
            if conflict is None:
                target = i
                break
            if same_name:
                failures.append(
                    f"{MERGE_FAILURE_PREFIX}table '{name}' is designed differently in "
                    f"{_groups_label(groups[i])} and group {group} ({conflict}); the merge kept "
                    f"the {_groups_label(groups[i])} design. Give the tables different names or "
                    "reconcile them into one design."
                )
                target = -1
                break
        if target == -1:
            continue
        if target is None:
            kept.append(table)
            groups.append([group])
            continue
        survivor = kept[target]
        kept[target] = _absorb(survivor, table)
        if group not in groups[target]:
            groups[target].append(group)
        if name and name != survivor.get("table_name"):
            renames[name] = str(survivor.get("table_name"))
            shared = [
                s for s in survivor.get("source_tables") or [] if normalize_table_name(s) in sources
            ]
            merge_trade_offs.append(
                {
                    "description": (
                        f"Group designs {survivor.get('table_name')} ({_groups_label(groups[target][:1])}) "
                        f"and {name} (group {group}) both model {', '.join(shared)} with the same "
                        f"keys ({_describe_keys(survivor)}); the merge combines them into "
                        f"{survivor.get('table_name')}."
                    ),
                    "impact": (
                        f"{', '.join(shared)} {'has' if len(shared) == 1 else 'have'} one "
                        f"DynamoDB home, {survivor.get('table_name')}; "
                        f"access patterns designed against {name} run against "
                        f"{survivor.get('table_name')}."
                    ),
                    "source_tables": shared,
                    "target_tables": [str(survivor.get("table_name"))],
                    "query_ids": list(dict.fromkeys(ap_queries.get(name, []))),
                    "engine": "dynamodb",
                }
            )

    # A survivor is never absorbed later, so ``renames`` maps straight to final names.
    merged["table_definitions"] = kept
    for field in ("access_patterns", "hot_partition_analysis"):
        if isinstance(merged.get(field), list):
            merged[field] = [
                (
                    {**item, "table_name": _renamed(item.get("table_name"), renames)}
                    if isinstance(item, dict)
                    else item
                )
                for item in merged[field]
            ]
    if isinstance(merged.get("trade_offs"), list):
        merged["trade_offs"] = [
            (
                {
                    **t,
                    "target_tables": list(
                        dict.fromkeys(_renamed(n, renames) for n in t.get("target_tables") or [])
                    ),
                }
                if isinstance(t, dict) and isinstance(t.get("target_tables"), list)
                else t
            )
            for t in merged["trade_offs"]
        ] + merge_trade_offs

    # Source tables still designed in more than one table need a stated reason.
    homes: dict[str, list[int]] = {}
    for i, table in enumerate(kept):
        for source in _designed_sources(table):
            homes.setdefault(source, []).append(i)
    shown = {
        normalize_table_name(s): s
        for t in kept
        for s in t.get("source_tables") or []
        if isinstance(s, str)
    }
    trade_offs = _dicts(merged.get("trade_offs"))
    unjustified: dict[tuple[int, ...], list[str]] = {}
    for source, homes_of in homes.items():
        home_names = [str(kept[i].get("table_name")) for i in homes_of]
        if len(homes_of) > 1 and not any(_covers(t, source, home_names) for t in trade_offs):
            unjustified.setdefault(tuple(homes_of), []).append(shown.get(source, source))
    for home_idxs, unjustified_sources in unjustified.items():
        tables = ", ".join(
            f"{kept[i].get('table_name')} ({_groups_label(groups[i])}; {_describe_keys(kept[i])})"
            for i in home_idxs
        )
        quoted = ", ".join(f"'{s}'" for s in unjustified_sources)
        listed = ", ".join(str(kept[i].get("table_name")) for i in home_idxs)
        failures.append(
            f"{MERGE_FAILURE_PREFIX}source table(s) {quoted} designed in {len(home_idxs)} "
            f"tables: {tables}. Consolidate them into one table, or keep them separate and add "
            f"a trade_off whose source_tables include {quoted} and whose target_tables list "
            f"{listed}, saying which access patterns need each table and how writes keep the "
            "copies in sync."
        )
    return failures


def _draft_passed(draft: dict) -> bool:
    """``validation_passed`` of a draft, ignoring stale DynamoDB merge messages."""
    if draft.get("validation_passed", True):
        return True
    stale = merge_failures(draft)
    own = [f for f in draft.get("validation_failures") or [] if f not in stale]
    return bool(stale) and not own


def merge_failures(schema_output: Any) -> list[str]:
    """Return the DynamoDB merge failures recorded in ``schema_output``."""
    if not isinstance(schema_output, dict):
        return []
    failures = schema_output.get("validation_failures")
    if not isinstance(failures, list):
        return []
    return [f for f in failures if isinstance(f, str) and f.startswith(MERGE_FAILURE_PREFIX)]


def merge_group_drafts(
    drafts: list[dict], engine: str, group_indices: list[int] | None = None
) -> dict:
    """Merge multiple group drafts into a single schema draft.

    Args:
        drafts: List of per-group schema draft dicts.
        engine: Target engine name (dynamodb, documentdb, opensearch).
        group_indices: Group index of each draft, used in DynamoDB merge
            messages (defaults to the draft's position).

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
        # Merge messages copied into a draft are stale: they are recomputed here.
        failures = [
            f for f in merged.get("validation_failures") or [] if f not in merge_failures(merged)
        ]
        merged["validation_passed"] = all(_draft_passed(d) for d in drafts)
        origins = [
            g
            for d, g in zip(drafts, group_indices, strict=True)
            for _ in d.get("table_definitions") or []
        ]
        new_failures = _reconcile_dynamodb(merged, origins)
        if "validation_failures" in merged or failures or new_failures:
            merged["validation_failures"] = failures + new_failures
        if new_failures:
            merged["validation_passed"] = False

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

    # Merge
    merged = merge_group_drafts(drafts, engine, group_indices=indices)

    # Write merged draft
    store.write_json(f"{base_key}/schema_output.json", merged)

    print(
        f"[schema-merge/{engine}] Merged {len(drafts)} group drafts"
        + (f" (skipped {len(missing)} missing)" if missing else "")
    )

    return merged
