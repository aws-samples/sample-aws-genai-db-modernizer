"""Post-Schema Router — Deterministic routing of unsupported queries.

After schema design, some queries are flagged as unsupported by their assigned
engine. This router reads PE-confirmed routing notes and remaining unsupported
patterns, then maps each orphan query to the next-best engine.

This is NOT an LLM agent — it's pure deterministic Python code.
"""

from __future__ import annotations

import re
from collections import defaultdict

from src.agents.referee.engine_exclusions import check_exclusions
from src.contracts.post_schema_router_output import QueryRouting, RouterOutput

# PE notes use this prefix for confirmed routing recommendations
_ROUTING_PREFIX_RE = re.compile(
    r"\[ROUTING\]\s*query_ids=\[([^\]]+)\]\s*→\s*(\w+)\s*\|\s*reason:\s*(.+)",
    re.IGNORECASE,
)

# Engine priority for fallback routing (most general → most specialized)
ENGINE_PRIORITY = ["documentdb", "opensearch", "dynamodb"]


def _parse_pe_routing_notes(pe_notes: list[str]) -> list[dict]:
    """Parse structured [ROUTING] entries from PE notes.

    Expected format:
        [ROUTING] query_ids=[q-42,q-55] → opensearch | reason: full-text LIKE search
    """
    routings = []
    for note in pe_notes:
        match = _ROUTING_PREFIX_RE.search(note)
        if match:
            query_ids_str, target_engine, reason = match.groups()
            query_ids = [qid.strip() for qid in query_ids_str.split(",")]
            routings.append(
                {
                    "query_ids": query_ids,
                    "target_engine": target_engine.strip(),
                    "reason": reason.strip(),
                }
            )
    return routings


def _extract_unsupported_query_ids(schema_output: dict, engine: str) -> list[dict]:
    """Extract query IDs from unsupported_patterns in a schema output.

    Handles all three engine contracts:
    - DynamoDB: UnsupportedPattern has query_ids field
    - DocumentDB: UnsupportedPattern has source_query_ids field
    - OpenSearch: UnsupportedPattern has query_ids field (newly added)
    """
    unsupported = schema_output.get("unsupported_patterns", [])
    results = []
    for pattern in unsupported:
        query_ids = pattern.get("query_ids") or pattern.get("source_query_ids") or []
        reason = pattern.get("reason") or pattern.get("recommendation") or "unsupported pattern"
        if query_ids:
            results.append({"query_ids": query_ids, "reason": reason})
    return results


def _select_target_engine(
    query_id: str,
    query_text: str,
    from_engine: str,
    active_engines: list[str],
    already_failed: set[str],
) -> str | None:
    """Select the next-best engine for a query, respecting exclusions.

    Returns None if no engine can serve it (application-layer).
    """
    candidates = [
        e
        for e in ENGINE_PRIORITY
        if e != from_engine and e in active_engines and e not in already_failed
    ]

    for candidate in candidates:
        exclusion = check_exclusions(query_id, query_text, candidate)
        if exclusion is None:
            return candidate

    return None


def route_unsupported_queries(
    schema_outputs: dict[str, dict],
    active_engines: list[str],
    pe_notes_by_engine: dict[str, list[str]] | None = None,
    query_texts: dict[str, str] | None = None,
    cascade_depth: int = 0,
    max_depth: int = 2,
    already_routed: set[str] | None = None,
    lightweight_query_ids: set[str] | None = None,
) -> RouterOutput:
    """Route unsupported queries to the next-best engine.

    Args:
        schema_outputs: {engine: schema_output_dict} for each engine
        active_engines: engines selected by triage
        pe_notes_by_engine: {engine: list of pe_notes strings} — optional
        query_texts: {query_id: SQL text} for exclusion checking
        cascade_depth: current depth (0 = first pass)
        max_depth: maximum cascade depth before declaring terminal
        already_routed: query IDs already routed in prior passes

    Returns:
        RouterOutput with routing decisions and terminal queries
    """
    if already_routed is None:
        already_routed = set()
    if query_texts is None:
        query_texts = {}
    if pe_notes_by_engine is None:
        pe_notes_by_engine = {}
    if lightweight_query_ids is None:
        lightweight_query_ids = set()

    routings: list[QueryRouting] = []
    terminal: list[str] = []
    seen_query_ids: set[str] = set()

    # Track which engines have already failed each query
    failed_engines: dict[str, set[str]] = defaultdict(set)

    for engine, output in schema_outputs.items():
        # 1. Parse PE routing notes (highest priority — PE confirmed these)
        pe_notes = pe_notes_by_engine.get(engine, [])
        pe_routings = _parse_pe_routing_notes(pe_notes)
        for routing in pe_routings:
            for qid in routing["query_ids"]:
                if qid in seen_query_ids or qid in already_routed:
                    continue
                if qid in lightweight_query_ids:
                    continue
                seen_query_ids.add(qid)
                failed_engines[qid].add(engine)

                target = routing["target_engine"]
                # Validate target is active and not excluded
                sql = query_texts.get(qid, "")
                if target in active_engines:
                    exclusion = check_exclusions(qid, sql, target) if sql else None
                    if exclusion is None:
                        routings.append(
                            QueryRouting(
                                query_id=qid,
                                from_engine=engine,
                                to_engine=target,
                                reason=routing["reason"],
                                cascade_depth=cascade_depth,
                            )
                        )
                        continue

                # PE-suggested target is invalid — fall back to priority
                fallback = _select_target_engine(
                    qid, sql, engine, active_engines, failed_engines[qid]
                )
                if fallback:
                    routings.append(
                        QueryRouting(
                            query_id=qid,
                            from_engine=engine,
                            to_engine=fallback,
                            reason=routing["reason"],
                            cascade_depth=cascade_depth,
                        )
                    )
                else:
                    terminal.append(qid)

        # 2. Extract remaining unsupported patterns (not already handled by PE notes)
        unsupported = _extract_unsupported_query_ids(output, engine)
        for entry in unsupported:
            for qid in entry["query_ids"]:
                if qid in seen_query_ids or qid in already_routed:
                    continue
                if qid in lightweight_query_ids:
                    continue
                seen_query_ids.add(qid)
                failed_engines[qid].add(engine)

                sql = query_texts.get(qid, "")

                if cascade_depth >= max_depth:
                    terminal.append(qid)
                    continue

                target = _select_target_engine(
                    qid, sql, engine, active_engines, failed_engines[qid]
                )
                if target:
                    routings.append(
                        QueryRouting(
                            query_id=qid,
                            from_engine=engine,
                            to_engine=target,
                            reason=entry["reason"],
                            cascade_depth=cascade_depth,
                        )
                    )
                else:
                    terminal.append(qid)

    return RouterOutput(
        job_id="",  # Caller sets this
        routings=routings,
        terminal_queries=terminal,
        cascade_depth=cascade_depth,
    )
