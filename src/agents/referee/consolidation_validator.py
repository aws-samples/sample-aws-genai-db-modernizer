"""Consolidation Validator — LLM-based validation of deterministic consolidation decisions.

After the deterministic reality check decides to consolidate queries from one engine
to another, this module asks an LLM to validate: "Can the target engine actually serve
these query patterns, or will they fail during schema design?"

This prevents the flip-flop UX: "You don't need DocumentDB" → schema design fails →
"Actually you do need DocumentDB." The LLM catches soft failures that regex/scoring miss.

Architecture:
- Production: Strands + Bedrock (same pattern as executive summary)
- Local: boto3 direct call (optional, skipped if unavailable)
- Graceful degradation: if no LLM available, consolidation proceeds unchanged
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# Engine-specific context for the LLM to understand architectural constraints
ENGINE_CONTEXT: dict[str, str] = {
    "dynamodb": (
        "DynamoDB is a key-value/document store. Access patterns MUST be defined upfront. "
        "It excels at: single-item lookups by primary key, range queries on sort key, "
        "denormalized aggregates via GSI. "
        "It struggles with: ad-hoc queries, multi-table JOINs (requires denormalization), "
        "full-text search (no LIKE '%...%'), complex aggregations across partitions, "
        "queries that scan large datasets without a known partition key, "
        "transactions spanning many items (25 item limit)."
    ),
    "documentdb": (
        "DocumentDB is a document database (MongoDB-compatible). "
        "It excels at: flexible schemas, nested document queries, aggregation pipelines, "
        "multi-document ACID transactions, $regex text matching (basic), JOINs via $lookup. "
        "It struggles with: full-text search at scale (no inverted index), "
        "extreme write throughput (single-leader architecture), "
        "queries requiring horizontal scaling beyond a few TB."
    ),
    "opensearch": (
        "OpenSearch is a search and analytics engine. "
        "It excels at: full-text search, fuzzy matching, aggregations, analytics, "
        "time-series data, geo-spatial queries, faceted search. "
        "It struggles with: ACID transactions, strong consistency, "
        "primary write path (not a source of truth), complex relational queries, "
        "frequent single-document updates (reindexing cost)."
    ),
}

# Maximum queries to send in one validation call (token budget)
MAX_QUERIES_PER_CALL = 30


def validate_consolidations(
    consolidations: list[dict],
    revised_assignments: list[dict],
    query_map: dict[str, dict],
    query_signals: dict[str, list[str]],
) -> list[dict]:
    """Validate consolidation decisions using an LLM.

    For each consolidation, asks the LLM whether the target engine can
    actually serve the moved queries. Returns a list of corrections:
    queries that should NOT have been moved.

    Args:
        consolidations: List of consolidation dicts from run_reality_check()
        revised_assignments: The revised query assignments after consolidation
        query_map: {query_id: query_pattern_dict} from collector
        query_signals: {query_id: [signal_names]} from triage

    Returns:
        List of correction dicts: [{"query_id": str, "original_engine": str,
        "reason": str}] — queries that should be moved back.
        Empty list if all consolidations are valid or LLM is unavailable.
    """
    if not consolidations:
        return []

    # Build the validation requests per consolidation
    corrections: list[dict] = []

    for consolidation in consolidations:
        from_engine = consolidation["from_engine"]
        to_engine = consolidation["to_engine"]

        # Find the queries that were moved in this consolidation
        moved_queries = [
            qa
            for qa in revised_assignments
            if qa["assigned_engine"] == to_engine
            and f"consolidated from {from_engine}" in qa.get("assignment_reason", "")
        ]

        if not moved_queries:
            continue

        # Build query details for the LLM
        query_details = []
        for qa in moved_queries[:MAX_QUERIES_PER_CALL]:
            qid = qa["query_id"]
            q = query_map.get(qid, {})
            signals = query_signals.get(qid, [])
            query_details.append(
                {
                    "query_id": qid,
                    "sql": q.get("query_text", "")[:500],  # Truncate long SQL
                    "type": q.get("query_type", ""),
                    "tables": q.get("tables_accessed", []),
                    "signals": signals,
                    "cps": q.get("calls_per_second", 0),
                }
            )

        # Call LLM for validation
        flagged = _call_llm_validator(
            from_engine=from_engine,
            to_engine=to_engine,
            queries=query_details,
        )

        for entry in flagged:
            corrections.append(
                {
                    "query_id": entry["query_id"],
                    "original_engine": from_engine,
                    "failed_target": to_engine,
                    "reason": entry.get("reason", "LLM flagged as unserviceable"),
                }
            )

    return corrections


def apply_corrections(
    corrections: list[dict],
    revised_assignments: list[dict],
    consolidations: list[dict],
    surviving_engines: set[str] | None = None,
    all_original_engines: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Apply LLM corrections back to the assignments and consolidations.

    When a query can't be served by its consolidation target, redirects it to
    a committed Aurora engine if one exists (these are relational patterns that
    Aurora handles natively). Falls back to the original engine only if no
    Aurora is available.

    Args:
        corrections: queries flagged as unserviceable on target
        revised_assignments: current query assignments
        consolidations: consolidation records
        surviving_engines: engines committed in the architecture (used
            to determine if Aurora is available for redirection)
        all_original_engines: all engines from the pre-consolidation
            distribution (used as a broader pool to find Aurora targets
            even if Aurora was temporarily consolidated)

    Returns:
        (updated_assignments, updated_consolidations)
    """
    if not corrections:
        return revised_assignments, consolidations

    # Determine redirect target: prefer Aurora over original engine.
    # Check both surviving engines AND original engines — Aurora may have been
    # temporarily consolidated but is still a valid relational safety net.
    aurora_engines = {"aurora_postgresql", "aurora_mysql"}
    redirect_engine: str | None = None
    candidate_pool = (surviving_engines or set()) | (all_original_engines or set())
    committed_aurora = aurora_engines & candidate_pool
    if committed_aurora:
        # Prefer PG (broader capability set), then MySQL
        if "aurora_postgresql" in committed_aurora:
            redirect_engine = "aurora_postgresql"
        else:
            redirect_engine = committed_aurora.pop()

    # Build correction lookup: query_id → original_engine
    correction_map = {c["query_id"]: c for c in corrections}

    # Move queries to Aurora (or back to original if no Aurora available)
    for qa in revised_assignments:
        if qa["query_id"] in correction_map:
            corr = correction_map[qa["query_id"]]
            target = redirect_engine if redirect_engine else corr["original_engine"]
            qa["assigned_engine"] = target
            if redirect_engine:
                qa["assignment_reason"] = (
                    f"reality check: redirected to {redirect_engine} "
                    f"(unserviceable on consolidation target: {corr['reason']})"
                )
            else:
                qa["assignment_reason"] = (
                    f"reality check: consolidation reversed by validation " f"({corr['reason']})"
                )

    # Update consolidation records
    updated_consolidations = []
    for c in consolidations:
        from_engine = c["from_engine"]
        to_engine = c["to_engine"]
        # Count how many queries from this consolidation were reversed
        # Match on both original_engine AND failed_target to avoid double-counting
        reversed_ids = [
            corr["query_id"]
            for corr in corrections
            if corr["original_engine"] == from_engine
            and corr.get("failed_target", to_engine) == to_engine
        ]

        if not reversed_ids:
            updated_consolidations.append(c)
            continue

        remaining_moved = c["query_count"] - len(reversed_ids)
        if remaining_moved <= 0:
            # Full reversal — remove this consolidation entirely
            continue

        # Partial — update the consolidation record
        original_total = c["query_count"] + len(reversed_ids)
        if redirect_engine:
            retention_reason = (
                f"Redirected to {redirect_engine} "
                f"(relational patterns unserviceable on {to_engine})"
            )
            reason_suffix = f"{len(reversed_ids)} redirected to {redirect_engine}"
        else:
            retention_reason = "LLM validation flagged these as unserviceable on target engine"
            reason_suffix = f"{len(reversed_ids)} retained (unserviceable on target)"

        updated_consolidations.append(
            {
                **c,
                "query_count": remaining_moved,
                "reason": (  # nosemgrep: string-concat-in-list
                    f"Partial consolidation: {remaining_moved} of {original_total} "
                    f"{from_engine} queries moved to {c['to_engine']}; "
                    f"{reason_suffix}"
                ),
                "action": "partial",
                "queries_retained": reversed_ids,
                "retention_reason": retention_reason,
            }
        )

    return revised_assignments, updated_consolidations


