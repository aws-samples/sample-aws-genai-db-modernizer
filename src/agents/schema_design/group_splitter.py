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
from collections.abc import Callable

from src.agents.referee.table_resolution import PSEUDO_TABLES, TableNameResolver
from src.agents.schema_design.group_input import (
    READ_PAGE_CHARS,
    build_group_input,
    read_pages,
    render_group_input,
)
from src.contracts.schema_design_input import (
    ExcludedQuery,
    SchemaDesignGroupEntry,
    SchemaDesignGroupsManifest,
)
from src.storage.artifact_store import ArtifactStore

# A query with no reason to see any table at all. The exclusion reason
# written to the split manifest for these (#276/#369).
NO_SOURCE_TABLE_REASON = "not designed: no source table"

# Groups with fewer queries than this get batched together
SMALL_GROUP_THRESHOLD = 5
# Maximum queries per group — larger groups get sub-split by primary table
MAX_GROUP_SIZE = 20
# Maximum size of one group's input file: three Read pages (see
# group_input.READ_PAGE_CHARS), both in characters and in the whole-line pages
# read_pages() lists, so a group subagent reads its input in at most three
# Read calls. A group over it is halved until every part fits (or
# has one query); MAX_GROUP_SIZE stays the upper bound on queries per group (#272).
MAX_GROUP_INPUT_PAGES = 3
MAX_GROUP_INPUT_CHARS = MAX_GROUP_INPUT_PAGES * READ_PAGE_CHARS


def get_primary_table(query: dict, db_name: str) -> str:
    """Return the primary table for a query (first schema-qualified table)."""
    tables: list[str] = query.get("tables_accessed", [])
    prefix = f"{db_name}."
    for t in tables:
        if t.startswith(prefix):
            return t
    return tables[0] if tables else "unknown"


def has_no_source_table(query: dict) -> bool:
    """True when ``query`` touches nothing but pseudo tables (#276/#369).

    A catalog or utility statement (``SELECT obj_description(...)``,
    ``SELECT pg_get_serial_sequence(...)``, ...) has ``tables_accessed``
    that is empty or only ``PSEUDO_TABLES`` (``unknown``, ``DUAL``) -- it
    names no real source table at all, so there is nothing to design
    against, ever, regardless of what the collector's schema contains.

    This is a narrower, and different, condition than "no table matched":
    a query naming a real table the collector's schema doesn't have (a
    spelling mismatch, a genuine resolution failure) is NOT "no source
    table" -- it stays in the normal grouping, visible if it still can't be
    designed, rather than being quietly folded away with catalog queries
    (367's review, finding 369-2).
    """
    tables = query.get("tables_accessed") or []
    return all(t in PSEUDO_TABLES for t in tables)


# ---------------------------------------------------------------------------
# Table affinity clustering
# ---------------------------------------------------------------------------


def _qualify(table: str, db_name: str) -> str:
    """Schema-qualify a bare table name with db_name (leaves qualified names as-is)."""
    if table and "." not in table and db_name:
        return f"{db_name}.{table}"
    return table


def _canon(table: str, db_name: str, resolver: TableNameResolver | None) -> str:
    """Resolve ``table`` to the collector's canonical ``table_id`` when possible.

    Falls back to :func:`_qualify` (schema-qualify with ``db_name``) when
    there is no resolver, or it has no match -- the same fallback
    ``filter_collector_for_assignment`` uses (#116). Without this, FK/co-
    dependency clustering keys are built from the collector's own
    (canonical) ``table_id``, but a query's ``tables_accessed`` can be a bare
    name a live engine's parser emits (367-2): two FK-linked tables then
    cluster together only when both sides happen to already be schema-
    qualified the same way, and split into separate groups otherwise.
    Pseudo tables (``unknown``, ``DUAL``) are returned unchanged: they name
    nothing to qualify or resolve.
    """
    if table in PSEUDO_TABLES:
        return table
    if resolver is not None:
        resolved = resolver.resolve(table)
        if resolved:
            return resolved
    return _qualify(table, db_name)


