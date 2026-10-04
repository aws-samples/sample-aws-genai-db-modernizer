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
from collections import Counter

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.agents.referee.aurora_choice import AURORA_ENGINES, pick_aurora_engine
from src.agents.referee.reality_check_request import moved_queries, moved_query_record

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

# Output budget for one validation call: room for every query of a batch to be
# flagged with a short reason, in compact JSON.
VALIDATOR_MAX_TOKENS = 4096

VALIDATOR_SYSTEM_PROMPT = (
    "You are a database migration expert validating engine consolidation decisions. "
    "Be conservative: only flag queries that are genuinely unserviceable. "
    "Most queries can be served by any engine with proper modeling. "
    "Respond with JSON only.\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE
)


class ValidationFailed(Exception):
    """An LLM validation call ran but gave no usable verdict (error or bad JSON)."""


def validate_consolidations(
    consolidations: list[dict],
    revised_assignments: list[dict],
    query_map: dict[str, dict],
    query_signals: dict[str, list[str]],
    original_assignments: list[dict] | None = None,
    incomplete: list[dict] | None = None,
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
        original_assignments: the assignments before consolidation; with them a
            moved query is found by its engine change (see ``moved_queries``)
        incomplete: when given, one entry is appended per batch that got no
            verdict (``from_engine``, ``to_engine``, ``batch`` as "start-end"
            1-based of ``total``, ``error``); its queries keep the move

    Returns:
        List of correction dicts: [{"query_id": str, "original_engine": str,
        "failed_target": str, "reason": str}] — queries that should be moved back,
        each one of the consolidation's moved queries. Empty list if all
        consolidations are valid or LLM is unavailable.
    """
    if not consolidations:
        return []

    corrections: list[dict] = []
    reviewed: dict[tuple[str, str], set[str]] = {}

    for consolidation in consolidations:
        from_engine = consolidation["from_engine"]
        to_engine = consolidation["to_engine"]
        seen = reviewed.setdefault((from_engine, to_engine), set())

        # The same records the external request lists for this consolidation
        # (#285); a query listed under an earlier record of the same move is not
        # sent twice.
        query_details = [
            moved_query_record(qa["query_id"], query_map, query_signals)
            for qa in moved_queries(consolidation, revised_assignments, original_assignments)
            if qa["query_id"] not in seen
        ]
        seen.update(q["query_id"] for q in query_details)
        total = len(query_details)

        # Every moved query is reviewed, MAX_QUERIES_PER_CALL per call
        for start in range(0, total, MAX_QUERIES_PER_CALL):
            batch = query_details[start : start + MAX_QUERIES_PER_CALL]
            batch_range = f"{start + 1}-{start + len(batch)}"
            try:
                flagged = _call_llm_validator(
                    from_engine=from_engine,
                    to_engine=to_engine,
                    queries=batch,
                    batch=(batch_range, total),
                )
            except ValidationFailed as exc:
                logger.warning(
                    "Consolidation %s -> %s: validation of queries %s of %d failed (%s); "
                    "those moves stand unreviewed",
                    from_engine,
                    to_engine,
                    batch_range,
                    total,
                    exc,
                )
                if incomplete is not None:
                    incomplete.append(
                        {
                            "from_engine": from_engine,
                            "to_engine": to_engine,
                            "batch": batch_range,
                            "total": total,
                            "error": str(exc)[:300],
                        }
                    )
                continue
            batch_ids = {q["query_id"] for q in batch}
            for entry in flagged:
                if entry["query_id"] not in batch_ids:
                    logger.warning(
                        "Consolidation %s -> %s: dropped a correction for %r, "
                        "which is not one of the queries under review",
                        from_engine,
                        to_engine,
                        str(entry["query_id"])[:80],
                    )
                    continue
                corrections.append(
                    {
                        "query_id": entry["query_id"],
                        "original_engine": from_engine,
                        "failed_target": to_engine,
                        "reason": entry.get("reason", "LLM flagged as unserviceable"),
                    }
                )

    return corrections


def corrections_for_moved_queries(
    corrections: list[dict],
    consolidations: list[dict],
    revised_assignments: list[dict],
    original_assignments: list[dict] | None,
) -> list[dict]:
    """Keep only corrections naming a query a consolidation moved; log the rest.

    A correction matches when its ``query_id`` is among the moved queries of a
    consolidation out of its ``original_engine`` (and into its ``failed_target``,
    when it names one). Anything else (an unknown id, a query no consolidation
    moved, the wrong source engine) would move a query the review never covered.
    """
    moved: dict[tuple[str, str], set[str]] = {}
    for c in consolidations:
        ids = {qa["query_id"] for qa in moved_queries(c, revised_assignments, original_assignments)}
        moved.setdefault((c["from_engine"], c["to_engine"]), set()).update(ids)

    kept: list[dict] = []
    for corr in corrections:
        qid = corr.get("query_id") if isinstance(corr, dict) else None
        source = corr.get("original_engine") if isinstance(corr, dict) else None
        target = corr.get("failed_target") if isinstance(corr, dict) else None
        if any(
            qid in ids and src == source and (not target or dst == target)
            for (src, dst), ids in moved.items()
        ):
            kept.append(corr)
        else:
            logger.warning(
                "Dropped a consolidation correction for %r from %r: not one of the "
                "queries a consolidation moved",
                str(qid)[:80],
                str(source)[:40],
            )
    return kept


def apply_corrections(
    corrections: list[dict],
    revised_assignments: list[dict],
    consolidations: list[dict],
    surviving_engines: set[str] | None = None,
    all_original_engines: set[str] | None = None,
    source_engine: str = "",
) -> tuple[list[dict], list[dict]]:
    """Apply LLM corrections back to the assignments and consolidations.

    When a query can't be served by its consolidation target, redirects it to
    a committed Aurora engine if one exists (these are relational patterns that
    Aurora handles natively). Falls back to the original engine only if no
    Aurora is available. With both Aurora engines available, the one matching
    the source database's dialect wins, then the one with more queries, then
    Aurora PostgreSQL (``pick_aurora_engine``, #288).

    Args:
        corrections: queries flagged as unserviceable on target
        revised_assignments: current query assignments
        consolidations: consolidation records
        surviving_engines: engines committed in the architecture (used
            to determine if Aurora is available for redirection)
        all_original_engines: all engines from the pre-consolidation
            distribution (used as a broader pool to find Aurora targets
            even if Aurora was temporarily consolidated)
        source_engine: the source database engine (e.g. ``"mysql"``)

    Returns:
        (updated_assignments, updated_consolidations)
    """
    if not corrections:
        return revised_assignments, consolidations

    # Determine redirect target: prefer Aurora over original engine.
    # Check both surviving engines AND original engines — Aurora may have been
    # temporarily consolidated but is still a valid relational safety net.
    candidate_pool = (surviving_engines or set()) | (all_original_engines or set())
    redirect_engine = pick_aurora_engine(
        candidate_pool,
        source_engine,
        Counter(qa["assigned_engine"] for qa in revised_assignments),
    )

    # Build correction lookup: query_id → original_engine. A correction without
    # ``failed_target`` (the external validator's shape) failed on the engine its
    # query sits on now; without this it matched every consolidation out of its
    # original engine and deleted the smaller ones (#218).
    current_engine = {qa["query_id"]: qa["assigned_engine"] for qa in revised_assignments}
    corrections = [
        {**c, "failed_target": c.get("failed_target") or current_engine.get(c["query_id"])}
        for c in corrections
    ]
    correction_map = {c["query_id"]: c for c in corrections}

    def _target(corr: dict) -> str:
        return redirect_engine if redirect_engine else corr["original_engine"]

    # Move queries to Aurora (or back to original if no Aurora available)
    for qa in revised_assignments:
        if qa["query_id"] in correction_map:
            corr = correction_map[qa["query_id"]]
            target = _target(corr)
            qa["assigned_engine"] = target
            if target != corr["original_engine"]:
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
        reversed_corrections = [
            corr
            for corr in corrections
            if corr["original_engine"] == from_engine and corr["failed_target"] == to_engine
        ]
        reversed_ids = [corr["query_id"] for corr in reversed_corrections]

        if not reversed_ids:
            updated_consolidations.append(c)
            continue

        remaining_moved = c["query_count"] - len(reversed_ids)
        if remaining_moved <= 0:
            # Full reversal — remove this consolidation entirely
            continue

        # Partial — update the consolidation record. ``query_count`` is the number
        # this consolidation proposed to move, reversed queries included (#218).
        original_total = c["query_count"]
        redirected = sum(1 for corr in reversed_corrections if _target(corr) != from_engine)
        stayed = len(reversed_ids) - redirected
        parts = []
        if stayed:
            parts.append(f"{stayed} {'stays' if stayed == 1 else 'stay'} on {from_engine}")
        if redirected:
            parts.append(f"{redirected} redirected to {redirect_engine}")
        reason_suffix = " and ".join(parts) + f" (unserviceable on {to_engine})"
        if redirected:
            retention_reason = (
                f"Redirected to {redirect_engine} "
                f"(relational patterns unserviceable on {to_engine})"
            )
        else:
            retention_reason = f"LLM validation flagged these as unserviceable on {to_engine}"

        updated_consolidations.append(
            {
                **c,
                "query_count": remaining_moved,
                # The source engine stays, so no cluster is avoided.
                "saved_cost_estimate": 0,
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
    source_engine: str = "",
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
        source_engine: the source database engine (e.g. ``"mysql"``)

    Returns:
        (updated_assignments, updated_consolidations) — may be unchanged
    """
    # Count queries per engine
    engine_counts = Counter(qa["assigned_engine"] for qa in revised_assignments)

    # Committed Aurora engine: the source's dialect, else the one with the most
    # queries, else PostgreSQL (#288)
    aurora_target = pick_aurora_engine(engine_counts, source_engine, engine_counts)
    if aurora_target is None:
        return revised_assignments, consolidations

    # Find orphan engines (non-Aurora, few queries, not the primary engine)
    primary_engine = max(engine_counts, key=lambda e: engine_counts[e])

    for engine, count in list(engine_counts.items()):
        if engine in AURORA_ENGINES:
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
    batch: tuple[str, int] | None = None,
) -> list[dict]:
    """Call the LLM to validate whether target engine can serve the queries.

    Returns list of flagged queries: [{"query_id": str, "reason": str}]. Raises
    :class:`ValidationFailed` when no LLM gave a usable verdict (none available,
    a call error, or a response that is not the JSON array asked for).
    """
    errors: list[str] = []
    # Try Strands (production), fall back to boto3 (local)
    for attempt in (_try_strands_validator, _try_boto3_validator):
        try:
            result = attempt(from_engine, to_engine, queries, batch)
        except ValidationFailed as exc:
            errors.append(str(exc))
            continue
        if result is not None:
            return result

    raise ValidationFailed("; ".join(errors) or "no LLM available")


def _build_validation_prompt(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> str:
    """Build the validation prompt for the LLM.

    The query records (SQL, table names) are customer data: they are framed as
    untrusted data, never instructions. ``batch`` is ``("start-end", total)``
    when the consolidation's moved queries are reviewed in several calls.
    """
    target_context = ENGINE_CONTEXT.get(to_engine, f"{to_engine} database")

    queries_block = ""
    for q in queries:
        signals_str = f" [signals: {', '.join(q['signals'])}]" if q["signals"] else ""
        queries_block += (
            f"- {q['query_id']} ({q['type']}, {q['cps']:.1f} cps, "
            f"tables: {', '.join(q['tables'])}){signals_str}\n"
            f"  SQL: {q['sql']}\n\n"
        )

    if batch is not None and batch[1] > len(queries):
        scope = f"queries {batch[0]} of the {batch[1]} moved"
    else:
        scope = f"{len(queries)} queries"
    return (
        f"You are validating a database migration consolidation decision.\n\n"
        f"DECISION: Move {scope} from {from_engine} to {to_engine}.\n\n"
        f"TARGET ENGINE CAPABILITIES:\n{target_context}\n\n"
        f"QUERIES BEING MOVED:\n" + frame_untrusted(queries_block, label="moved queries") + "\n\n"
        f"TASK: For each query above, determine if {to_engine} can realistically serve it "
        f"without significant degradation. Consider:\n"
        f"- Can the access pattern be modeled naturally on {to_engine}?\n"
        f"- Would it require extreme denormalization or client-side processing?\n"
        f"- Is the query pattern fundamentally incompatible with {to_engine}'s architecture?\n\n"
        f"DO NOT flag queries just because they require denormalization — that's expected. "
        f"Only flag queries where {to_engine} is a genuinely poor fit that would result in:\n"
        f"- Unacceptable performance (full table scans on key-value store)\n"
        f"- Architectural impossibility (full-text search on DynamoDB)\n"
        f"- Unreasonable complexity (5+ GSIs for a single query pattern)\n\n"
        f"Respond with ONLY a compact JSON array of flagged queries (no indentation, "
        f"a reason of at most 15 words). If all queries are fine, respond with an "
        f"empty array [].\n"
        f'Format: [{{"query_id": "...", "reason": "brief explanation"}}]\n\n'
        f"JSON response:"
    )


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse the LLM's JSON response, handling common formatting issues.

    Returns [] for a response that is not a JSON array; see :func:`_parse_flagged`.
    """
    return _parse_flagged(response_text) or []


def _parse_flagged(response_text: str) -> list[dict] | None:
    """The flagged queries in a response, or None when it is not a JSON array."""
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM validation response: %s", text[:200])
        return None
    if not isinstance(result, list):
        logger.warning("LLM validation response is not a JSON array: %s", text[:200])
        return None
    # Validate each entry has required fields
    return [
        {"query_id": entry["query_id"], "reason": entry.get("reason", "flagged")}
        for entry in result
        if isinstance(entry, dict) and "query_id" in entry
    ]


def _report(from_engine: str, to_engine: str, queries: list[dict], flagged: list[dict]) -> None:
    if flagged:
        print(
            f"[reality-check] LLM flagged {len(flagged)}/{len(queries)} queries "
            f"as unserviceable on {to_engine}"
        )
        for f in flagged:
            print(f"[reality-check]   {f['query_id']}: {f['reason']}")
    else:
        print(f"[reality-check] LLM confirmed all {len(queries)} queries OK on {to_engine}")


def _validator_model_id() -> str:
    return os.environ.get(
        "VALIDATOR_MODEL_ID",
        os.environ.get("SUMMARY_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
    )


def _try_strands_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> list[dict] | None:
    """Validate using a Strands agent (production); None when Strands is not installed.

    Raises :class:`ValidationFailed` when the call fails or the response is unusable.
    """
    try:
        from strands import Agent
        from strands.models.bedrock import BedrockModel
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries, batch)

    try:
        model = BedrockModel(
            model_id=_validator_model_id(),
            max_tokens=VALIDATOR_MAX_TOKENS,
            temperature=0.0,
        )
        agent = Agent(
            model=model,
            system_prompt=VALIDATOR_SYSTEM_PROMPT,
            tools=[],
            callback_handler=None,
        )

        print(
            f"[reality-check] Validating {len(queries)} queries "
            f"moved {from_engine} → {to_engine}..."
        )
        response_text = str(agent(prompt)).strip()
    except Exception as exc:
        logger.error("Strands validation failed: %s", exc)
        print(f"[reality-check] LLM validation failed: {exc}")
        raise ValidationFailed(f"strands: {exc}") from exc

    flagged = _parse_flagged(response_text)
    if flagged is None:
        raise ValidationFailed("strands: response is not a JSON array (possibly truncated)")
    _report(from_engine, to_engine, queries, flagged)
    return flagged


def _try_boto3_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> list[dict] | None:
    """Validate using boto3 Bedrock directly (local development); None without boto3.

    Raises :class:`ValidationFailed` when the call fails or the response is unusable.
    """
    try:
        import boto3
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries, batch)

    try:
        client = boto3.client("bedrock-runtime")
        response = client.converse(
            modelId=_validator_model_id(),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": VALIDATOR_SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": VALIDATOR_MAX_TOKENS, "temperature": 0.0},
        )
        response_text = response["output"]["message"]["content"][0]["text"]
    except Exception as exc:
        logger.error("boto3 validation failed: %s", exc)
        raise ValidationFailed(f"boto3: {exc}") from exc

    print(f"[reality-check] Validating {len(queries)} queries moved {from_engine} → {to_engine}...")
    flagged = _parse_flagged(response_text)
    if flagged is None:
        raise ValidationFailed("boto3: response is not a JSON array (possibly truncated)")
    _report(from_engine, to_engine, queries, flagged)
    return flagged