# ---------------------------------------------------------------------------
# Sanity sweep — catch orphan engines after all consolidations and corrections
# ---------------------------------------------------------------------------

# Engines with this many or fewer queries (and no hard capability requirements)
# are candidates for the sanity sweep redirect.
ORPHAN_ENGINE_THRESHOLD = 5


def sanity_sweep(
    revised_assignments: list[dict],
    consolidations: list[dict],
    query_capabilities: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    """Final pass: redirect tiny orphan engines to Aurora if available.

    After all consolidations and LLM corrections, some engines may survive
    with very few queries (e.g., 3 trivial queries that got bounced back).
    If those queries have no hard capability requirements and an Aurora engine
    is committed, redirect them there.

    This prevents the "DocumentDB for 3 SHOW FIELDS commands" problem.

    Args:
        revised_assignments: current query assignments (post-corrections)
        consolidations: current consolidation records
        query_capabilities: {query_id: [capability_names]} from triage

    Returns:
        (updated_assignments, updated_consolidations) — may be unchanged
    """
    from collections import Counter

    # Count queries per engine
    engine_counts = Counter(qa["assigned_engine"] for qa in revised_assignments)

    # Find committed Aurora engine (the one with the most queries)
    aurora_engines = {"aurora_postgresql", "aurora_mysql"}
    committed_aurora = [e for e in engine_counts if e in aurora_engines]
    if not committed_aurora:
        return revised_assignments, consolidations

    # Pick the Aurora engine with the most queries (it's the primary relational target)
    aurora_target = max(committed_aurora, key=lambda e: engine_counts[e])

    # Find orphan engines (non-Aurora, few queries, not the primary engine)
    primary_engine = max(engine_counts, key=lambda e: engine_counts[e])

    for engine, count in list(engine_counts.items()):
        if engine in aurora_engines:
            continue
        if engine == primary_engine:
            continue
        if count > ORPHAN_ENGINE_THRESHOLD:
            continue

        # Check if ALL queries in this engine have no hard capability requirements
        engine_qids = [
            qa["query_id"] for qa in revised_assignments if qa["assigned_engine"] == engine
        ]
        has_hard_caps = any(query_capabilities.get(qid, []) for qid in engine_qids)
        if has_hard_caps:
            continue

        # Redirect all queries from this orphan engine to Aurora
        for qa in revised_assignments:
            if qa["assigned_engine"] == engine:
                qa["assigned_engine"] = aurora_target
                qa["assignment_reason"] = (
                    f"sanity sweep: redirected from {engine} to {aurora_target} "
                    f"(orphan engine with {count} queries, no hard capability requirements)"
                )

        # Record as a consolidation
        consolidations.append(
            {
                "from_engine": engine,
                "to_engine": aurora_target,
                "query_count": count,
                "reason": (
                    f"Sanity sweep: {engine} had only {count} queries with no hard "
                    f"capability requirements — redirected to {aurora_target}"
                ),
                "saved_cost_estimate": 500.0,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        )

        print(
            f"[reality-check] Sanity sweep: {engine} ({count} queries) "
            f"→ {aurora_target} (no hard capabilities required)"
        )

    return revised_assignments, consolidations


def _call_llm_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
) -> list[dict]:
    """Call the LLM to validate whether target engine can serve the queries.

    Returns list of flagged queries: [{"query_id": str, "reason": str}]
    """
    # Try Strands (production), fall back to boto3 (local), skip if neither available
    result = _try_strands_validator(from_engine, to_engine, queries)
    if result is not None:
        return result

    result = _try_boto3_validator(from_engine, to_engine, queries)
    if result is not None:
        return result

    logger.info("No LLM available for consolidation validation — skipping")
    return []


def _build_validation_prompt(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
) -> str:
    """Build the validation prompt for the LLM."""
    target_context = ENGINE_CONTEXT.get(to_engine, f"{to_engine} database")

    queries_block = ""
    for q in queries:
        signals_str = f" [signals: {', '.join(q['signals'])}]" if q["signals"] else ""
        queries_block += (
            f"- {q['query_id']} ({q['type']}, {q['cps']:.1f} cps, "
            f"tables: {', '.join(q['tables'])}){signals_str}\n"
            f"  SQL: {q['sql']}\n\n"
        )

    return (
        f"You are validating a database migration consolidation decision.\n\n"
        f"DECISION: Move {len(queries)} queries from {from_engine} to {to_engine}.\n\n"
        f"TARGET ENGINE CAPABILITIES:\n{target_context}\n\n"
        f"QUERIES BEING MOVED:\n{queries_block}\n"
        f"TASK: For each query, determine if {to_engine} can realistically serve it "
        f"without significant degradation. Consider:\n"
        f"- Can the access pattern be modeled naturally on {to_engine}?\n"
        f"- Would it require extreme denormalization or client-side processing?\n"
        f"- Is the query pattern fundamentally incompatible with {to_engine}'s architecture?\n\n"
        f"DO NOT flag queries just because they require denormalization — that's expected. "
        f"Only flag queries where {to_engine} is a genuinely poor fit that would result in:\n"
        f"- Unacceptable performance (full table scans on key-value store)\n"
        f"- Architectural impossibility (full-text search on DynamoDB)\n"
        f"- Unreasonable complexity (5+ GSIs for a single query pattern)\n\n"
        f"Respond with ONLY a JSON array of flagged queries. If all queries are fine, "
        f"respond with an empty array [].\n"
        f'Format: [{{"query_id": "...", "reason": "brief explanation"}}]\n\n'
        f"JSON response:"
    )


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse the LLM's JSON response, handling common formatting issues."""
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            # Validate each entry has required fields
            return [
                {"query_id": entry["query_id"], "reason": entry.get("reason", "flagged")}
                for entry in result
                if isinstance(entry, dict) and "query_id" in entry
            ]
    except (json.JSONDecodeError, KeyError):
        logger.warning("Failed to parse LLM validation response: %s", text[:200])

    return []


def _try_strands_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
) -> list[dict] | None:
    """Try to validate using Strands agent (production)."""
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries)

    try:
        model = BedrockModel(
            model_id=os.environ.get(
                "VALIDATOR_MODEL_ID",
                os.environ.get("SUMMARY_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
            ),
            max_tokens=2048,
            temperature=0.0,
        )
        agent = Agent(
            model=model,
            system_prompt=(
                "You are a database migration expert validating engine consolidation decisions. "
                "Be conservative: only flag queries that are genuinely unserviceable. "
                "Most queries can be served by any engine with proper modeling. "
                "Respond with JSON only."
            ),
            tools=[],
            callback_handler=None,
        )

        print(
            f"[reality-check] Validating {len(queries)} queries "
            f"moved {from_engine} → {to_engine}..."
        )
        result = agent(prompt)
        response_text = str(result).strip()
        flagged = _parse_llm_response(response_text)

        if flagged:
            print(
                f"[reality-check] LLM flagged {len(flagged)}/{len(queries)} queries "
                f"as unserviceable on {to_engine}"
            )
            for f in flagged:
                print(f"[reality-check]   {f['query_id']}: {f['reason']}")
        else:
            print(f"[reality-check] LLM confirmed all {len(queries)} queries OK on {to_engine}")

        return flagged

    except Exception as exc:
        logger.error("Strands validation failed: %s", exc)
        print(f"[reality-check] LLM validation failed: {exc}")
        return None


def _try_boto3_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
) -> list[dict] | None:
    """Try to validate using boto3 Bedrock directly (local development)."""
    try:
        import boto3
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries)
    model_id = os.environ.get(
        "VALIDATOR_MODEL_ID",
        os.environ.get("SUMMARY_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
    )

    try:
        client = boto3.client("bedrock-runtime")
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[
                {
                    "text": (
                        "You are a database migration expert validating engine consolidation decisions. "
                        "Be conservative: only flag queries that are genuinely unserviceable. "
                        "Most queries can be served by any engine with proper modeling. "
                        "Respond with JSON only."
                    )
                }
            ],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
        )

        response_text = response["output"]["message"]["content"][0]["text"]
        flagged = _parse_llm_response(response_text)

        print(
            f"[reality-check] Validating {len(queries)} queries "
            f"moved {from_engine} → {to_engine}..."
        )
        if flagged:
            print(
                f"[reality-check] LLM flagged {len(flagged)}/{len(queries)} queries "
                f"as unserviceable on {to_engine}"
            )
            for f in flagged:
                print(f"[reality-check]   {f['query_id']}: {f['reason']}")
        else:
            print(f"[reality-check] LLM confirmed all {len(queries)} queries OK on {to_engine}")

        return flagged

    except Exception as exc:
        logger.error("boto3 validation failed: %s", exc)
        return None