def _build_table_clusters(
    collector_output: dict,
    analysis_output: dict | None,
    db_name: str,
    queries: list[dict] | None = None,
    co_dependency_groups: list[list[str]] | None = None,
    resolver: TableNameResolver | None = None,
) -> dict[str, str]:
    """Build table clusters from FK relationships and analysis signals.

    Returns a mapping of table_id → cluster_root using union-find.
    Tables in the same cluster should be designed together because they
    share FK relationships, appear in the same aggregates, are frequently
    co-accessed, or are joined by co-dependent queries (see Source 4).
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
            ref_table = _canon(fk.get("referenced_table", ""), db_name, resolver)
            if table_id and ref_table:
                union(table_id, ref_table)

    # Source 4: Co-dependency groups (JOIN-based relatedness from the assignment).
    # These are the union-find over significant JOINs computed at assignment time
    # (ADR-027 amendment). Queries sharing a significant JOIN must be modeled
    # together, so we union the tables touched by each group's queries. Only the
    # queries actually routed to this engine (present in ``queries``) are
    # considered, so a group split across engines only pulls in this engine's
    # share. Runs regardless of ``analysis_output`` so grouping stays co-dependency
    # aware even when analysis signals are absent.
    if co_dependency_groups and queries:
        qid_to_tables: dict[str, list[str]] = {
            q.get("query_id", ""): (q.get("tables_accessed") or []) for q in queries
        }
        for group in co_dependency_groups:
            group_tables: list[str] = []
            for qid in group:
                group_tables.extend(qid_to_tables.get(qid, []))
            qualified = list(dict.fromkeys(_canon(t, db_name, resolver) for t in group_tables if t))
            for i in range(1, len(qualified)):
                union(qualified[0], qualified[i])

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
    co_dependency_groups: list[list[str]] | None = None,
) -> list[dict]:
    """Split queries into groups using table affinity clusters.

    Clusters related tables from FK relationships, aggregate recommendations,
    co-access patterns, and the assignment's ``co_dependency_groups`` (JOIN-based
    relatedness). Queries are assigned to clusters, then clusters are sized into
    groups so co-dependent / related queries are designed together.

    Returns a list of dicts with keys: group_name, primary_tables, queries.
    """
    # Resolver for canonicalising tables_accessed before clustering (367-2),
    # built once from the collector's own schema; None keeps the pre-367-2
    # qualify-only fallback when there is no collector output to resolve against.
    resolver = TableNameResolver.from_collector(collector_output) if collector_output else None

    # Build table clusters when we have any clustering signal. co_dependency_groups
    # alone is enough (it clusters from the queries' own tables), so this no longer
    # requires analysis_output.
    if collector_output or co_dependency_groups:
        parent = _build_table_clusters(
            collector_output or {},
            analysis_output,
            db_name,
            queries=queries,
            co_dependency_groups=co_dependency_groups,
            resolver=resolver,
        )
    else:
        parent = {}

    # Map each query to its cluster root
    by_cluster: dict[str, list[dict]] = defaultdict(list)
    cluster_tables: dict[str, set[str]] = defaultdict(set)

    for q in queries:
        primary = _canon(get_primary_table(q, db_name), db_name, resolver)
        root = _get_cluster_root(primary, parent)
        by_cluster[root].append(q)
        cluster_tables[root].add(primary)
        # Also track all accessed tables for naming
        for t in q.get("tables_accessed", []):
            t_canon = _canon(t, db_name, resolver)
            if "." in t_canon:
                t_root = _get_cluster_root(t_canon, parent)
                if t_root == root:
                    cluster_tables[root].add(t_canon)

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
            # Flush first so the batch never goes over MAX_GROUP_SIZE (#272).
            if small_batch and len(small_batch) + len(cluster_queries) > MAX_GROUP_SIZE:
                groups.append(
                    {
                        "group_name": f"misc_batch_{len(groups)}",
                        "primary_tables": sorted(set(small_tables)),
                        "queries": small_batch[:],
                    }
                )
                small_batch = []
                small_tables = []
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


def tables_for_queries(
    queries: list[dict],
    all_tables: list[dict],
    resolver: TableNameResolver | None = None,
) -> list[dict]:
    """Return only the source tables referenced by the given queries.

    Resolved through ``resolver`` (:class:`TableNameResolver`, #319) when given,
    the same resolution ``filter_collector_for_assignment`` uses (#116): a
    query's ``tables_accessed`` can be qualified with the SQL schema the parser
    saw while ``table_id`` is qualified with the customer-entered database
    label, and an exact-string join drops every table when the two diverge.
    Review finding 367-1: this is the join ``--split`` uses for every group
    once an engine has more than ``MAX_GROUP_SIZE`` queries, so it needs the
    same resolution ``filter_collector_for_assignment`` already has, or the
    qualifier mismatch resurfaces downstream of it. Falls back to the exact
    match when no resolver is given, for callers that build it once per split
    (:func:`split_schema_input`) and reuse it across every group.
    """
    referenced: set[str] = set()
    for q in queries:
        referenced.update(q.get("tables_accessed", []))
    if resolver is not None:
        referenced |= {m for m in (resolver.resolve(name) for name in referenced) if m}
    return [
        t
        for t in all_tables
        if t.get("table_id") in referenced or t.get("table_name") in referenced
    ]


def fit_groups_to_budget(
    groups: list[dict],
    measure: Callable[[dict], int],
    max_chars: int = MAX_GROUP_INPUT_CHARS,
) -> list[dict]:
    """Halve each group whose input is over ``max_chars`` until every part fits.

    ``measure(group)`` returns the size of the group's rendered input. A
    single-query group is kept whatever its size. Parts keep the group's
    ``primary_tables`` and are named ``<group_name>_s1``, ``_s2``, ... Each
    part is measured under the longest name it could get (``_s<query count>``),
    so the written file is never larger than the size that was checked.
    """
    fitted: list[dict] = []
    for group in groups:

        longest_name = f"{group['group_name']}_s{len(group['queries'])}"

        def measure_part(qs: list[dict], base: dict = group, name: str = longest_name) -> int:
            return measure({**base, "group_name": name, "queries": qs})

        parts = _halve_to_fit(group["queries"], measure_part, max_chars)
        if len(parts) == 1:
            fitted.append(group)
            continue
        for n, part in enumerate(parts, start=1):
            fitted.append({**group, "group_name": f"{group['group_name']}_s{n}", "queries": part})
    return fitted


def _halve_to_fit(
    queries: list[dict], measure: Callable[[list[dict]], int], max_chars: int
) -> list[list[dict]]:
    if len(queries) <= 1 or measure(queries) <= max_chars:
        return [queries]
    mid = (len(queries) + 1) // 2
    return _halve_to_fit(queries[:mid], measure, max_chars) + _halve_to_fit(
        queries[mid:], measure, max_chars
    )


def _group_input(
    job_id: str,
    database_name: str,
    engine: str,
    idx: int,
    group: dict,
    group_tables: list[dict],
    collector_output: dict,
    analysis_output: dict,
    total_queries: int,
) -> dict:
    return build_group_input(
        job_id=job_id,
        database_name=database_name,
        engine=engine,
        group_index=idx,
        group_name=group["group_name"],
        primary_tables=group["primary_tables"],
        group_queries=group["queries"],
        group_tables=group_tables,
        collector_output=collector_output,
        analysis_output=analysis_output,
        total_queries=total_queries,
    )


def split_schema_input(
    job_id: str,
    database_name: str,
    engine: str,
    collector_output: dict,
    analysis_output: dict,
    queries: list[dict],
    store: ArtifactStore,
    schema_version: int = 1,
    co_dependency_groups: list[list[str]] | None = None,
) -> SchemaDesignGroupsManifest:
    """Split schema design input into groups and write per-group input files.

    Args:
        job_id: Pipeline job ID.
        database_name: Source database name.
        engine: Target engine (dynamodb, documentdb, opensearch, elasticache).
        collector_output: Full collector output dict.
        analysis_output: Full analysis output dict.
        queries: Filtered query patterns (only those assigned to this engine).
        store: ArtifactStore for writing artifacts.
        schema_version: Schema version number for artifact paths.
        co_dependency_groups: The assignment's co-dependency groups (query-id
            groups sharing significant JOINs). Folded into table clustering so
            co-dependent queries are designed together.

    Returns:
        SchemaDesignGroupsManifest with group entries. A query that touches no
        source table (``has_no_source_table``, #276/#369) is left out of every
        group -- there is nothing to design it against, and routing it into a
        real table's group would ask that table's design to invent access
        patterns for a catalog/utility statement it cannot serve either. It is
        listed in the manifest's ``excluded_queries`` instead, so merge and
        synthesis can account for it rather than silently dropping it. A query
        that names a real table the collector's schema does not have (a
        genuine resolution failure, not a pseudo table) is NOT excluded: it
        stays in normal grouping, visible if it still can't be designed.
    """
    all_tables = collector_output.get("database_schema", {}).get("tables", [])
    # Built once and reused for every group (367-1): the same resolution
    # filter_collector_for_assignment uses for the table-filtering join, so
    # a group's tables don't regress to an exact-string match once an engine
    # has more than one group.
    resolver = TableNameResolver.from_collector(collector_output)
    design_queries = [q for q in queries if not has_no_source_table(q)]
    excluded_queries = [
        ExcludedQuery(query_id=str(q["query_id"]), reason=NO_SOURCE_TABLE_REASON)
        for q in queries
        if has_no_source_table(q) and q.get("query_id") is not None
    ]
    groups = build_groups(
        design_queries,
        database_name,
        collector_output,
        analysis_output,
        co_dependency_groups=co_dependency_groups,
    )
    base_key = f"{database_name}/{job_id}/schema-{engine}/v{schema_version}"

    def measure(group: dict) -> int:
        tables = tables_for_queries(group["queries"], all_tables, resolver)
        data = _group_input(
            job_id,
            database_name,
            engine,
            len(design_queries),  # no group index is larger (one group has 1+ queries)
            group,
            tables,
            collector_output,
            analysis_output,
            len(design_queries),
        )
        text = render_group_input(data)
        if len(read_pages(text)) > MAX_GROUP_INPUT_PAGES:
            # Whole-line pages can pack under the character budget into more
            # pages than intended; count that as over budget too.
            return MAX_GROUP_INPUT_CHARS + 1
        return len(text)

    manifest_groups: list[SchemaDesignGroupEntry] = []

    for idx, group in enumerate(fit_groups_to_budget(groups, measure)):
        group_queries = group["queries"]
        group_tables = tables_for_queries(group_queries, all_tables, resolver)
        text = render_group_input(
            _group_input(
                job_id,
                database_name,
                engine,
                idx,
                group,
                group_tables,
                collector_output,
                analysis_output,
                len(design_queries),
            )
        )
        input_file = f"input_group_{idx}.json"
        store.write_bytes(f"{base_key}/{input_file}", text.encode())

        manifest_groups.append(
            SchemaDesignGroupEntry(
                group_index=idx,
                group_name=group["group_name"],
                primary_tables=group["primary_tables"],
                query_count=len(group_queries),
                table_count=len(group_tables),
                input_file=input_file,
                input_pages=read_pages(text),
            )
        )

    manifest = SchemaDesignGroupsManifest(
        job_id=job_id,
        database_name=database_name,
        target_engine=engine,
        total_queries=len(queries),
        total_groups=len(manifest_groups),
        groups=manifest_groups,
        excluded_queries=excluded_queries,
    )

    store.write_json(f"{base_key}/groups_manifest.json", manifest.model_dump())

    return manifest
