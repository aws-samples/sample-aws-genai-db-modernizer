"""
Group Splitter — Splits large schema design workloads into manageable groups.

When a database has 50+ tables, a single LLM prompt can't handle all of them
within token limits. This module splits tables into groups (MAX_GROUP_SIZE),
writes per-group input files, and produces a manifest for the schema design
pipeline to iterate over.

The grouping strategy (enhanced with analysis signals):
  1. Build table affinity clusters from FKs, aggregates, and co-access patterns
  2. Map queries to their cluster (not just primary table)
  3. Clusters with SMALL_GROUP_THRESHOLD+ queries get their own group
  4. Smaller clusters are batched together into misc groups
  5. Groups exceeding MAX_GROUP_SIZE are sub-split into chunks

Works with ArtifactStore for both local and S3 backends.
"""

from __future__ import annotations

from collections import defaultdict

from src.contracts.schema_design_input import SchemaDesignGroupEntry, SchemaDesignGroupsManifest
from src.storage.artifact_store import ArtifactStore

# Groups with fewer queries than this get batched together
SMALL_GROUP_THRESHOLD = 5
# Maximum queries per group — larger groups get sub-split by primary table
MAX_GROUP_SIZE = 20


def get_primary_table(query: dict, db_name: str) -> str:
    """Return the primary table for a query (first schema-qualified table)."""
    tables: list[str] = query.get("tables_accessed", [])
    prefix = f"{db_name}."
    for t in tables:
        if t.startswith(prefix):
            return t
    return tables[0] if tables else "unknown"


# ---------------------------------------------------------------------------
# Table affinity clustering
# ---------------------------------------------------------------------------


