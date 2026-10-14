"""Co-dependency groups: queries joined closely enough to move between engines as one."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Co-dependency detection (Task 4.2)
# ---------------------------------------------------------------------------


def is_significant_join(query: dict, table: str) -> bool:
    """Determine if a query's JOIN on a table is significant (load-bearing).

    A JOIN is significant when:
    - join_count >= 2 (multi-table join)
    - has_aggregations is True (GROUP BY across joined tables; the collector's field)
    - table appears in filter_tables (WHERE clause references the joined table)

    Light JOINs that only fetch a display field do not create co-dependencies.

    Requirements: 2.3
    """
    return (
        query.get("join_count", 0) >= 2
        or query.get("has_aggregations", False)
        or table in query.get("filter_tables", [])
    )


def build_co_dependency_groups(
    queries: list[dict],
    tables: list[dict],
) -> list[list[str]]:
    """Build co-dependency groups using union-find with significance filter.

    Groups queries that share significant JOINs on the same tables.
    Only returns groups with 2+ queries.

    Requirements: 2.3
    """
    # Map: table → query_ids (input order, deduplicated) with significant JOINs
    # on that table. A dict, not a set: set order depends on PYTHONHASHSEED and
    # would leak into the group order and membership order (#288).
    table_to_queries: dict[str, dict[str, None]] = {}
    for q in queries:
        if q.get("has_joins") or q.get("join_count", 0) > 0:
            for table in q.get("tables_accessed", []):
                if is_significant_join(q, table):
                    table_to_queries.setdefault(table, {})[q["query_id"]] = None

    # Union-find with path compression
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Union queries that share significant JOINs on the same table
    for _table, query_ids in table_to_queries.items():
        query_list = list(query_ids)
        for i in range(1, len(query_list)):
            union(query_list[0], query_list[i])

    # Collect groups
    groups: dict[str, list[str]] = {}
    for qid in parent:
        root = find(qid)
        groups.setdefault(root, []).append(qid)

    # Also include query_ids that appear in table_to_queries but not in parent
    # (single-entry sets that were never unioned)
    all_qids_in_tables: dict[str, None] = {}
    for qids in table_to_queries.values():
        all_qids_in_tables.update(qids)
    for qid in all_qids_in_tables:
        if qid not in parent:
            root = find(qid)
            groups.setdefault(root, []).append(qid)

    # Only return groups with 2+ queries
    return [g for g in groups.values() if len(g) >= 2]
