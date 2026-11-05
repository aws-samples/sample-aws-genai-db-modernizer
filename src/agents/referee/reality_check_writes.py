# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who carries the writes, for the reality-check summary (#1928).

A table has one writer (ADR-031 Amendment 9): its writes sit on one engine, its
source of truth, and every other engine that reads it serves a copy kept in sync
from that engine by change data capture. The reality-check summary states this
in prose, so the model gets the facts from the assignment (:func:`write_ownership`,
in the request's ``executive_summary`` block) and finalize checks the summary
against the same facts (``reality_check_summary.check_summary_write_claims``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from src.agents.referee.write_groups import table_sources, unservable_write_groups


def write_ownership(
    assignment: Mapping | None,
    query_assignments: list[dict],
    collector_output: Mapping | None = None,
) -> dict[str, Any] | None:
    """The write owners of ``query_assignments`` (the revised ones), from ``assignment``'s
    write groups; ``None`` for an assignment written before write groups existed.

    - ``write_engines``: ``{engine: in-scope write statements on it}``;
    - ``read_only_engines``: the engines with in-scope queries and no write;
    - ``tables``: each written table's ``source_of_truth`` (``None`` when its writes
      sit on two engines), ``write_engines`` and ``replica_engines`` (the other
      engines that read it, from a copy kept in sync from the source of truth).
    """
    assignment = assignment or {}
    if "write_groups" not in assignment:
        return None
    revised = {**assignment, "query_assignments": query_assignments}
    unservable = assignment.get("unservable_queries") or []
    sources = table_sources(
        revised, unservable_write_groups(collector_output, unservable) if unservable else None
    )
    engine_of = {
        str(qa.get("query_id")): str(qa["assigned_engine"])
        for qa in query_assignments
        if qa.get("in_scope", True) and qa.get("assigned_engine")
    }
    writes = {str(q) for row in sources for q in cast(list, row["write_query_ids"])}
    write_engines: dict[str, int] = {}
    for qid in sorted(writes):
        if qid in engine_of:
            write_engines[engine_of[qid]] = write_engines.get(engine_of[qid], 0) + 1
    return {
        "write_engines": dict(sorted(write_engines.items())),
        "read_only_engines": sorted(set(engine_of.values()) - set(write_engines)),
        "tables": [
            {
                "table": row["table"],
                "source_of_truth": row["source_of_truth"],
                "write_engines": row["write_engines"],
                "replica_engines": [r["engine"] for r in cast(list, row["replicas"])],
            }
            for row in sources
        ],
    }


def result_write_ownership(result: Mapping) -> dict[str, Any] | None:
    """:func:`write_ownership` of a reality-check result (its input assignment and its
    revised assignments)."""
    return write_ownership(
        result.get("assignment"),
        list(result.get("revised_assignments") or []),
        result.get("collector_output"),
    )