def _build_table_clusters(
    collector_output: dict,
    analysis_output: dict | None,
    db_name: str,
) -> dict[str, str]:
    """Build table clusters from FK relationships and analysis signals.

    Returns a mapping of table_id → cluster_root using union-find.
    Tables in the same cluster should be designed together because they
    share FK relationships, appear in the same aggregates, or are
    frequently co-accessed.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Source 1: Foreign key relationships from collector
    for table in collector_output.get("database_schema", {}).get("tables", []):
        table_id = table.get("table_id", "")
        for fk in table.get("foreign_keys") or []:
            ref_table = fk.get("referenced_table", "")
            # Qualify if not already
            if "." not in ref_table and db_name:
                ref_table = f"{db_name}.{ref_table}"
            if table_id and ref_table:
                union(table_id, ref_table)

    if not analysis_output:
        return parent

    # Source 2: Aggregate recommendations (multi-table aggregates)
    for agg in analysis_output.get("aggregate_recommendations") or []:
        members = agg.get("member_tables", [])
        if len(members) >= 2:
            for i in range(1, len(members)):
                union(members[0], members[i])

    # Source 3: Co-accessed-tables and bounded-parent-child patterns
    wa = analysis_output.get("workload_analysis", {})
    for pattern in wa.get("patterns_detected") or []:
        ptype = pattern.get("pattern_type", "")
        if ptype in ("co-accessed-tables", "bounded-parent-child", "many-to-many-junction"):
            table_ids = pattern.get("table_ids", [])
            # Only cluster tables that are actually schema-qualified
            qualified = [t for t in table_ids if "." in t]
            if len(qualified) >= 2:
                for i in range(1, len(qualified)):
                    union(qualified[0], qualified[i])

    return parent


def _get_cluster_root(table_id: str, parent: dict[str, str]) -> str:
    """Find the cluster root for a table, with path compression."""
    if table_id not in parent:
        return table_id
    # Path compression
    while parent.get(table_id, table_id) != table_id:
        parent[table_id] = parent.get(parent[table_id], parent[table_id])
        table_id = parent[table_id]
    return table_id


def _short_name(table_id: str) -> str:
    """Extract short table name from schema-qualified ID."""
    return table_id.split(".")[-1] if "." in table_id else table_id


# ---------------------------------------------------------------------------
# Group building
# ---------------------------------------------------------------------------


def build_groups(
    queries: list[dict],
    db_name: str,
    collector_output: dict | None = None,
    analysis_output: dict | None = None,
) -> list[dict]:
    """Split queries into groups using table affinity clusters.

    When analysis_output is provided, uses FK relationships, aggregate
    recommendations, and co-access patterns to cluster related tables.
    Queries are assigned to clusters, then clusters are sized into groups.

    Returns a list of dicts with keys: group_name, primary_tables, queries.
    """
    # Build table clusters if we have the data
    if collector_output and analysis_output:
        parent = _build_table_clusters(collector_output, analysis_output, db_name)
    else:
        parent = {}

    # Map each query to its cluster root
    by_cluster: dict[str, list[dict]] = defaultdict(list)
    cluster_tables: dict[str, set[str]] = defaultdict(set)

    for q in queries:
        primary = get_primary_table(q, db_name)
        root = _get_cluster_root(primary, parent)
        by_cluster[root].append(q)
        cluster_tables[root].add(primary)
        # Also track all accessed tables for naming
        for t in q.get("tables_accessed", []):
            if "." in t:
                t_root = _get_cluster_root(t, parent)
                if t_root == root:
                    cluster_tables[root].add(t)

    groups: list[dict] = []
    small_batch: list[dict] = []
    small_tables: list[str] = []

    for cluster_root in sorted(by_cluster, key=lambda c: len(by_cluster[c]), reverse=True):
        cluster_queries = by_cluster[cluster_root]
        tables_in_cluster = sorted(cluster_tables.get(cluster_root, {cluster_root}))

        # Name the group after the root table (or first table alphabetically)
        group_base_name = _short_name(cluster_root)

        if len(cluster_queries) >= SMALL_GROUP_THRESHOLD:
            if len(cluster_queries) <= MAX_GROUP_SIZE:
                groups.append(
                    {
                        "group_name": group_base_name,
                        "primary_tables": tables_in_cluster,
                        "queries": cluster_queries,
                    }
                )
            else:
                # Sub-split large clusters by chunk
                chunk_num = 0
                for i in range(0, len(cluster_queries), MAX_GROUP_SIZE):
                    chunk = cluster_queries[i : i + MAX_GROUP_SIZE]
                    chunk_num += 1
                    suffix = (
                        f"_part{chunk_num}"
                        if chunk_num > 1 or i + MAX_GROUP_SIZE < len(cluster_queries)
                        else ""
                    )
                    groups.append(
                        {
                            "group_name": f"{group_base_name}{suffix}",
                            "primary_tables": tables_in_cluster,
                            "queries": chunk,
                        }
                    )
        else:
            small_batch.extend(cluster_queries)
            small_tables.extend(tables_in_cluster)
            if len(small_batch) >= MAX_GROUP_SIZE:
                groups.append(
                    {
                        "group_name": f"misc_batch_{len(groups)}",
                        "primary_tables": sorted(set(small_tables)),
                        "queries": small_batch[:],
                    }
                )
                small_batch = []
                small_tables = []

    if small_batch:
        groups.append(
            {
                "group_name": f"misc_batch_{len(groups)}",
                "primary_tables": sorted(set(small_tables)),
                "queries": small_batch,
            }
        )

    return groups


def tables_for_queries(queries: list[dict], all_tables: list[dict]) -> list[dict]:
    """Return only the source tables referenced by the given queries."""
    referenced: set[str] = set()
    for q in queries:
        referenced.update(q.get("tables_accessed", []))
    return [
        t
        for t in all_tables
        if t.get("table_id") in referenced or t.get("table_name") in referenced
    ]


def recommendations_for_tables(table_ids: set[str], analysis_output: dict) -> list[dict]:
    """Return only the analysis recommendations relevant to the given tables."""
    return [
        r
        for r in analysis_output.get("table_recommendations", [])
        if r.get("table_id") in table_ids or r.get("table_name") in table_ids
    ]


def split_schema_input(
    job_id: str,
    database_name: str,
    engine: str,
    collector_output: dict,
    analysis_output: dict,
    queries: list[dict],
    store: ArtifactStore,
    schema_version: int = 1,
) -> SchemaDesignGroupsManifest:
    """Split schema design input into groups and write per-group input files.

    Args:
        job_id: Pipeline job ID.
        database_name: Source database name.
        engine: Target engine (dynamodb, documentdb, opensearch).
        collector_output: Full collector output dict.
        analysis_output: Full analysis output dict.
        queries: Filtered query patterns (only those assigned to this engine).
        store: ArtifactStore for writing artifacts.
        schema_version: Schema version number for artifact paths.

    Returns:
        SchemaDesignGroupsManifest with group entries.
    """
    all_tables = collector_output.get("database_schema", {}).get("tables", [])
    groups = build_groups(queries, database_name, collector_output, analysis_output)
    base_key = f"{database_name}/{job_id}/schema-{engine}/v{schema_version}"

    manifest_groups: list[SchemaDesignGroupEntry] = []

    for idx, group in enumerate(groups):
        group_queries = group["queries"]
        group_tables = tables_for_queries(group_queries, all_tables)
        group_table_ids = {t.get("table_id") for t in group_tables}
        group_recs = recommendations_for_tables(group_table_ids, analysis_output)

        group_db_schema = {
            **collector_output.get("database_schema", {}),
            "tables": group_tables,
        }
        group_collector = {
            **collector_output,
            "database_schema": group_db_schema,
            "tables": group_tables,
            "queries": {
                "query_patterns": group_queries,
                "_filtered": True,
                "_filter_engine": engine,
                "_group_index": idx,
                "_group_name": group["group_name"],
                "_original_count": len(queries),
                "_filtered_count": len(group_queries),
            },
        }

        group_analysis = {
            **analysis_output,
            "table_recommendations": group_recs,
        }

        combined = {
            "job_id": job_id,
            "database_name": database_name,
            "target_engine": engine,
            "group_index": idx,
            "group_name": group["group_name"],
            "group_primary_tables": group["primary_tables"],
            "collector_output": group_collector,
            "analysis_output": group_analysis,
        }

        input_file = f"input_group_{idx}.json"
        store.write_json(f"{base_key}/{input_file}", combined)

        manifest_groups.append(
            SchemaDesignGroupEntry(
                group_index=idx,
                group_name=group["group_name"],
                primary_tables=group["primary_tables"],
                query_count=len(group_queries),
                table_count=len(group_tables),
                input_file=input_file,
            )
        )

    manifest = SchemaDesignGroupsManifest(
        job_id=job_id,
        database_name=database_name,
        target_engine=engine,
        total_queries=len(queries),
        total_groups=len(groups),
        groups=manifest_groups,
    )

    store.write_json(f"{base_key}/groups_manifest.json", manifest.model_dump())

    return manifest
