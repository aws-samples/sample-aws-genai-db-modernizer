"""Cache overlay: ElastiCache fronts hot reads, it never owns a query (#296).

ElastiCache is a cache layer, not a system of record. Every query is owned by the
engine that stores its data (``assigned_engine``); a hot, bounded read can also
carry ``cache_engine`` so the cache serves it cache-aside in front of its owner.
The owner distribution never counts the cache; :func:`overlay_summary` is the
separate view (queries and share of calls).

A query qualifies (:func:`cache_eligibility`) when every rule holds:

- it is a ``SELECT`` (the cache never takes a write);
- it runs at least :data:`HOT_READ_MIN_CALLS_PER_SECOND` calls/s. A query below
  that is not cache material however its SQL looks: at 0.1 calls/s a cached entry
  is read a few times before it expires, and the miss path is the owner anyway;
- it returns at most :data:`CACHE_MAX_ROWS_AVG` rows on average (a bounded value);
- it has a cacheable shape (:func:`cache_pattern`): a keyed point lookup, a top-N
  (``ORDER BY ... LIMIT``), a session/token lookup, or a full read of a small
  reference table. Aggregations, text search and locking reads do not qualify;
- none of its tables is write-heavy: writes are less than
  :data:`WRITE_HEAVY_TABLE_MIN_WRITE_SHARE` of the table's calls, so entries are
  not invalidated faster than they are read.

The rules use only the collector's query metrics, so the overlay is re-evaluated
the same way whenever the owner changes (Reality Check consolidation).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

# Engines that can only be a cache layer: they never own a query.
CACHE_OVERLAY_ENGINES = frozenset({"elasticache"})

# Engines that are not a system of record for a table, so they never own a write.
# OpenSearch's write gate is tracked in #303.
NON_SYSTEM_OF_RECORD_ENGINES = frozenset({"elasticache"})

# Hot-read floor (calls/s). Starting value from #296; tune with data. On the two
# reference samples every overlay candidate is either >= 1.26 calls/s or <= 0.95.
HOT_READ_MIN_CALLS_PER_SECOND = 1.0

# Largest average result size (rows) a cached query may return.
CACHE_MAX_ROWS_AVG = 100.0

# A table whose writes are at least this share of its calls is write-heavy: cache
# entries on it would be invalidated about as often as they are read.
WRITE_HEAVY_TABLE_MIN_WRITE_SHARE = 0.5

# Triage signals that hint at a cacheable shape. Hints only: they never decide
# ownership, and they count only for a query that is already hot.
CACHE_HINT_SIGNALS: dict[str, str] = {
    "session_store": "session_lookup",
    "leaderboard_pattern": "top_n",
}

PATTERN_LABELS: dict[str, str] = {
    "point_lookup": "point lookup",
    "top_n": "top-N lookup",
    "session_lookup": "session/token lookup",
    "reference_read": "small reference read",
}

WRITE_QUERY_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT"})

_TOP_N_RE = re.compile(r"order\s+by\b.*\blimit\b", re.IGNORECASE | re.DOTALL)
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
_KEYED_RE = re.compile(r"(?<![<>!])=|\bin\s*\(", re.IGNORECASE)
_AGGREGATE_RE = re.compile(r"\bgroup\s+by\b|\b(sum|count|avg)\s*\(", re.IGNORECASE)
_TEXT_SEARCH_RE = re.compile(
    r"\b(i?like|regexp|rlike)\b|\bmatch\s*\(|to_tsvector|to_tsquery|@@", re.IGNORECASE
)
# Same keywords as triage's session_store signal
SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|csrf)\b", re.IGNORECASE)
_LOCKING_RE = re.compile(r"\bfor\s+(update|share)\b|\block\s+in\s+share\s+mode\b", re.IGNORECASE)


def is_write_query(query: Mapping) -> bool:
    """True for INSERT/UPDATE/DELETE (and other data-changing) statements."""
    return str(query.get("query_type") or "").upper() in WRITE_QUERY_TYPES


def can_own(engine: str, query: Mapping | None = None) -> bool:
    """Whether ``engine`` may own ``query`` (the write gate, #296).

    A cache-only engine owns nothing; an engine that is not a system of record
    owns no write. With no query, answers for reads.
    """
    if engine in CACHE_OVERLAY_ENGINES:
        return False
    return not (
        query is not None and is_write_query(query) and engine in NON_SYSTEM_OF_RECORD_ENGINES
    )


def owner_candidates(engines: Iterable[str], query: Mapping | None = None) -> list[str]:
    """``engines`` that may own ``query``, in their given order."""
    return [e for e in engines if can_own(e, query)]


def write_heavy_tables(queries: Iterable[Mapping]) -> set[str]:
    """Tables whose writes are at least WRITE_HEAVY_TABLE_MIN_WRITE_SHARE of their calls."""
    reads: dict[str, float] = defaultdict(float)
    writes: dict[str, float] = defaultdict(float)
    for q in queries:
        cps = float(q.get("calls_per_second") or 0)
        side = writes if is_write_query(q) else reads
        for t in q.get("tables_accessed") or []:
            side[t] += cps
    heavy = set()
    for t in set(reads) | set(writes):
        total = reads[t] + writes[t]
        if total > 0 and writes[t] / total >= WRITE_HEAVY_TABLE_MIN_WRITE_SHARE:
            heavy.add(t)
    return heavy


def cache_pattern(query: Mapping) -> str | None:
    """The cacheable shape of a read, or None when it is not a lookup.

    Detected from the SQL itself, with the same keywords and shape as triage's
    ``session_store`` and ``leaderboard_pattern`` signals (``CACHE_HINT_SIGNALS``),
    so the overlay is re-evaluated identically wherever only the collector is at hand.
    """
    text = str(query.get("query_text") or "")
    if _AGGREGATE_RE.search(text) or _TEXT_SEARCH_RE.search(text) or _LOCKING_RE.search(text):
        return None
    if query.get("has_text_search") or query.get("has_aggregations"):
        return None
    if SESSION_RE.search(text):
        return CACHE_HINT_SIGNALS["session_store"]
    if _TOP_N_RE.search(text):
        return "top_n" if float(query.get("rows_returned_avg") or 0) > 1 else "point_lookup"
    parts = _WHERE_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        # A keyed read: an equality or IN predicate (a range scan is not a lookup).
        return "point_lookup" if _KEYED_RE.search(parts[1]) else None
    # No WHERE: a full read, cacheable only as a small reference table.
    return "reference_read"


def cache_eligibility(query: Mapping, heavy_tables: set[str]) -> tuple[str, str] | None:
    """``(cache_pattern, cache_reason)`` when ``query`` qualifies for the overlay, else None."""
    if str(query.get("query_type") or "").upper() != "SELECT":
        return None
    cps = float(query.get("calls_per_second") or 0)
    if cps < HOT_READ_MIN_CALLS_PER_SECOND:
        return None
    rows = float(query.get("rows_returned_avg") or 0)
    if rows > CACHE_MAX_ROWS_AVG:
        return None
    if heavy_tables & set(query.get("tables_accessed") or []):
        return None
    pattern = cache_pattern(query)
    if pattern is None:
        return None
    reason = f"hot {PATTERN_LABELS[pattern]}: {cps:.1f} calls/s, {rows:.1f} rows avg"
    return pattern, reason


def available_cache_engine(engines: Iterable[str]) -> str | None:
    """The cache engine among ``engines`` (analyzed engines), if any."""
    return next((e for e in sorted(CACHE_OVERLAY_ENGINES) if e in set(engines)), None)


CUSTOMER_CACHE_REASON = "customer requested"
CUSTOMER_UNCACHE_REASON = "customer removed the cache"


def _warn(qa: dict, message: str) -> None:
    warnings = qa.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


def _clear(qa: dict) -> None:
    qa["cache_engine"] = None
    qa["cache_pattern"] = None
    qa["cache_reason"] = None


def pin_customer_cache(qa: dict, cache: str, query: Mapping | None, heavy: set[str]) -> None:
    """Mark ``qa`` cached by ``cache`` because the customer asked for it (#296).

    The owner is unchanged. A query that fails the hot-read rule keeps the cache
    and carries a warning saying why it would not have been chosen.
    """
    verdict = cache_eligibility(query, heavy) if query else None
    qa["cache_engine"] = cache
    qa["cache_pattern"] = verdict[0] if verdict else (cache_pattern(query) if query else None)
    qa["cache_reason"] = CUSTOMER_CACHE_REASON
    qa["cache_customer_override"] = True
    qa["cache_dropped"] = False
    if not verdict:
        _warn(
            qa,
            f"WARNING [LOW]: Query {qa.get('query_id')} is cached by {cache} at the "
            "customer's request, but it does not meet the hot-read rule (SELECT, "
            f">= {HOT_READ_MIN_CALLS_PER_SECOND:g} calls/s, <= {CACHE_MAX_ROWS_AVG:g} rows, "
            "lookup shape, table not write-heavy).",
        )


def apply_cache_overlay(
    query_assignments: list[dict],
    queries: Iterable[Mapping],
    engines: Iterable[str],
) -> list[dict]:
    """Set or clear the overlay fields on every assignment dict, in place.

    ``engines`` are the analyzed engines; with no cache engine among them no query
    is cached. Eligibility depends only on the query, so calling this again after
    owners change re-evaluates the overlay the same way, and clears an earlier
    safety-net drop (the next schema design gets its own chance). A cache the
    customer decided (``cache_customer_override``) is kept as decided: a pin is
    never cleared and a removal is never re-enabled. Returns the list.
    """
    query_list = list(queries)
    by_id = {q.get("query_id"): q for q in query_list}
    cache = available_cache_engine(engines)
    heavy = write_heavy_tables(query_list)
    for qa in query_assignments:
        q = by_id.get(qa.get("query_id"))
        if qa.get("cache_customer_override"):
            # The customer decided: cached (cache_engine set) stays cached, removed
            # (cache_engine empty) stays removed, whatever the rules say.
            if qa.get("cache_engine"):
                pin_customer_cache(qa, qa["cache_engine"], q, heavy)
            else:
                qa["cache_engine"] = None
                qa["cache_pattern"] = None
                qa["cache_reason"] = CUSTOMER_UNCACHE_REASON
                qa["cache_dropped"] = False
            continue
        qa["cache_dropped"] = False
        verdict = cache_eligibility(q, heavy) if cache and q else None
        if verdict and qa.get("assigned_engine") != cache:
            qa["cache_engine"] = cache
            qa["cache_pattern"], qa["cache_reason"] = verdict
        else:
            _clear(qa)
    return query_assignments


def fallback_owner(
    query: Mapping | None,
    engines: Iterable[str],
    source_engine: str = "",
    owner_counts: Mapping[str, int] | None = None,
) -> str:
    """The system-of-record engine for a query a cache owned (legacy) or was pinned to.

    The source-compatible Aurora engine when analyzed, else the Aurora engine
    ``pick_aurora_engine`` picks, else the owner candidate already serving the most
    queries, then by name. With no candidate at all, the source-compatible Aurora
    engine (the relational baseline).
    """
    from src.agents.referee.aurora_choice import pick_aurora_engine
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    counts = owner_counts or {}
    candidates = sorted(owner_candidates(set(engines), query))
    aurora = pick_aurora_engine(candidates, source_engine, counts)
    if aurora:
        return aurora
    if candidates:
        return min(candidates, key=lambda e: (-counts.get(e, 0), e))
    return SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower(), "aurora_postgresql")


def normalize_cache_owners(
    assignment: dict,
    queries: Iterable[Mapping],
    engines: Iterable[str],
    source_engine: str = "",
) -> list[str]:
    """Move queries a cache owns to a system-of-record engine, in place (#296).

    An assignment written before the cache overlay has ElastiCache owners; left
    as is, every customer edit fails validation and Reality Check re-runs keep
    them. Each such query is owned by :func:`fallback_owner` and stays cached
    when it is a hot read. Records one note in ``cache_notes``, refreshes the
    overlay summary and returns the new notes (empty when nothing was owned by a
    cache). Derived table views are the caller's to recompute.
    """
    qas = assignment.get("query_assignments") or []
    legacy = [qa for qa in qas if qa.get("assigned_engine") in CACHE_OVERLAY_ENGINES]
    if not legacy:
        return []
    query_list = list(queries)
    by_id = {q.get("query_id"): q for q in query_list}
    engine_set = set(engines)
    heavy = write_heavy_tables(query_list)
    counts = Counter(
        qa.get("assigned_engine")
        for qa in qas
        if qa.get("assigned_engine") and qa.get("assigned_engine") not in CACHE_OVERLAY_ENGINES
    )
    moved: Counter = Counter()
    kept = 0
    cache = legacy[0]["assigned_engine"]
    for qa in legacy:
        q = by_id.get(qa.get("query_id"))
        owner = fallback_owner(q, engine_set, source_engine, counts)
        qa["assignment_reason"] = (
            f"cache overlay migration: {qa['assigned_engine']} owned this query in an "
            f"assignment written before the cache overlay; owned by {owner} "
            f"(was: {qa.get('assignment_reason', '')})"
        )
        qa["assigned_engine"] = owner
        qa["signal_override"] = None
        moved[owner] += 1
        verdict = cache_eligibility(q, heavy) if q else None
        if verdict:
            qa["cache_engine"] = cache
            qa["cache_pattern"], qa["cache_reason"] = verdict
            kept += 1
        elif not qa.get("cache_customer_override"):
            _clear(qa)
    note = (
        f"{len(legacy)} {'query' if len(legacy) == 1 else 'queries'} owned by {cache} in an "
        "assignment written before the cache overlay moved to a system-of-record engine ("
        + ", ".join(f"{e} {n}" for e, n in sorted(moved.items()))
        + f"); {kept} stay cached as hot reads, the rest are no longer cached."
    )
    notes = assignment.setdefault("cache_notes", [])
    if note not in notes:
        notes.append(note)
    assignment["cache_overlay"] = overlay_summary(qas, query_list)
    return [note]


def overlay_summary(
    query_assignments: Iterable[Mapping], queries: Iterable[Mapping]
) -> dict | None:
    """The ``cache_overlay`` summary for in-scope queries, or None when nothing is cached."""
    cps = {q.get("query_id"): float(q.get("calls_per_second") or 0) for q in queries}
    in_scope = [qa for qa in query_assignments if qa.get("in_scope", True)]
    cached = [qa for qa in in_scope if qa.get("cache_engine")]
    if not cached:
        return None
    total = sum(cps.get(qa.get("query_id"), 0.0) for qa in in_scope)
    cached_cps = sum(cps.get(qa.get("query_id"), 0.0) for qa in cached)
    engine = Counter(qa["cache_engine"] for qa in cached).most_common(1)[0][0]
    return {
        "engine": engine,
        "query_count": len(cached),
        "calls_per_second": round(cached_cps, 3),
        "call_share_percent": round(min(100.0, cached_cps / total * 100), 1) if total else 0.0,
        "owners": dict(sorted(Counter(qa.get("assigned_engine", "") for qa in cached).items())),
        "patterns": dict(sorted(Counter(qa.get("cache_pattern") or "" for qa in cached).items())),
        "min_calls_per_second": HOT_READ_MIN_CALLS_PER_SECOND,
        "max_rows_avg": CACHE_MAX_ROWS_AVG,
    }


def refresh_cache_overlay(
    assignment: dict,
    queries: Iterable[Mapping],
    engines: Iterable[str],
) -> dict:
    """Re-evaluate every query's overlay and the summary on an assignment dict, in place."""
    query_list = list(queries)
    apply_cache_overlay(assignment.get("query_assignments") or [], query_list, engines)
    assignment["cache_overlay"] = overlay_summary(
        assignment.get("query_assignments") or [], query_list
    )
    return assignment


def dropped_reason(engine: str) -> str:
    """``cache_reason`` of a query the safety net dropped."""
    return (
        f"cache overlay dropped after schema design: no in-scope {engine} access pattern "
        "serves it (owner unchanged)"
    )


def apply_schema_safety_net(
    assignment: dict, cache_schema: Mapping | None, queries: Iterable[Mapping]
) -> list[str]:
    """Drop the overlay of cached queries the cache's schema design does not serve (#296).

    Runs after schema design. A cached query with no in-scope access pattern in the
    cache engine's design loses ``cache_engine`` (``cache_dropped`` set, the reason
    in ``cache_reason``); its owner is unchanged. A cache the customer pinned is
    kept, with a warning. With no design (schema design did not run) nothing is
    dropped. Mutates ``assignment`` and returns the dropped query ids.
    """
    if not cache_schema:
        return []
    covered: set[str] = set()
    for ap in cache_schema.get("access_patterns") or []:
        if isinstance(ap, Mapping) and ap.get("in_scope", True):
            covered.update(
                str(q) for q in (ap.get("source_query_ids") or ap.get("query_ids") or [])
            )
    dropped: list[str] = []
    for qa in assignment.get("query_assignments") or []:
        engine = qa.get("cache_engine")
        if not engine or not qa.get("in_scope", True) or qa.get("query_id") in covered:
            continue
        if qa.get("cache_customer_override"):
            _warn(
                qa,
                f"WARNING [MEDIUM]: Query {qa.get('query_id')} is cached by {engine} at the "
                f"customer's request, but the {engine} schema design has no in-scope access "
                "pattern for it.",
            )
            continue
        dropped.append(qa["query_id"])
        _clear(qa)
        qa["cache_reason"] = dropped_reason(engine)
        qa["cache_dropped"] = True
    if dropped:
        assignment["cache_overlay"] = overlay_summary(
            assignment.get("query_assignments") or [], queries
        )
    return dropped


def safety_net_note(engine: str, dropped: list[str]) -> str:
    """The note recorded when the safety net drops cached queries."""
    n = len(dropped)
    noun = "query" if n == 1 else "queries"
    return (
        f"{n} cached {noun} had no in-scope access pattern in the {engine} schema design, "
        f"so {'it is' if n == 1 else 'they are'} served by {'its' if n == 1 else 'their'} "
        "owner engine only (cache overlay dropped, owner unchanged)."
    )
