# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Co-dependency groups: queries joined closely enough to move between engines as one."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

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


# ---------------------------------------------------------------------------
# A group's engine (ADR-031 Decision 4, Amendment 4, #727)
# ---------------------------------------------------------------------------


# A group's score is rounded to this many decimal places, so float noise in a
# weighted mean (``75.75000000000001`` against ``75.75``) never decides between
# two engines: scores equal to this precision are an exact tie, which goes
# through ``tie_break`` (ADR-031 Decisions 2 and 12, #992). Every pass compares
# the rounded value (the reality check rounds a group's mean to 2 places on top).
GROUP_SCORE_PLACES = 6


def group_engine_scores(
    members: Iterable[str],
    candidates: Iterable[str],
    member_score: Callable[[str, str], float | None],
    weights: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """A co-dependency group's score on each candidate engine that can serve
    every member: the mean of the members' own scores there.

    ``member_score(query_id, engine)`` is the caller's shared-scorer score for
    one member, or ``None`` when that member cannot be served there (a hard or
    capability gate, or an engine that may not own it). One ``None`` drops the
    engine for the whole group: the group flies as one, so an engine that
    strands a member is never a candidate, however well it scores for the rest.
    Members are scored in the order given, and scoring an engine stops at its
    first ``None``. Candidates keep their order; no engine serving every member
    gives ``{}``.

    ``weights`` (#992, ADR-031 Amendment 9): a unit that holds a table's write
    group is judged on the call-rate-weighted mean of its members' scores, each
    member's weight from ``write_groups.unit_call_weights``. Only when ``weights``
    covers every member; otherwise (a plain co-dependency group, or a unit with
    no call statistics) the plain mean, Amendment 4's rule. Either way the mean is
    rounded to ``GROUP_SCORE_PLACES``.
    """
    member_ids = list(members)
    out: dict[str, float] = {}
    if not member_ids:
        return out
    weight = (
        {qid: float(weights[qid]) for qid in member_ids}
        if weights and all(qid in weights for qid in member_ids)
        else {qid: 1.0 for qid in member_ids}
    )
    total_weight = sum(weight.values())
    for engine in candidates:
        total = 0.0
        for qid in member_ids:
            score = member_score(qid, engine)
            if score is None:
                break
            total += weight[qid] * score
        else:
            out[engine] = round(total / total_weight, GROUP_SCORE_PLACES)
    return out
