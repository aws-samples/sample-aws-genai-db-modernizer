"""Cache overlay: ElastiCache fronts hot reads, it never owns a query (#296).

ElastiCache is a cache layer, not a system of record. Every query is owned by the
engine that stores its data (``assigned_engine``); a hot, bounded read can also
carry ``cache_engine`` so the cache serves it cache-aside in front of its owner.
The owner distribution never counts the cache; :func:`overlay_summary` is the
separate view (queries and share of calls).

This module also hosts the write gate every non-system-of-record engine shares
(:func:`can_own`, :data:`NON_SYSTEM_OF_RECORD_ENGINES`): ElastiCache owns nothing
at all, and OpenSearch -- a read model that indexes a durable owner's data, never
the only copy (#303, epic #340) -- owns reads but never a write. Both rules are
enforced wherever :func:`can_own` gates an assignment: the resolver's v1 scoring,
signal overrides and co-dependency groups, the reality check's consolidation
(``reality_check_rules.may_absorb``), and :class:`AssignmentValidator`'s hard-error
check on the final assignment, customer edits included.

A query qualifies (:func:`cache_eligibility`) when every rule holds:

- it is a ``SELECT`` (the cache never takes a write);
- it runs at least :data:`HOT_READ_MIN_CALLS_PER_SECOND` calls/s. A query below
  that is not cache material however its SQL looks: at 0.1 calls/s a cached entry
  is read a few times before it expires, and the miss path is the owner anyway;
- it returns at most :data:`CACHE_MAX_ROWS_AVG` rows on average (a bounded value);
- it has a cacheable shape (:func:`cache_pattern`): a keyed point lookup, a top-N
  (``ORDER BY ... LIMIT``, ``TOP (n) ... ORDER BY`` or ``FETCH FIRST/NEXT``), a
  session/token lookup, or a full read of a small reference table. Aggregations,
  text search, locking reads and sequence fetches (``NEXT VALUE FOR``,
  ``seq.NEXTVAL``, ``nextval(...)``) do not qualify;
- none of its tables is write-heavy: writes are less than
  :data:`WRITE_HEAVY_TABLE_MIN_WRITE_SHARE` of the table's calls, so entries are
  not invalidated faster than they are read.

The rules use only the collector's query metrics, so the overlay is re-evaluated
the same way whenever the owner changes (Reality Check consolidation).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.agents.referee.query_shape import locking_clause
from src.agents.referee.tie_break import break_tie, settled_counts
from src.agents.referee.utility_statements import is_sequence_fetch
from src.contracts.move_ledger import MoveEvidence, MovePass, ReasonCode
from src.shared.cache_engines import CACHE_ENGINES, HOT_READ_MIN_CALLS_PER_SECOND, cache_threshold
from src.shared.cache_pattern_labels import CACHE_PATTERN_LABELS
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import SEARCH_ENGINES
from src.tools.sql.query_facts import query_facts

if TYPE_CHECKING:
    from src.agents.referee.move_ledger import MoveLedger
    from src.agents.referee.query_scoring import ScoringContext

# Engines that can only be a cache layer: they never own a query. Declared once
# in ``src.shared.cache_engines`` (#398) and re-exported here for routing.
CACHE_OVERLAY_ENGINES = CACHE_ENGINES

# Engines that are not a system of record for a table, so they never own a write
# (or a locking read, see ``is_locking_read``). Built from the shared cache and
# search buckets (``CACHE_OVERLAY_ENGINES``, ``migration_wave_engines.
# SEARCH_ENGINES``) rather than its own copy, so a change to either bucket
# composes here instead of silently dropping OpenSearch's write gate again.
# OpenSearch is a read model (#303, epic #340 decision 2026-10-04): it indexes a
# durable owner's data and may own reads, but it is never the system of record
# for a write, the same write gate ElastiCache already has (#296).
NON_SYSTEM_OF_RECORD_ENGINES = CACHE_OVERLAY_ENGINES | SEARCH_ENGINES

# Largest average result size (rows) a cached query may return.
CACHE_MAX_ROWS_AVG = cache_threshold("max_rows_avg")

# A table whose writes are at least this share of its calls is write-heavy: cache
# entries on it would be invalidated about as often as they are read.
WRITE_HEAVY_TABLE_MIN_WRITE_SHARE = cache_threshold("write_heavy_table_min_write_share")

# Triage signals that hint at a cacheable shape. Hints only: they never decide
# ownership, and they count only for a query that is already hot.
CACHE_HINT_SIGNALS: dict[str, str] = {
    "session_store": "session_lookup",
    "leaderboard_pattern": "top_n",
}

# Shared with the deliverables (src/shared/cache_pattern_labels.py).
PATTERN_LABELS: dict[str, str] = CACHE_PATTERN_LABELS

WRITE_QUERY_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT"})


def has_session_keyword(text: str) -> bool:
    """Whether ``text`` is a session/token statement (``session_store``, #663):
    the engine-neutral ``session_key`` query fact (``src.tools.sql.query_facts``,
    #491). Needs the SQL in its original case."""
    return query_facts(text or "").session_key


def is_write_query(query: Mapping, source_engine: str | None = None) -> bool:
    """True for INSERT/UPDATE/DELETE (and other data-changing) statements.

    The collector's own ``query_type`` catches the common case, a statement that
    opens with a comment included (#1178). A data-modifying CTE, ``CALL``,
    ``TRUNCATE`` or ``COPY ... TO`` comes through as ``OTHER`` instead (review of
    #303), so for that type a DML keyword or a leading ``CALL``/``TRUNCATE``/
    ``COPY`` in the SQL itself -- after stripping any leading comment --
    counts as a write too. ``source_engine`` is the dialect the text is read in: a
    keyword inside a MySQL ``#`` comment or a PostgreSQL ``$$`` string is no write
    (#1144), but a ``DO $$ ... $$`` block or a function body is code, and so is any
    dynamic SQL string inside one, and counts. With no engine, the text is read in
    the common grammar only.
    """
    qtype = str(query.get("query_type") or "").upper()
    if qtype in WRITE_QUERY_TYPES:
        return True
    if qtype != "OTHER":
        return False
    return query_facts(str(query.get("query_text") or ""), source_engine).write_statement_text


def is_locking_read(query: Mapping, source_engine: str | None = None) -> bool:
    """True for a read that takes a row lock (``query_shape.locking_clause``).

    A locking read takes a row lock as part of a transaction -- the same
    consistency guarantee a write needs -- so a non-system-of-record engine
    cannot serve it either (review of #303), even though it is not itself a
    write and :func:`is_write_query` says False. The lock is read by the one
    definition the shape check records (#799, #838): ``FOR UPDATE``, ``FOR NO
    KEY UPDATE``, ``FOR SHARE``, ``FOR KEY SHARE``, MySQL ``LOCK IN SHARE
    MODE`` and a SQL Server ``WITH (UPDLOCK | HOLDLOCK | XLOCK |
    SERIALIZABLE)`` hint, matched after comments, literals and quoted
    identifiers are removed, in ``source_engine``'s dialect (MySQL ``#`` comments,
    PostgreSQL ``$$`` strings). The UI engine picker runs the same rule
    (``src/ui/src/utils/sqlLocking.js``) in the same dialect, which it reads from
    the collector payload's ``metadata.source_database.engine``; both are held to
    the same cases (``locking_clause_cases.json``, whose ``engine`` field names
    the dialect).
    """
    return locking_clause(str(query.get("query_text") or ""), source_engine) is not None


def can_own(engine: str, query: Mapping | None = None, source_engine: str | None = None) -> bool:
    """Whether ``engine`` may own ``query`` (the write gate, #296, #303).

    A cache-only engine owns nothing; an engine that is not a system of record
    owns no write and no locking read. With no query, answers for reads.
    """
    if engine in CACHE_OVERLAY_ENGINES:
        return False
    if query is None or engine not in NON_SYSTEM_OF_RECORD_ENGINES:
        return True
    return not (is_write_query(query, source_engine) or is_locking_read(query, source_engine))


def owner_candidates(
    engines: Iterable[str], query: Mapping | None = None, source_engine: str | None = None
) -> list[str]:
    """``engines`` that may own ``query``, in their given order."""
    return [e for e in engines if can_own(e, query, source_engine)]


def write_heavy_tables(queries: Iterable[Mapping], source_engine: str | None = None) -> set[str]:
    """Tables whose writes are at least WRITE_HEAVY_TABLE_MIN_WRITE_SHARE of their calls."""
    reads: dict[str, float] = defaultdict(float)
    writes: dict[str, float] = defaultdict(float)
    for q in queries:
        cps = float(q.get("calls_per_second") or 0)
        side = writes if is_write_query(q, source_engine) else reads
        for t in q.get("tables_accessed") or []:
            side[t] += cps
    heavy = set()
    for t in set(reads) | set(writes):
        total = reads[t] + writes[t]
        if total > 0 and writes[t] / total >= WRITE_HEAVY_TABLE_MIN_WRITE_SHARE:
            heavy.add(t)
    return heavy


def cache_pattern(query: Mapping, source_engine: str = "") -> str | None:
    """The cacheable shape of a read, or None when it is not a lookup.

    Detected from the SQL itself, with the same facts and shape as triage's
    ``session_store``, ``leaderboard_pattern`` and ``text_search`` signals
    (``CACHE_HINT_SIGNALS``), read in ``source_engine``'s dialect, so the overlay
    is re-evaluated identically wherever only the collector is at hand. An
    aggregation or a text search (the ``aggregation`` and ``text_search`` facts,
    #893) is not a lookup; a prefix ``LIKE 'abc%'`` is a range, which the keyed
    test below decides.
    """
    text = str(query.get("query_text") or "")
    facts = query_facts(text, source_engine)
    if facts.aggregation or facts.text_search or locking_clause(text, source_engine):
        return None
    # A sequence fetch returns a new value on every call: a cached answer is wrong (#654).
    if is_sequence_fetch(text):
        return None
    if query.get("has_text_search") or query.get("has_aggregations"):
        return None
    if facts.session_key:
        return CACHE_HINT_SIGNALS["session_store"]
    if facts.top_n:
        return "top_n" if float(query.get("rows_returned_avg") or 0) > 1 else "point_lookup"
    if facts.where_word:
        # A keyed read: an equality or IN predicate (a range scan is not a lookup).
        return "point_lookup" if facts.keyed_after_where else None
    # No WHERE: a full read, cacheable only as a small reference table.
    return "reference_read"


def cache_eligibility(
    query: Mapping, heavy_tables: set[str], source_engine: str = ""
) -> tuple[str, str] | None:
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
    pattern = cache_pattern(query, source_engine)
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


def pin_customer_cache(
    qa: dict, cache: str, query: Mapping | None, heavy: set[str], source_engine: str = ""
) -> None:
    """Mark ``qa`` cached by ``cache`` because the customer asked for it (#296).

    The owner is unchanged. A query that fails the hot-read rule keeps the cache
    and carries a warning saying why it would not have been chosen.
    """
    verdict = cache_eligibility(query, heavy, source_engine) if query else None
    qa["cache_engine"] = cache
    qa["cache_pattern"] = (
        verdict[0] if verdict else (cache_pattern(query, source_engine) if query else None)
    )
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
    source_engine: str = "",
) -> list[dict]:
    """Set or clear the overlay fields on every assignment dict, in place.

    ``engines`` are the analyzed engines; with no cache engine among them no query
    is cached. Eligibility depends only on the query, so calling this again after
    owners change re-evaluates the overlay the same way, and clears an earlier
    safety-net drop (the next schema design gets its own chance). The post-schema
    reroute never calls this (#1379): it keeps the approved version's cache fields,
    a safety-net drop included, and recomputes only the summary
    (``refresh_consolidated_assignment(..., keep_cache_overlay=True)``); the next
    synthesis safety net re-judges the drop. A cache the
    customer decided (``cache_customer_override``) is kept as decided: a pin is
    never cleared and a removal is never re-enabled. ``source_engine`` (the
    collector's) is the dialect the query facts are read in. Returns the list.
    """
    query_list = list(queries)
    by_id = {q.get("query_id"): q for q in query_list}
    cache = available_cache_engine(engines)
    heavy = write_heavy_tables(query_list, source_engine)
    for qa in query_assignments:
        q = by_id.get(qa.get("query_id"))
        if qa.get("cache_customer_override"):
            # The customer decided: cached (cache_engine set) stays cached, removed
            # (cache_engine empty) stays removed, whatever the rules say.
            if qa.get("cache_engine"):
                pin_customer_cache(qa, qa["cache_engine"], q, heavy, source_engine)
            else:
                qa["cache_engine"] = None
                qa["cache_pattern"] = None
                qa["cache_reason"] = CUSTOMER_UNCACHE_REASON
                qa["cache_dropped"] = False
            continue
        qa["cache_dropped"] = False
        verdict = cache_eligibility(q, heavy, source_engine) if cache and q else None
        if verdict and qa.get("assigned_engine") != cache:
            qa["cache_engine"] = cache
            qa["cache_pattern"], qa["cache_reason"] = verdict
        else:
            _clear(qa)
    return query_assignments


def _candidate_scores(
    query: Mapping,
    candidates: list[str],
    engines: Iterable[str] | Mapping[str, Mapping],
    triage: Mapping | None,
    collector: Mapping | None,
    ctx: ScoringContext | None = None,
) -> dict[str, float] | None:
    """Each serviceable ``candidate``'s own adjusted score, or ``None`` when
    ``engines`` carries no real per-engine analysis to score with at all.

    The score is :func:`src.agents.referee.query_scoring.score_query_for_engine`,
    scored against ``ctx`` when the caller already built one (:func:`fallback_owner`'s
    batch callers do this once, #549 round-3 review, Suggestion 3), else a
    fresh :meth:`src.agents.referee.query_scoring.ScoringContext.from_pipeline`
    -- the exclusion gate, the capability gate (``triage``'s own
    ``query_capabilities`` when it has one, else detected from ``collector``,
    #549 round-2 review, Critical 1), the base confidence and the
    anti-pattern penalty, clamped at 0 (ADR-031 Decision 2 steps 2-5) -- the
    exact pipeline ``AssignmentResolver.resolve()`` uses, so a candidate that
    cannot actually serve the query at all is never compared against one
    that can, and never scored on its raw base alone with the penalty
    silently skipped (#549 round-2 review, Important 1). A candidate
    ``score_query_for_engine`` gates to ``None`` is left out of the returned
    mapping entirely, never recorded as a 0 a real, lower-but-serviceable
    score could lose to by accident.

    ``engines`` is normally the real ``{engine: analysis}`` mapping (every
    production caller passes it since #786: the reality check, the customer
    gate and synthesis); a plain engine-name iterable, or a candidate with no
    analysis, still works. ``None`` here -- rather than a
    partial score for whichever candidates happen to have analysis and a
    silent exclusion for the rest -- means every remaining candidate that
    passes the scorer's gates is treated as tied (:func:`fallback_owner`;
    the gates need no analysis, :func:`_gate_kinds`, #812), which is the
    honest answer when there is no fitness signal to compare them with,
    never a stand-in for "the engine with no analysis loses."
    """
    scored = _score_candidates(query, candidates, engines, triage, collector, ctx)
    return None if scored is None else scored[0]


def _score_candidates(
    query: Mapping,
    candidates: list[str],
    engines: Iterable[str] | Mapping[str, Mapping],
    triage: Mapping | None,
    collector: Mapping | None,
    ctx: ScoringContext | None = None,
) -> tuple[dict[str, float], dict[str, str]] | None:
    """:func:`_candidate_scores` plus each gated candidate's gate kind.

    ``(scores, gates)``: ``scores`` as :func:`_candidate_scores` returns it, and
    ``gates`` the scorer's gate kind for every candidate it gated (the move
    ledger's candidate ladder, #616). ``None`` when there is no analysis.
    """
    if not isinstance(engines, Mapping):
        return None
    from src.agents.referee.query_scoring import ScoringContext

    if any(not isinstance(engines.get(e), Mapping) or not engines.get(e) for e in candidates):
        return None
    if ctx is None:
        ctx = ScoringContext.from_pipeline(engines, triage, collector)
    return _scored_and_gated(query, candidates, ctx)


def _scored_and_gated(
    query: Mapping, candidates: list[str], ctx: ScoringContext
) -> tuple[dict[str, float], dict[str, str]]:
    """Each candidate's shared-scorer score, or its gate kind when the scorer gates it.

    The one loop :func:`_score_candidates` and :func:`_gate_kinds` share, so
    the scored and the no-analysis branches gate the same way (#812).
    """
    from src.agents.referee.query_scoring import score_query_for_engine

    scores: dict[str, float] = {}
    gates: dict[str, str] = {}
    for engine in candidates:
        result = score_query_for_engine(query, engine, ctx)
        if result.score is not None:
            scores[engine] = result.score
        else:
            gates[engine] = result.gate_kind or "gated"
    return scores, gates


def _gate_kinds(
    query: Mapping,
    candidates: list[str],
    engines: Iterable[str] | Mapping[str, Mapping],
    triage: Mapping | None,
    collector: Mapping | None,
    ctx: ScoringContext | None = None,
) -> dict[str, str]:
    """The shared scorer's gate kind for every ``candidate`` it gates, with no analysis needed.

    The hard exclusion and capability gates read only the query and its
    required capabilities (``triage``'s, else detected from ``collector``),
    never an engine's analysis, so :func:`owner_decision` can apply them when
    there is nothing to score with (#812).
    """
    from src.agents.referee.query_scoring import ScoringContext

    if ctx is None:
        analysis = engines if isinstance(engines, Mapping) else {}
        ctx = ScoringContext.from_pipeline(analysis, triage, collector)
    return _scored_and_gated(query, candidates, ctx)[1]


# Customer-facing reason for fallback_owner's "no serviceable candidate"
# fallback (#549 round-3 review, Important 1): the relational engine is
# always picked here (never one of the just-excluded candidates), because
# every registered capability includes both Aurora engines
# (``capability_registry.ENGINE_CAPABILITIES``) and ``engine_exclusions``
# has no rule that targets either one -- the capability gate can never rule
# out the relational engine. The query moves there (it is leaving the
# cache). Only the source-compatible engine of a homogeneous source runs the
# source SQL unchanged; the generic placeholder (a heterogeneous or unknown
# source, #786) makes no such claim (ADR-031 Decision 6, Amendment 1).
_NO_SERVICEABLE_SOURCE_COMPATIBLE = (
    "No analyzed engine can serve this query; it moves to {engine}, which runs the "
    "source SQL as is."
)
_NO_SERVICEABLE_FALLBACK = (
    "No analyzed engine can serve this query; it moves to {engine}, the relational fallback."
)


def no_serviceable_candidate_reason(engine: str, source_engine: str | None) -> str:
    """The customer-facing sentence for a "no serviceable candidate" fallback onto ``engine``.

    It says ``engine`` runs the source SQL as is only when ``engine`` is the
    source's own Aurora engine (#816 review).
    """
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    compatible = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower()) == engine
    template = _NO_SERVICEABLE_SOURCE_COMPATIBLE if compatible else _NO_SERVICEABLE_FALLBACK
    return template.format(engine=display_engine(engine))


def fallback_owner(
    query: Mapping | None,
    engines: Iterable[str] | Mapping[str, Mapping],
    source_engine: str = "",
    owner_counts: Mapping[str, int] | None = None,
    *,
    triage: Mapping | None,
    collector: Mapping | None,
    ctx: ScoringContext | None = None,
) -> tuple[str, list[str], str | None]:
    """The system-of-record engine for a query a cache owned (legacy) or was pinned to.

    Every owner candidate -- Aurora included -- is scored the same way
    (:func:`_candidate_scores`, the resolver's own
    ``query_scoring.score_query_for_engine``, #549, scored against ``triage``
    and ``collector`` the same way ``AssignmentResolver.resolve()`` would, or
    against ``ctx`` directly when the caller already built one): the highest
    wins outright. An exact tie, or no score at all (``engines`` has no real
    per-engine analysis to score with: then every candidate that passes the
    hard exclusion and capability gates, which need no analysis, is tied;
    a gated one never is, #812), goes through
    :func:`src.agents.referee.tie_break.break_tie`, the one shared ADR-031
    tie-break every other routing call site uses: the source-compatible
    engine wins ONLY an exact tie that includes it (ADR-031 Decision 2 says
    nothing about Aurora winning outright before any score is compared --
    #549 review, Important 2), else the maintainer's placeholder for a tie
    ADR-031 does not itself resolve (ADR-031 Decision 12). ``owner_counts``
    is never a direct bias here (#549, before this fix it was the FIRST
    thing compared, with name as the second): it only ever reaches the
    placeholder as the settled-workload-share signal that rule is allowed
    to use. ``triage``/``collector`` are required keyword-only arguments
    (#549 round-3 review, Suggestion 4): there is no default that would let
    a new call site quietly skip the capability gate's real data.

    When there is no candidate at all, or every candidate fails the
    exclusion/capability gate (``_candidate_scores`` returns ``{}``, not
    ``None``; or, with no analysis, :func:`_gate_kinds` gates every one),
    there is no serviceable owner -- this is never a tie to
    resolve among engines already known unable to serve the query. Both
    cases fall back through the one shared rule,
    :func:`src.agents.referee.aurora_choice.fallback_aurora_engine` (#786):
    the source-compatible Aurora engine when the source has one, else the
    generic ``aurora`` placeholder. ``AssignmentResolver.resolve()``'s Step 7
    Aurora fallback uses the same rule once no Aurora engine is in its pool;
    when one is (triage always selects the source's Aurora engine, and both
    for a heterogeneous source), it follows that engine or the #381
    ``aurora_engine_choice`` instead, which this function does not have
    access to (#549 round-3 review, Suggestion 1).

    Returns ``(owner, tied_engines, fallback_reason)``. ``tied_engines`` is
    the sorted tied candidate set when ``break_tie`` reports the pick as an
    unresolved tie (for the caller's own ``QueryAssignment.tied_engines``,
    recorded the same way every other tie-break call site in this codebase
    records it), else empty. ``fallback_reason`` is ``None`` for every real,
    scored (or tied-and-resolved) pick; it is
    :func:`no_serviceable_candidate_reason` -- a complete, customer-facing
    sentence naming the owner with :func:`src.shared.engine_names.
    display_engine` -- for the "no candidate"/"no serviceable candidate"
    fallback above, so a caller never has to say "owned by {engine}" as if
    that engine had actually been compared and won (#549 round-3 review,
    Important 1: before this fix, both ``normalize_cache_owners`` and
    ``assignment_overrides._apply_cache_override`` wrote exactly that).
    """
    decision = owner_decision(
        query,
        engines,
        source_engine,
        owner_counts,
        triage=triage,
        collector=collector,
        ctx=ctx,
    )
    return decision.owner, decision.tied_engines, decision.fallback_reason


@dataclass(frozen=True)
class OwnerDecision:
    """:func:`fallback_owner`'s pick, with what it compared (for the move ledger, #616).

    ``owner``, ``tied_engines`` and ``fallback_reason`` are
    :func:`fallback_owner`'s return values. ``candidates`` are the owner
    candidates it considered, ``scores`` the shared scorer's value for each
    serviceable one (``None`` when there was no analysis to score with),
    ``gates`` the gate kind of each gated one, and ``tie_rule`` the tie rule
    that picked ``owner`` when an exact tie did (``None`` otherwise).
    ``fallback_rule`` is the fallback rule when no candidate could serve the
    query (#786: ``fallback.no_owner`` with no owner candidate at all,
    ``fallback.no_score`` when every candidate is gated), the same reason
    codes the first assignment's Aurora fallback records; ``None`` for a
    scored pick.
    """

    owner: str
    tied_engines: list[str]
    fallback_reason: str | None
    candidates: list[str] = field(default_factory=list)
    scores: dict[str, float] | None = None
    gates: dict[str, str] = field(default_factory=dict)
    tie_rule: ReasonCode | None = None
    fallback_rule: ReasonCode | None = None


def owner_decision(
    query: Mapping | None,
    engines: Iterable[str] | Mapping[str, Mapping],
    source_engine: str = "",
    owner_counts: Mapping[str, int] | None = None,
    *,
    triage: Mapping | None,
    collector: Mapping | None,
    ctx: ScoringContext | None = None,
) -> OwnerDecision:
    """:func:`fallback_owner`, returning the whole :class:`OwnerDecision`."""
    from src.agents.referee.aurora_choice import fallback_aurora_engine
    from src.agents.referee.reality_check_rules import tie_reason_code
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    counts = owner_counts or {}
    default = fallback_aurora_engine(source_engine)
    fallback_reason = no_serviceable_candidate_reason(default, source_engine)
    candidates = sorted(owner_candidates(set(engines), query, source_engine))
    if not candidates:
        return OwnerDecision(
            default, [], fallback_reason, fallback_rule=ReasonCode.FALLBACK_NO_OWNER
        )
    scored = (
        _score_candidates(query, candidates, engines, triage, collector, ctx) if query else None
    )
    scores, gates = scored if scored is not None else (None, {})
    if scores is None and query:
        # No analysis to score with, but the scorer's gates need none: a
        # candidate the hard exclusion or capability gate rules out is never
        # tied (ADR-031 Decision 2 steps 2-3, Decision 12; #812).
        gates = _gate_kinds(query, candidates, engines, triage, collector, ctx)
        if len(gates) == len(candidates):
            return OwnerDecision(
                default,
                [],
                fallback_reason,
                candidates,
                None,
                gates,
                fallback_rule=ReasonCode.FALLBACK_NO_SCORE,
            )
    if scores is None:
        # No fitness signal available at all (no real analysis to score
        # with): every serviceable candidate is tied -- break_tie still
        # resolves it deterministically, rather than this function picking
        # one by name.
        tied = [e for e in candidates if e not in gates]
    elif not scores:
        # Every candidate failed the exclusion/capability gate: there is no
        # serviceable owner among them, so this is not a tie either.
        return OwnerDecision(
            default,
            [],
            fallback_reason,
            candidates,
            scores,
            gates,
            fallback_rule=ReasonCode.FALLBACK_NO_SCORE,
        )
    else:
        top = max(scores.values())
        tied = sorted(e for e in scores if scores[e] == top)
    if len(tied) == 1:
        return OwnerDecision(tied[0], [], None, candidates, scores, gates)
    query_id = str((query or {}).get("query_id") or "")
    source_aurora = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    winner, tie_kind = break_tie(tied, set(), source_aurora, counts, query_id)
    return OwnerDecision(
        winner,
        sorted(tied) if tie_kind == "tie_unresolved" else [],
        None,
        candidates,
        scores,
        gates,
        tie_reason_code(tie_kind, tied, counts, query_id),
    )


def owner_move_fields(decision: OwnerDecision) -> dict:
    """The Move fields of a cache owner pick: its rule, scores, runner-up and ladder (#616).

    The rule is ``cache.legacy_owner`` for a scored pick and the fallback rule
    (``fallback.*``, #786) when no candidate could serve the query.
    """
    from src.agents.referee.move_ledger import best_other, candidate_ladder

    scores = decision.scores or {}
    return {
        "reason_code": decision.fallback_rule or ReasonCode.CACHE_LEGACY_OWNER,
        "score_after": scores.get(decision.owner),
        "runner_up": best_other(scores, decision.owner),
        "evidence": MoveEvidence(
            candidates=candidate_ladder(decision.candidates, scores, decision.gates),
            tie_rule=decision.tie_rule,
        ),
        "tied_engines": decision.tied_engines,
    }


def normalize_cache_owners(
    assignment: dict,
    queries: Iterable[Mapping],
    engines: Iterable[str] | Mapping[str, Mapping],
    source_engine: str = "",
    *,
    triage: Mapping | None,
    collector: Mapping | None,
    ledger: MoveLedger | None = None,
) -> list[str]:
    """Move queries a cache owns to a system-of-record engine, in place (#296).

    An assignment written before the cache overlay has ElastiCache owners; left
    as is, every customer edit fails validation and Reality Check re-runs keep
    them. Each such query is owned by :func:`fallback_owner` and stays cached
    when it is a hot read. ``engines`` is passed through to ``fallback_owner``
    as given (a plain engine-name iterable at some call sites, the real
    ``{engine: analysis}`` mapping at others, #549) rather than collapsed to a
    name set here, so a caller that has real analysis lets ``fallback_owner``
    score with it. ``triage``/``collector`` are required keyword-only
    arguments (#549 round-3 review, Suggestion 4: no default that would let a
    new call site quietly skip the capability gate's real data) and, when
    ``engines`` is a real ``{engine: analysis}`` mapping, feed ONE
    :meth:`src.agents.referee.query_scoring.ScoringContext.from_pipeline`
    built here, before the loop below, and handed to every ``fallback_owner``
    call as ``ctx=`` -- #549 round-3 review, Suggestion 3: this assignment
    can carry many legacy-owned rows, and building a fresh context (the
    anti-pattern map, and the capability map when triage needs re-detecting
    it) per row rather than once was measured to cost real time on a large
    sample's worth of them. The settled-workload counts ``fallback_owner``
    passes on to an unresolved tie's placeholder come from
    :func:`tie_break.settled_counts` (#549 review, Important 3), never a raw
    count of every ``assigned_engine`` -- a query already carrying its own
    ``tied_engines`` must not feed another tie's count. Records one note in
    ``cache_notes``, refreshes the overlay summary and returns the new notes
    (empty when nothing was owned by a cache). Derived table views are the
    caller's to recompute.

    Each move is a Move on ``ledger`` (#616: pass ``cache_owner_normalize``,
    rule ``cache.legacy_owner``, or the ``fallback.*`` rule when no candidate
    could serve the query (#786), with the shared scorer's scores, the runner-up,
    the candidate ladder and, when an exact tie picked the owner, the tie rule).
    A caller writing a new version passes the ledger that version's Moves come
    from. Without one, the change is to this version in memory: the Moves are
    appended to ``assignment["moves"]``, after the version's own, so it still
    replays from its input.
    """
    from src.agents.referee.move_ledger import apply_to_assignment, ledger_continuing

    qas = assignment.get("query_assignments") or []
    legacy = [qa for qa in qas if qa.get("assigned_engine") in CACHE_OVERLAY_ENGINES]
    if not legacy:
        return []
    query_list = list(queries)
    by_id = {q.get("query_id"): q for q in query_list}
    heavy = write_heavy_tables(query_list, source_engine)
    counts = settled_counts(qas)
    ctx = None
    if isinstance(engines, Mapping):
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.write_groups import assignment_sources_of_truth

        # The capability waivers (#377) read this version's sources of truth.
        ctx = ScoringContext.from_pipeline(engines, triage, collector).with_sources_of_truth(
            assignment_sources_of_truth(assignment)
        )
    in_memory = ledger is None
    if ledger is None:
        ledger = ledger_continuing(assignment)
    recorded = len(ledger.moves)
    moved: Counter = Counter()
    kept = 0
    cache = legacy[0]["assigned_engine"]
    for qa in legacy:
        q = by_id.get(qa.get("query_id"))
        decision = owner_decision(
            q, engines, source_engine, counts, triage=triage, collector=collector, ctx=ctx
        )
        owner, fallback_reason = decision.owner, decision.fallback_reason
        # #572: owned by {display_engine(owner)}, never the raw id -- fallback_reason
        # (the no-serviceable-candidate sentence) already names its engine this way.
        owner_clause = fallback_reason or f"owned by {display_engine(owner)}"
        qa["assignment_reason"] = (
            f"cache overlay migration: {display_engine(qa['assigned_engine'])} owned this "
            f"query in an assignment written before the cache overlay; {owner_clause} "
            f"(was: {qa.get('assignment_reason', '')})"
        )
        apply_to_assignment(
            ledger,
            qa,
            owner,
            pass_=MovePass.CACHE_OWNER_NORMALIZE,
            **owner_move_fields(decision),
        )
        qa["signal_override"] = None
        moved[owner] += 1
        verdict = cache_eligibility(q, heavy, source_engine) if q else None
        if verdict:
            qa["cache_engine"] = cache
            qa["cache_pattern"], qa["cache_reason"] = verdict
            kept += 1
        elif not qa.get("cache_customer_override"):
            _clear(qa)
    note = (
        f"{len(legacy)} {'query' if len(legacy) == 1 else 'queries'} owned by "
        f"{display_engine(cache)} in an assignment written before the cache overlay moved to "
        "a system-of-record engine ("
        + ", ".join(f"{display_engine(e)} {n}" for e, n in sorted(moved.items()))
        + f"); {kept} stay cached as hot reads, the rest are no longer cached."
    )
    notes = assignment.setdefault("cache_notes", [])
    if note not in notes:
        notes.append(note)
    assignment["cache_overlay"] = overlay_summary(qas, query_list)
    if in_memory:
        assignment["moves"] = [
            *(assignment.get("moves") or []),
            *(m.model_dump(mode="json") for m in ledger.moves[recorded:]),
        ]
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
    source_engine: str = "",
) -> dict:
    """Re-evaluate every query's overlay and the summary on an assignment dict, in place."""
    query_list = list(queries)
    apply_cache_overlay(
        assignment.get("query_assignments") or [], query_list, engines, source_engine
    )
    assignment["cache_overlay"] = overlay_summary(
        assignment.get("query_assignments") or [], query_list
    )
    return assignment


def dropped_reason(engine: str) -> str:
    """``cache_reason`` of a query the safety net dropped."""
    return (
        f"cache overlay dropped after schema design: no in-scope {display_engine(engine)} "
        "access pattern serves it (owner unchanged)"
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
            name = display_engine(engine)
            _warn(
                qa,
                f"WARNING [MEDIUM]: Query {qa.get('query_id')} is cached by {name} at the "
                f"customer's request, but the {name} schema design has no in-scope access "
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


def _call_share(summary: Mapping | None) -> float:
    """``call_share_percent`` off an :func:`overlay_summary` dict, 0.0 when absent."""
    value = (summary or {}).get("call_share_percent")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def safety_net_note(
    engine: str,
    dropped: list[str],
    before: Mapping | None = None,
    after: Mapping | None = None,
) -> str:
    """The safety net's own note, shown to customers (#296, #424, #459).

    With ``before`` and ``after`` -- the :func:`overlay_summary` taken just
    before and just after the drop -- the note states how many hot reads were
    assigned at the assignment gate, how many the engine's schema design
    covers (and so stay cached), how many no longer are, and the after share
    of calls. Named with :func:`src.shared.engine_names.display_engine`,
    never the raw engine id, so the assignment gate's own ``cache_overlay``
    and this report's never disagree without saying why.

    This is the customer-facing summary (``cache_overlay.safety_net_notes``,
    #459 review): never the full ``cache_notes`` audit trail, which also
    carries customer-edit notes (full query hashes) and the legacy-migration
    note -- those stay internal, kept only for the engineering report
    (``cache_overlay.notes``). Per-query detail (which query, which reason)
    stays on :func:`dropped_reason`, set on each dropped
    ``QueryAssignment.cache_reason``; this note never repeats it per query.

    Without ``before`` (a caller with no snapshot), the note names only what
    was dropped, as before #459. ``after`` is never falsy on the before/after
    branch in production (:func:`src.agents.referee.synthesis_data.
    _apply_cache_safety_net` passes the zero-value fallback
    ``{"query_count": 0, "call_share_percent": 0.0}`` when
    :func:`overlay_summary` returns ``None`` -- dropping every cached read,
    #459 round 2) -- so the branch is keyed on ``before`` alone: a caller with
    a real ``before`` always has a real (possibly all-zero) ``after`` too.

    A schema-design re-run for the same assignment version starts from the
    *already-reduced* overlay the previous run persisted
    (``synthesis_handler._persist_cache_safety_net`` writes a drop back to the
    assignment artifact in place): a second run's own ``before`` is the first
    run's ``after``, not the original assignment-gate count. The note stays
    correct for what changed in *that* run; it is not a running total across
    runs.
    """
    n = len(dropped)
    if not before:
        noun = "hot read" if n == 1 else "hot reads"
        return (
            f"{n} {noun} had no in-scope access pattern in the "
            f"{display_engine(engine)} schema design, so "
            f"{'it is' if n == 1 else 'they are'} served by "
            f"{'its' if n == 1 else 'their'} owner engine only "
            "(cache overlay dropped, owner unchanged)."
        )
    before_count = int(before.get("query_count") or 0)
    after_count = int((after or {}).get("query_count") or 0)
    before_share = _call_share(before)
    after_share = _call_share(after)
    name = display_engine(engine)
    # #459 round 2/3: a full drop (after_count == 0) states it plainly
    # ("covers none of them", "all N", "None remain") instead of the
    # numerically-correct but easy-to-miss "covers 0 of them", "the other N",
    # "0 hot reads (0.0% of calls) remain." -- this is exactly the shape
    # #424's own report described (the cache engine leaves the engine list
    # entirely).
    if after_count == 0:
        covers = "covers none of them"
        no_longer_cached = "it is no longer cached" if n == 1 else f"all {n} are no longer cached"
        remaining = "None remain"
    else:
        covers = f"covers {after_count} of them"
        no_longer_cached = f"the other {n} {'is' if n == 1 else 'are'} no longer cached"
        remaining = (
            f"{after_count} hot {'read' if after_count == 1 else 'reads'} "
            f"({after_share:.1f}% of calls) {'remains' if after_count == 1 else 'remain'}"
        )
    return (
        f"{before_count} hot {'read' if before_count == 1 else 'reads'} "
        f"({before_share:.1f}% of calls) {'was' if before_count == 1 else 'were'} "
        f"assigned at the assignment gate. The {name} schema design {covers}; "
        f"{no_longer_cached} and {'stays' if n == 1 else 'stay'} served by "
        f"{'its' if n == 1 else 'their'} owner engine. {remaining}."
    )
