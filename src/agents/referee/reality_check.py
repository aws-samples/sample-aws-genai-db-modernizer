"""
Reality Check — CTO-level optimization of query-to-engine assignments.

Runs after the initial assignment and before schema design. Thinks like a CTO:
"Every additional database engine = monitoring, backups, failover, expertise,
and operational burden. Does this engine earn its place?"

Default posture: 2 engines max. A third engine must provide genuinely unique
capabilities that no other committed engine can serve.

Steps:
  1. Settle (Phase 1) — which engines earn a place: an engine is removed when the
     other committed engines can serve its queries (shared per-query scores)
  2. Place (Phase 2) — every query on its highest-scoring remaining engine
  3. Justification floors — a mandatory engine must still earn its fixed cost
  4. Architectural pattern detection — CQRS, materialized views, event sourcing
  5. Integration topology — recommend specific sync mechanisms and patterns

After validation corrections bring back a removed engine, the same review runs
again (``resettle_after_corrections``, ADR-031 Decision 7).

The output is a revised assignment (new version) plus architectural recommendations
that feed into synthesis.
"""

from __future__ import annotations

import itertools
import re
import warnings
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import can_own
from src.agents.referee.capability_registry import (
    can_engine_serve_capability,
    suggest_lightweight_alternative,
)
from src.agents.referee.tie_break import break_tie
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.shared.engine_elimination import in_scope_distribution
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine

if TYPE_CHECKING:
    from src.agents.referee.query_scoring import ScoringContext

# #490: this module used to keep its own fit-scoring capability matrix here
# (``ENGINE_CAPABILITIES``), separate from both the resolver's own fit score
# and ``capability_registry``'s hard-architectural-constraint matrix of the
# same name -- two capability tables and two fit scorers that could (and did)
# disagree. Deleted: every caller now reads ``query_scoring.
# score_query_for_engine`` (the capability/exclusion gates, the base
# confidence and the anti-pattern penalty, all in one call, against one
# ``ScoringContext``) for both fit and hard capability. There is now one
# fit function and one capability source.

# Minimum query threshold — if an engine has fewer queries than this,
# it's a trivial consolidation target (even without unique value check)
MIN_QUERIES_THRESHOLD = 5

# Target engine count — the reality check tries to stay at or below this.
# A third engine must provide genuinely unique, irreplaceable capabilities.
TARGET_ENGINE_COUNT = 2

# Per-engine operational overhead (monthly base cost + ops burden).
# This is not just infrastructure — it includes monitoring, backups,
# failover config, team expertise, security patching, etc.
ENGINE_BASE_COST: dict[str, float] = {
    "dynamodb": 0,  # Serverless, pay per use, minimal ops
    "documentdb": 200,  # Minimum cluster + ops burden
    "opensearch": 150,  # Minimum domain + ops burden
    "elasticache": 50,  # Minimum node + minimal ops
    "aurora_postgresql": 250,  # Minimum cluster (writer + reader) + ops burden
    "aurora_mysql": 250,  # Minimum cluster (writer + reader) + ops burden
}

# Additional operational burden for each engine beyond TARGET_ENGINE_COUNT.
# This captures the non-monetary cost: team needs expertise in N databases,
# N monitoring dashboards, N backup strategies, N failover runbooks, etc.
EXTRA_ENGINE_BURDEN_MONTHLY = 300  # $/mo equivalent per extra engine

# ---------------------------------------------------------------------------
# Cost-per-query floor for a mandatory engine (#167)
#
# A signal override makes an engine "mandatory" (Pass 0 never asks whether it
# has unique value), but that protection should not be permanent: if the
# engine's fixed monthly cost is high and the share of the workload it serves
# is tiny, it has not earned its place either, unless a capability (#338) or
# a dedicated justification (#326, OpenSearch's own floor) says otherwise.
# ---------------------------------------------------------------------------

# An engine's fixed monthly cost (ENGINE_BASE_COST) must be at least this much
# to be worth questioning at all -- DynamoDB ($0) and ElastiCache ($50) are
# cheap enough that a small share is not disproportionate.
MANDATORY_ENGINE_COST_FLOOR = 100

# Share of in-scope queries (0-1) below which a mandatory engine's fixed cost
# counts as disproportionate to the workload it serves.
MANDATORY_ENGINE_MAX_QUERY_SHARE = 0.05


def _mandatory_cost_share_reason(engine: str, qas: list[dict], total_queries: int) -> str | None:
    """None if ``engine``'s fixed cost is proportionate to its workload share (#167).

    Otherwise, the reason a mandatory signal override does not excuse it from
    the operational-burden review.
    """
    cost = ENGINE_BASE_COST.get(engine, 100)
    share = len(qas) / total_queries if total_queries else 0.0
    if cost >= MANDATORY_ENGINE_COST_FLOOR and share < MANDATORY_ENGINE_MAX_QUERY_SHARE:
        return (
            f"{display_engine(engine)} serves {len(qas)} of {total_queries} queries "
            f"({share:.1%}) at a fixed ${cost:.0f}/mo — disproportionate to the workload "
            "share it serves"
        )
    return None


# Set of Aurora engine identifiers
AURORA_ENGINES = {"aurora_postgresql", "aurora_mysql"}


def _query_has_join(query: dict) -> bool:
    """Whether ``query`` joins two or more tables (``has_joins``/``join_count``)."""
    return bool(query.get("has_joins")) or (query.get("join_count") or 0) >= 1


# ---------------------------------------------------------------------------
# Differentiation scoring — how much better is this engine vs alternatives?
# ---------------------------------------------------------------------------

# Minimum confidence delta for a query to be "unique" to an engine.
# If the best alternative is within this many points, the query is redundant.
UNIQUE_DELTA_THRESHOLD = 15

# Engines that may absorb only the queries their specialty serves (#296): an
# engine of this kind is a search/analytics read model, not a general owner, so
# consolidation never routes plain lookups or writes to it. Signals by query.
SPECIALIST_ABSORB_SIGNALS: dict[str, frozenset[str]] = {
    "opensearch": frozenset({"text_search", "aggregations"}),
}
SPECIALIST_ABSORB_CAPABILITIES: dict[str, frozenset[str]] = {
    "opensearch": frozenset({"inverted_index"}),
}

# ---------------------------------------------------------------------------
# OpenSearch justification floor (#326)
#
# A text_search signal override alone keeps OpenSearch "mandatory" everywhere
# else in this module, which is exactly how 4 LIKE/prefix admin-search queries
# at under 1 call/s kept a $150+/mo standing domain in a maintainer's run: the
# signal made the engine mandatory, so Pass 0 never asked whether it earns its
# fixed cost. This floor runs before Pass 0 so a signal override is never
# enough on its own -- OpenSearch also needs real search depth and either
# traffic to justify its cost, or a search pattern the source engine's own
# text_search_basic cannot already serve. Mirrors the ElastiCache hot-read
# floor (#304, cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND).
# ---------------------------------------------------------------------------

# Minimum combined traffic (calls/s) across every query OpenSearch would own,
# before its fixed monthly cost (ENGINE_BASE_COST["opensearch"]) is worth a
# standing domain. 1.0 is the ElastiCache hot-read floor's own value
# (cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND): below one call/s, a cached
# entry (or a search index) is read a few times before the next miss pays the
# same cost as never having it, so one call/s is the lowest traffic worth a
# dedicated store at all. On the wordpress reference sample, the 4 queries
# OpenSearch was recommended for totalled well under 1 call/s combined.
OPENSEARCH_MIN_CALLS_PER_SECOND = 1.0

# Source database engines whose own text search (MySQL FULLTEXT via
# MATCH...AGAINST, Postgres tsvector/pg_trgm) already covers these patterns
# for free -- the Aurora engines' native, basic full-text support. A
# dedicated OpenSearch domain must clear a higher traffic bar than
# OPENSEARCH_MIN_CALLS_PER_SECOND to be worth it anyway.
NATIVE_TEXT_SEARCH_SOURCE_ENGINES = frozenset({"mysql", "mariadb", "postgresql", "postgres"})

# The traffic floor a native-text-search source engine's queries must clear
# before OpenSearch is justified despite the source already covering the
# pattern. 10x the base floor: the source engine's own index is free (already
# paid for by the primary cluster), so a second, dedicated search engine only
# earns its $150+/mo on top of that if the traffic is an order of magnitude
# higher, not merely above the floor that justifies a search engine starting
# from nothing.
OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND = 10.0

# A query returning this many rows on average, even below the traffic floor,
# signals a corpus large enough that a dedicated index can still earn its
# cost (e.g. a low-traffic but very large full-text search over millions of
# rows). This is a result-size proxy, not a corpus-size one: the collector
# gives no table-level document-count stat, only rows_returned_avg (how much
# a query's own result set averages), which is the only per-query signal
# available -- a query that returns a small page from a huge corpus looks
# the same as one that returns everything from a small one.
OPENSEARCH_MIN_CORPUS_ROWS_AVG = 1000.0

# A string literal ('...' or "...") is replaced with a single placeholder
# before any pattern below runs, so a literal's own characters are never
# mistaken for a SQL operator -- a review of #375 found a leading-wildcard
# LIKE pattern (LIKE 'abc%') being read as the pg_trgm similarity operator
# because the '%' inside the literal matched the same way `title % $1` does.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _strip_string_literals(text: str) -> str:
    return _STRING_LITERAL_RE.sub("?", text)


# A search predicate: the query actually filters on a text-search pattern,
# not just an ordinary equality/range WHERE clause. Required before ranking,
# fuzzy matching or facets are even considered -- a bare GROUP BY, or a
# to_tsvector() call with no @@ match, is not a search at all (see below).
_SEARCH_PREDICATE_RE = re.compile(
    r"\b(like|ilike)\b|match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|"
    r"plainto_tsquery\s*\(|@@|similarity\s*\(|word_similarity\s*\(",
    re.IGNORECASE,
)

# Relevance ranking: an actual full-text match operation. to_tsvector() alone
# (document construction, e.g. CREATE EXTENSION's companion index build, or a
# plain SELECT to_tsvector(...) with no @@) does not count -- only a real
# match (the @@ operator, or constructing the query side with to_tsquery/
# plainto_tsquery) does.
_RELEVANCE_RANKING_RE = re.compile(
    r"match\s*\(.+?\)\s*against\s*\(|to_tsquery\s*\(|plainto_tsquery\s*\(|@@",
    re.IGNORECASE,
)

# Fuzzy matching: an actual similarity computation -- the similarity()/
# word_similarity() functions, the pg_trgm distance/threshold operators
# ("<%", "%>", word-similarity thresholds), or the real pg_trgm "%" operator
# (an identifier or closing paren, then "%", then a parameter placeholder,
# "$N" or "?", not immediately followed by a comparison or arithmetic
# operator). Checked against literal-stripped text only: a leading-wildcard
# LIKE pattern like 'abc%' has no "%" left once its literal is stripped, so
# it never matches this on its own (a review of #375 found the previous
# regex doing exactly that -- matching a LIKE pattern's own wildcard as the
# trigram operator). The negative lookahead after the parameter excludes
# plain modulo arithmetic against a parameter, e.g. "id % $2 = 0" or
# "id % $2 + 1" -- a real trigram "%" is used as a boolean predicate on its
# own, never followed immediately by another comparison or arithmetic
# operator (a second review of #375 found "id % $2 = 0" misread as fuzzy
# matching).
_FUZZY_MATCH_RE = re.compile(
    r"\bsimilarity\s*\(|\bword_similarity\s*\(|<%|%>|"
    r"[\w)]\s*%\s*(?:\$\d+|\?)(?!\s*(?:=|<>|!=|<=|>=|<|>|[+\-*/%]))",
    re.IGNORECASE,
)

# A facet: a search predicate combined with GROUP BY in the SAME query. A
# bare GROUP BY on an unrelated report query is not a facet.
_GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)


def _query_has_search_depth(query_text: str | None) -> bool:
    """Real search depth in one query (#326 finding 4): a search predicate
    plus relevance ranking, fuzzy matching, or a facet (GROUP BY alongside
    the same predicate) -- never a bare GROUP BY, a plain LIKE/prefix filter,
    a plain join, or to_tsvector()/pg_trgm document/extension setup with no
    match operator.
    """
    text = _strip_string_literals(query_text or "")
    if _RELEVANCE_RANKING_RE.search(text) or _FUZZY_MATCH_RE.search(text):
        return True
    return bool(_SEARCH_PREDICATE_RE.search(text) and _GROUP_BY_RE.search(text))


def opensearch_justification(
    qas: list[dict], query_map: dict[str, dict], source_engine: str | None
) -> dict[str, tuple[bool, str]]:
    """Per-query verdict on whether OpenSearch earns its place for each query
    it owns (#326, review of #375 finding B1).

    A query with no real search depth never earns a place on its own,
    regardless of traffic -- it is judged, and reported, independently of
    whatever else OpenSearch owns (finding 4: one shallow query must not
    sink a real search sitting next to it, and the reverse: one deep query
    must not shield a shallow one). The traffic/corpus floor is then judged
    only across the queries that *do* show real depth, so a key lookup
    alongside a genuine `@@ plainto_tsquery` search at real traffic no
    longer drags that search's own floor down (or the lookup's lack of
    traffic up).

    Returns ``{query_id: (justified, reason)}``; ``reason`` always explains
    the decision so the deliverables can state why.
    """
    texts = {qa["query_id"]: query_map.get(qa["query_id"], {}).get("query_text") for qa in qas}
    deep_qids = {qid for qid, t in texts.items() if _query_has_search_depth(t)}

    shallow_reason = (
        "no search predicate with relevance ranking, fuzzy matching or a facet in this query"
    )
    verdicts: dict[str, tuple[bool, str]] = {
        qa["query_id"]: (False, shallow_reason) for qa in qas if qa["query_id"] not in deep_qids
    }
    if not deep_qids:
        return verdicts

    deep_rows = {qid: query_map.get(qid, {}) for qid in deep_qids}
    total_cps = sum(float(q.get("calls_per_second") or 0) for q in deep_rows.values())
    total_rows_avg = sum(float(q.get("rows_returned_avg") or 0) for q in deep_rows.values())
    native = (source_engine or "").lower() in NATIVE_TEXT_SEARCH_SOURCE_ENGINES
    floor = (
        OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND
        if native
        else OPENSEARCH_MIN_CALLS_PER_SECOND
    )

    if total_cps < floor and total_rows_avg < OPENSEARCH_MIN_CORPUS_ROWS_AVG:
        source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(
            (source_engine or "").lower(), source_engine or ""
        )
        native_note = (
            f"{source_name} already supports this search pattern natively "
            "(text_search_basic), so "
            if native
            else ""
        )
        reason = (
            f"{native_note}combined traffic {total_cps:.2f} calls/s across "
            f"{len(deep_qids)} queries with real search depth is below the {floor:g} calls/s "
            f"floor (and the {total_rows_avg:.0f}-row average result size is below the "
            f"{OPENSEARCH_MIN_CORPUS_ROWS_AVG:g}-row floor) for a "
            f"${ENGINE_BASE_COST.get('opensearch', 0):.0f}/mo standing domain"
        )
        for qid in deep_qids:
            verdicts[qid] = (False, reason)
        return verdicts

    reason = (
        f"{len(deep_qids)} queries need relevance ranking/fuzzy matching/facets at "
        f"{total_cps:.2f} calls/s (average result size {total_rows_avg:.0f} rows), clearing "
        f"the {floor:g} calls/s floor or the {OPENSEARCH_MIN_CORPUS_ROWS_AVG:g}-row floor"
    )
    for qid in deep_qids:
        verdicts[qid] = (True, reason)
    return verdicts


def _workload_ever_justified(
    engine: str,
    first_assignment_qas: list[dict],
    query_map: dict[str, dict],
    source_engine: str | None,
) -> bool | None:
    """Whether ``engine``'s FIRST-ASSIGNMENT workload ever earned its place (#475 review).

    ``None`` when no justification floor applies to ``engine`` at all (only
    OpenSearch has one, #326) -- the caller must not claim or deny a saving
    based on this when it's ``None``. ``True`` when at least one of the
    engine's own first-assignment queries clears ``opensearch_justification``
    (real search depth plus traffic or corpus size); ``False`` when none do.

    Judged once, here, against the engine's queries *before* any reality
    check pass moves anything -- a later pass fully consolidating this
    engine away is removing something that was never justified by the
    workload in the first place, not something that earned its place and
    then stopped being worth its cost. ``_build_recommendations`` reads this
    (carried on each consolidation record) to avoid presenting that removal
    as an infrastructure saving.
    """
    if engine != "opensearch" or not first_assignment_qas:
        return None
    verdicts = opensearch_justification(first_assignment_qas, query_map, source_engine)
    return any(justified for justified, _ in verdicts.values())


def may_absorb(
    engine: str,
    qid: str,
    query: dict,
    query_signals: dict[str, list[str]],
    query_capabilities: dict[str, list[str]] | None = None,
) -> bool:
    """Whether consolidation may move query ``qid`` onto ``engine`` (#296).

    The write gate (a cache owns nothing, a non-system-of-record engine owns no
    write), and specialist engines absorb only queries that need their specialty.
    """
    if not can_own(engine, query):
        return False
    signals = SPECIALIST_ABSORB_SIGNALS.get(engine)
    if signals is None:
        return True
    caps = SPECIALIST_ABSORB_CAPABILITIES.get(engine, frozenset())
    return bool(
        signals & set(query_signals.get(qid, []))
        or caps & set((query_capabilities or {}).get(qid, []))
    )


# ---------------------------------------------------------------------------
# Architectural patterns
# ---------------------------------------------------------------------------

ARCHITECTURAL_PATTERNS: dict[str, dict] = {
    "CQRS": {
        "name": "Command Query Responsibility Segregation (CQRS)",
        "description": (
            "Separate write operations (commands) from read operations (queries) "
            "across different databases. The write database is optimized for "
            "transactional consistency, while the read database is optimized for "
            "query performance and flexibility."
        ),
        "when": "One engine handles most writes and another handles reads/search/analytics",
        "example": (
            "DynamoDB handles all CRUD operations (writes + simple reads). "
            "OpenSearch serves as a read-optimized view for full-text search, "
            "faceted filtering, and analytics queries. Data flows from DynamoDB "
            "to OpenSearch via zero-ETL integration."
        ),
    },
    "MATERIALIZED_VIEW": {
        "name": "Materialized View Pattern",
        "description": (
            "Maintain a pre-computed, denormalized copy of data in a secondary "
            "database optimized for specific query patterns. The secondary store "
            "is a read-only projection — all writes go through the primary."
        ),
        "when": "A secondary engine serves read-only queries on data owned by the primary",
        "example": (
            "DynamoDB is the source of truth for forum data. OpenSearch maintains "
            "a materialized view of discussions + users for full-text search. "
            "The view is automatically kept in sync via DynamoDB zero-ETL."
        ),
    },
    "EVENT_SOURCING": {
        "name": "Event-Driven Sync",
        "description": (
            "Use database change streams or event logs to propagate changes "
            "between databases asynchronously. Each database maintains its own "
            "optimized representation of the data."
        ),
        "when": "Multiple engines need the same data in different shapes",
        "example": (
            "DynamoDB Streams captures all item changes. A Lambda function "
            "transforms and routes changes to OpenSearch (for search) and "
            "ElastiCache (for leaderboards). Each consumer shapes the data "
            "for its specific access patterns."
        ),
    },
    "POLYGLOT_PERSISTENCE": {
        "name": "Polyglot Persistence",
        "description": (
            "Use different databases for different bounded contexts within "
            "the application. Each service/module owns its data in the database "
            "best suited for its access patterns."
        ),
        "when": "Different parts of the application have fundamentally different data access needs",
        "example": (
            "User profiles and forum CRUD in DynamoDB (key-value + hierarchical). "
            "Search and discovery in OpenSearch (full-text + relevance). "
            "Real-time leaderboards in ElastiCache (sorted sets + pub/sub)."
        ),
    },
}


def _resolver_score(trace_by_query: dict[str, dict], qid: str, engine: str) -> float | None:
    """``engine``'s score for ``qid``, exactly as ``AssignmentResolver.resolve()``
    used it (``RoutingTrace.by_query[qid]["engines"][engine]``) -- ``None`` when
    the resolver's own hard exclusion/capability/write gate ruled ``engine`` out
    for this query entirely, never a recompute via a different scorer (#502
    review: Phase 1 and Phase 2 must read the identical scores, not Phase 1
    using the reality check's own, separate ``_engine_fit_score``).
    """
    payload = trace_by_query.get(qid)
    if not payload:
        return None
    value = payload.get("engines", {}).get(engine)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if "gate" in value:
            return None
        used = value.get("used")
        return float(used) if isinstance(used, (int, float)) else None
    return None


def _query_eligible_engines(
    qid: str,
    qa: dict,
    current_engine: str | Iterable[str],
    candidates: Iterable[str],
    trace_by_query: dict[str, dict],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
) -> list[str]:
    """``candidates`` that may actually own ``qid`` -- the ONE eligibility
    rule Phase 1 (which engines earn a place) and Phase 2 (where every
    query lands) both apply, so a query can never be judged eligible for
    one phase's purposes and ineligible for the other's (#502 review:
    this gap is exactly how a query could end up stranded on an engine
    Phase 1 had already removed).

    ``current_engine`` is normally the single engine the query is on today.
    For a query still flagged ``tied_engines`` (#503), callers pass the
    whole tied set instead of the one, arbitrary
    placeholder engine: judging eligibility from a single tied engine let
    the placeholder's own, otherwise-inconsequential pick decide the
    result through this gate -- a join query tied 70/70 between DocumentDB
    and DynamoDB became eligible for DynamoDB or not purely depending on
    which of the two the placeholder happened to be recorded as, silently
    turning a non-decision into one. Treating every tied engine as
    "current" means the gates below answer the same way regardless of
    which placeholder a prior run happened to pick.

    Four gates, all of which must pass:
    - the resolver's own score exists at all (``_resolver_score`` is not
      ``None``) -- the resolver's own hard exclusion/capability/write gate
      ruled the engine out for this query, so there is nothing for Phase 2
      to place it with even if the checks below would otherwise allow it;
    - the ownership/write gate and specialist-absorb restriction
      (``may_absorb``) -- a specialist engine (OpenSearch) only ever
      receives a query that matches its own specialty;
    - the hard capability gate (``can_engine_serve_capability``);
    - the join-on-DynamoDB block (review of #375, finding B2): a join
      query consolidated away from a non-Aurora engine never lands on
      DynamoDB. A join already on DynamoDB, or arrived there from Aurora,
      is unaffected -- this only blocks a *consolidation* onto it, so
      ``current_engine`` (where the query sits right now) decides whether
      the block applies, not just which engine is being considered.
    """
    required_caps = (query_capabilities or {}).get(qid, [])
    query = query_map.get(qid, qa)
    current_set = {current_engine} if isinstance(current_engine, str) else set(current_engine)
    # Named for what it gates, not what it checks (#502 review round 5):
    # this is true exactly when considering ``e`` for ``qid`` would be a
    # *consolidation onto* ``e`` away from an engine that is neither
    # DynamoDB nor Aurora -- the join-on-DynamoDB block below only applies
    # to that case, never to a join query already on DynamoDB or Aurora.
    # With a tied set, this is False (not "leaving") as soon as ANY tied
    # engine is dynamodb/Aurora -- the best case for dynamodb's own
    # eligibility, so an arbitrary OTHER placeholder pick can't block it.
    leaving_non_aurora_non_dynamodb_engine = not (current_set & ({"dynamodb"} | AURORA_ENGINES))

    def ok(e: str) -> bool:
        if _resolver_score(trace_by_query, qid, e) is None:
            return False
        # The engine already (legitimately) owning this query is always
        # eligible to keep it: ``may_absorb``'s specialist-absorb
        # restriction is about CONSOLIDATING a query onto an engine, never
        # about an engine continuing to hold what it already has (#502
        # review: applying it unconditionally stripped OpenSearch of its
        # own already-matched query whenever no triage signal/capability
        # happened to be attached to it, moving it away for no score
        # reason at all).
        if e not in current_set and not may_absorb(
            e, qid, query, query_signals, query_capabilities
        ):
            return False
        if not can_engine_serve_capability(e, required_caps):
            return False
        if (
            e == "dynamodb"
            and leaving_non_aurora_non_dynamodb_engine
            and _query_has_join(query_map.get(qid, {}))
        ):
            return False
        return True

    return [e for e in candidates if ok(e)]


def _current_for_eligibility(
    qid: str,
    current_engine_by_qid: dict[str, str],
    tied_engines_by_qid: Mapping[str, list[str]] | None,
) -> str | list[str]:
    """What ``_query_eligible_engines`` should treat as ``qid``'s current
    engine(s): its whole ``tied_engines`` set when it has one (#503), else
    the one real engine it is on today."""
    if tied_engines_by_qid:
        tied = tied_engines_by_qid.get(qid)
        if tied:
            return tied
    return current_engine_by_qid.get(qid, "")


def _group_eligible_engines(
    qid: str,
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    candidates: Iterable[str],
    mandatory_query_ids: set[str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
) -> list[str]:
    """``candidates`` eligible to hold ``qid`` -- group-aware (#502 review
    round 5, finding G).

    A co-dependency group moves as one unit, so an engine is eligible for a
    grouped query only if it is eligible for EVERY non-mandatory member of
    that group, including members currently sitting on a different engine,
    not just ``qid`` itself (``_query_eligible_engines`` on its own only
    ever asks about one query). An engine that cannot take the whole group
    is irreplaceable for it -- never split a group to make an engine look
    safe to remove, or to decide where it lands.

    A mandatory member (a signal override, ADR #296) never travels with its
    group, so it is excluded from the "every member" requirement here, the
    same way Phase 2 already excludes it when it applies the group's move.

    ``tied_engines_by_qid`` (#503) is forwarded to ``_query_eligible_engines``
    for every member still flagged with a
    tie, so eligibility never depends on which arbitrary engine a prior
    run's placeholder happened to pick.

    This is the ONE eligibility entry point both Phase 1
    (``_evaluate_engine_removal``, ``_settle_committed_engines``) and
    Phase 2 (``_place_settled_queries``) use, for grouped and ungrouped
    queries alike -- ``_query_eligible_engines`` itself is the single-query
    primitive this calls, never a second, divergent rule.
    """
    candidates = list(candidates)
    current_engine = _current_for_eligibility(qid, current_engine_by_qid, tied_engines_by_qid)
    group_info = trace_by_query.get(qid, {}).get("group")
    if not group_info:
        return _query_eligible_engines(
            qid,
            query_map.get(qid, {"query_id": qid}),
            current_engine,
            candidates,
            trace_by_query,
            query_signals,
            query_map,
            query_capabilities,
        )
    group_key = group_info["key"]
    members = [
        mqid
        for mqid, mpayload in trace_by_query.items()
        if (mg := mpayload.get("group")) and mg["key"] == group_key
        if mqid not in mandatory_query_ids
    ]
    return [
        e
        for e in candidates
        if all(
            e
            in _query_eligible_engines(
                m_qid,
                query_map.get(m_qid, {"query_id": m_qid}),
                _current_for_eligibility(m_qid, current_engine_by_qid, tied_engines_by_qid),
                {e},
                trace_by_query,
                query_signals,
                query_map,
                query_capabilities,
            )
            for m_qid in members
        )
    ]


def _evaluate_engine_removal(
    engine: str,
    native_qas: list[dict],
    other_committed: set[str],
    mandatory_query_ids: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    committed_count: int,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    orphaned_qas: list[dict] | None = None,
) -> tuple[bool, dict, float]:
    """Phase 1's per-engine question (#502 review): judged on ``engine``'s
    own native queries alone, using the resolver's own per-query scores
    (``_resolver_score``) -- never the reality check's separate fit
    scorer, and never a query another engine's placement happened to land
    on it this run. No engine is protected for being cheapest or
    "primary": the only queries that keep an engine committed outright
    are a mandatory signal override (ADR #296) or a query no other
    committed engine can serve at all (the shared, group-aware
    eligibility gate ``_group_eligible_engines`` -- #502 review round 5,
    finding G: a co-dependency group with a member on ``engine`` is
    irreplaceable for ``engine`` unless some survivor is eligible for
    EVERY member of that group, not just the one native to ``engine`` --
    never a score comparison). Short of that, the engine is removed
    unless at least one native query scores it ``UNIQUE_DELTA_THRESHOLD``
    points above every engine that could take it, and -- once more than
    ``TARGET_ENGINE_COUNT`` engines are already committed -- that unique
    value is worth the fixed cost of keeping it (#494: unchanged, flat
    ``EXTRA_ENGINE_BURDEN_MONTHLY`` and ``> 100`` cut-off).

    ``orphaned_qas`` (#485 review) are the native queries of engines
    ALREADY removed earlier in this same settling pass -- one flat,
    ever-growing list, not tracked per rescuing engine. The single rule:
    every one of them must still have an eligible home among
    ``other_committed`` (``engine`` excluded, like everything else here),
    whichever engine that turns out to be. Without this, ``engine``'s own
    removability check only ever saw ``native_qas`` (frozen from the first
    assignment), so a query rescued onto ``engine`` -- because ``engine``
    was its only eligible survivor when its own original engine was
    removed -- was invisible here, stranding it if ``engine`` was removed
    too, judged purely on ITS unrelated native queries. A stranded one is
    added to ``irreplaceable_queries`` (sorted, alongside any native ones)
    so ``engine`` is protected the same hard way a mandatory signal
    override is -- but ``total_queries``, ``unique_queries``,
    ``redundant_queries`` and ``unique_ratio`` below are computed from
    ``native_qas`` alone, same as if ``orphaned_qas`` were empty: it is
    not actually ``engine``'s own native workload.

    Also returns a rank key (burden per unique query) ``_settle_committed_engines``
    uses to decide WHICH removable engine to remove first when more than
    one is removable in the same round -- the worst value for the fixed
    operational cost, not evaluation order.
    """

    def _stranded(qas: Iterable[dict]) -> list[str]:
        stranded = []
        for qa in qas:
            qid = qa["query_id"]
            if qid in mandatory_query_ids:
                continue
            if not _group_eligible_engines(
                qid,
                trace_by_query,
                current_engine_by_qid,
                other_committed,
                mandatory_query_ids,
                query_signals,
                query_map,
                query_capabilities,
                tied_engines_by_qid,
            ):
                stranded.append(qid)
        return stranded

    orphan_stranded = _stranded(orphaned_qas or [])

    if not native_qas:
        return (
            not orphan_stranded,
            {
                "total_queries": 0,
                "irreplaceable_queries": sorted(orphan_stranded),
                "unique_queries": [],
                "redundant_queries": [],
                "unique_ratio": 0,
                "avg_delta": 0,
            },
            0.0,
        )

    irreplaceable: list[str] = []
    unique_by_score: list[str] = []
    redundant: list[str] = []
    deltas: list[float] = []

    for qa in native_qas:
        qid = qa["query_id"]
        if qid in mandatory_query_ids:
            irreplaceable.append(qid)
            continue
        eligible_alts = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            other_committed,
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        if not eligible_alts:
            irreplaceable.append(qid)
            continue
        this_score = _resolver_score(trace_by_query, qid, engine) or 0
        best_alt = max(_resolver_score(trace_by_query, qid, alt) or 0 for alt in eligible_alts)
        delta = this_score - best_alt
        deltas.append(delta)
        if delta >= UNIQUE_DELTA_THRESHOLD:
            unique_by_score.append(qid)
        else:
            redundant.append(qid)

    avg_delta = sum(deltas) / len(deltas) if deltas else 0
    assessment = {
        "total_queries": len(native_qas),
        "irreplaceable_queries": sorted(set(irreplaceable) | set(orphan_stranded)),
        "unique_queries": unique_by_score,
        "redundant_queries": redundant,
        "unique_ratio": (
            (len(irreplaceable) + len(unique_by_score)) / len(native_qas) if native_qas else 0
        ),
        "avg_delta": round(avg_delta, 1),
    }

    total_burden = ENGINE_BASE_COST.get(engine, 100) + EXTRA_ENGINE_BURDEN_MONTHLY
    rank_key = total_burden / max(len(unique_by_score), 1)

    if irreplaceable or orphan_stranded:
        return False, assessment, 0.0
    if not unique_by_score:
        return True, assessment, rank_key
    if committed_count > TARGET_ENGINE_COUNT and rank_key > 100:
        return True, assessment, rank_key
    return False, assessment, 0.0


def _round_evaluation_order(snapshot: set[str]) -> list[str]:
    """The order a round evaluates each committed engine's removability in.

    Alphabetical by default -- a plain, deterministic choice, not a rule
    any engine's removal depends on (every engine is judged against the
    SAME frozen snapshot regardless of the order it is checked in; see
    ``_settle_committed_engines``). A separate, overridable function so a
    test can force a different evaluation order and confirm the settled
    engine set does not change (#502 review item 7: order-independence
    should hold for evaluation order, not just input order).
    """
    return sorted(snapshot)


def _removal_tiebreak_key(
    engine: str,
    native_qas: list[dict],
    survivors: set[str],
    mandatory_query_ids: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
) -> tuple[int, float]:
    """Data-driven tiebreaker between two candidates tied on burden-per-unique-
    query (#502 review round 5, finding N1; round 6 corrected the second
    measure) -- no engine name involved.

    Simulates ``engine``'s removal: for every one of its native,
    non-mandatory queries, finds the highest-scoring eligible survivor
    (group-aware, same rule as everywhere else) it would actually land on.
    Returns ``(n_destinations, total_fit_lost)`` -- sorting candidates
    ascending on this prefers fewer distinct destination engines first (the
    more consolidated outcome), then the LEAST total fit lost (``engine``'s
    own score minus the destination's score, summed over every one of its
    queries, not just the ones that cleared ``UNIQUE_DELTA_THRESHOLD`` --
    an engine with a pile of barely-redundant queries loses more by being
    removed than one with the same unique-query count and nothing else).
    Two candidates that still tie on BOTH measures are genuinely
    indistinguishable by any data this review has identified; the caller
    reports that rather than picking one.

    Finding a query's destination among ``alts`` used to be a bare
    ``max(alts, key=score)`` over ``alts`` -- a list built from the
    ``survivors`` *set*, so when two or more alternates scored this query
    identically, which one "won" depended on Python's set iteration order:
    ``PYTHONHASHSEED`` and input order, neither an ADR-031 rule (#569).
    Scores are now collected into a ``dict`` and the top scorers read back
    through ``_top_scorers`` (sorted -- a stable key), and an actual tie
    among them is resolved the same ADR-031 Decision 2 way every other
    routing tie is: ``tie_break.break_tie``, fed this call's own
    ``source_compatible_engine``/``settled_counts`` and the query id, never
    by whichever alternate a set happened to produce first.
    """
    destinations: set[str] = set()
    total_fit_lost = 0.0
    for qa in native_qas:
        qid = qa["query_id"]
        if qid in mandatory_query_ids:
            continue
        alts = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            survivors,
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        if not alts:
            continue
        own_score = _resolver_score(trace_by_query, qid, engine) or 0
        alt_scores = {alt: _resolver_score(trace_by_query, qid, alt) or 0 for alt in alts}
        top_alts = _top_scorers(alt_scores)
        best_alt = (
            top_alts[0]
            if len(top_alts) == 1
            else break_tie(top_alts, set(), source_compatible_engine, settled_counts, qid)[0]
        )
        best_score = alt_scores[best_alt]
        destinations.add(best_alt)
        total_fit_lost += own_score - best_score
    return len(destinations), total_fit_lost


def _settle_committed_engines(
    engine_queries: dict[str, list[dict]],
    mandatory_query_ids: set[str],
    mandatory_committed_engines: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    max_rounds: int,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
) -> tuple[set[str], dict[str, dict]]:
    """Phase 1 (#502 review): settle which engines earn a place, independent
    of the order they happen to be evaluated in.

    Every engine in a round is judged against the SAME frozen snapshot of
    the committed set at the round's start, using the resolver's own
    per-query scores -- the verdict for any engine never depends on which
    other engine was checked first. Only ONE engine is actually removed per
    round: the removable candidate with the worst burden-per-unique-query
    (the least economically justified, a data-driven key, not evaluation
    order), and only if removing it alone leaves every one of its native
    queries -- and every co-dependency group with a member on it, as a
    whole unit (``_group_eligible_engines``, #502 review round 5, finding
    G) -- with an eligible home among everyone else still committed.
    Removing just one at a time, then re-evaluating everyone fresh against
    the smaller set, is what review round 3 found round-1's "remove every
    candidate in the round at once" batching got wrong: a cheap, high-volume
    engine could be charged the ``TARGET_ENGINE_COUNT`` burden only because
    another engine, being removed in the very same round, was still being
    counted toward the total. Repeats until a round removes nothing, capped
    at ``max_rounds`` (a warning, not a crash, if that cap is ever hit).

    When two or more candidates tie on burden-per-unique-query, which one
    actually gets removed is NOT decided by engine name (#502 review round
    5, finding N1: that is an arbitrary rule, and changing the input order
    of two equal-cost engines proved it mattered). ``_removal_tiebreak_key``
    resolves the tie by simulating each removal and comparing the result;
    if two candidates are still indistinguishable after that, this function
    removes neither of them this round -- both stay committed, flagged
    ``tie_unresolved`` in the assessment, and a different, non-tied,
    lower-ranked candidate (if any) is tried instead.

    ``source_compatible_engine``/``settled_counts`` are forwarded to
    ``_removal_tiebreak_key`` (#569) so that when ITS OWN simulation finds a
    query with two or more equally-scored destination engines, that
    destination tie goes through the same shared ``tie_break.break_tie``
    every other routing tie does, instead of ``max()`` over whatever order
    the eligible-engines set happens to iterate in -- which depended on
    ``PYTHONHASHSEED`` and input order, not on anything ADR-031 recognizes.
    """
    committed = {e for e in engine_queries if engine_queries.get(e)}
    assessment: dict[str, dict] = {}
    # #485 review: the native, non-mandatory queries of every engine
    # ALREADY removed earlier in this same settling pass -- one flat,
    # ever-growing list, not tracked per rescuing engine. Every engine's
    # removability check (``_evaluate_engine_removal``) re-verifies that
    # all of them still have a home among whichever engines remain, so a
    # chain of rescues (an engine that absorbed one of these, later
    # removed itself) is handled the same way a single rescue is, with no
    # per-destination bookkeeping: a query is appended exactly once, when
    # its own original engine is removed, never again afterward.
    orphaned_qas: list[dict] = []
    round_num = 0
    converged = False
    while round_num < max_rounds:
        round_num += 1
        snapshot = set(committed)
        candidates: list[tuple[float, str]] = []
        for engine in _round_evaluation_order(snapshot):
            other = snapshot - {engine}
            removable, info, rank_key = _evaluate_engine_removal(
                engine,
                engine_queries.get(engine, []),
                other,
                mandatory_query_ids,
                trace_by_query,
                current_engine_by_qid,
                query_signals,
                query_map,
                query_capabilities,
                len(snapshot),
                tied_engines_by_qid,
                orphaned_qas,
            )
            # #502 review: no engine is "primary" anymore -- every engine is
            # judged the same way. The contract field stays (a breaking
            # contract change is its own proposal, per CONTRIBUTING.md) but
            # is always False now rather than naming a protected engine.
            info["is_primary"] = False
            info["is_mandatory"] = engine in mandatory_committed_engines
            assessment[engine] = info
            if removable:
                candidates.append((rank_key, engine))
        if not candidates:
            converged = True
            break

        # Remove exactly one engine this round: the worst-ranked (highest
        # burden-per-unique-query) candidate that is actually safe on its
        # own. A tie in rank is resolved by data (``_removal_tiebreak_key``),
        # never by engine name; a tie that survives that too is reported,
        # not broken (both candidates stay committed this round).
        by_rank: dict[float, list[str]] = defaultdict(list)
        for rank_key, engine_name in candidates:
            by_rank[rank_key].append(engine_name)

        removed_one = False
        for rank_key in sorted(by_rank, reverse=True):
            tied = by_rank[rank_key]
            survivors_by_candidate = {c: snapshot - {c} for c in tied}
            if len(tied) > 1:
                extra = {
                    c: _removal_tiebreak_key(
                        c,
                        engine_queries.get(c, []),
                        survivors_by_candidate[c],
                        mandatory_query_ids,
                        trace_by_query,
                        current_engine_by_qid,
                        query_signals,
                        query_map,
                        query_capabilities,
                        tied_engines_by_qid,
                        source_compatible_engine,
                        settled_counts,
                    )
                    for c in tied
                }
                best = min(extra.values())
                decisive = sorted(c for c in tied if extra[c] == best)
                if len(decisive) > 1:
                    for c in decisive:
                        assessment[c]["tie_unresolved"] = True
                    continue
            else:
                decisive = tied
            candidate = decisive[0]
            survivors = survivors_by_candidate[candidate]
            native_qas = engine_queries.get(candidate, [])
            # #485 review: removing `candidate` must leave a home for its
            # own native queries AND for every query already orphaned by
            # an earlier removal this pass -- the same single rule
            # `_evaluate_engine_removal` just applied when it judged
            # `candidate` removable against this same `survivors` set.
            if all(
                _group_eligible_engines(
                    qa["query_id"],
                    trace_by_query,
                    current_engine_by_qid,
                    survivors,
                    mandatory_query_ids,
                    query_signals,
                    query_map,
                    query_capabilities,
                    tied_engines_by_qid,
                )
                for qa in native_qas + orphaned_qas
                if qa["query_id"] not in mandatory_query_ids
            ):
                committed.discard(candidate)
                removed_one = True
                # `candidate`'s own native queries are now orphaned too --
                # appended exactly once, here, never re-added on a later
                # removal (they are already in the list by then).
                orphaned_qas.extend(
                    qa for qa in native_qas if qa["query_id"] not in mandatory_query_ids
                )
                break
        if not removed_one:
            converged = True
            break

    if not converged:
        warnings.warn(
            f"_settle_committed_engines did not converge within {max_rounds} rounds "
            f"(stopped with {sorted(committed)} still committed) -- stopping instead of "
            "looping forever; this should not happen outside a pathological input.",
            stacklevel=2,
        )
    return committed, assessment


def _pick_winner(
    scores: dict[str, float],
    current_engines: set[str],
    source_compatible_engine: str | None,
    settled_counts: Mapping[str, int] | None = None,
    query_id: str = "",
) -> tuple[str, str | None]:
    """Highest score wins. Returns ``(winner, tie_kind)``; ``tie_kind`` is
    ``None`` when one engine strictly scores highest (no tie to resolve),
    otherwise the result of ``break_tie`` (shared with the resolver's
    ``break_owner_tie`` and this module's absorber tie-break, #503) -- see
    its docstring for the three-rule order (source-compatible, then
    stability, then an honestly-reported ``tie_unresolved``, decided by the
    maintainer's placeholder when it comes to that -- ``settled_counts``/
    ``query_id`` feed that placeholder only).
    """
    tied = _top_scorers(scores)
    if len(tied) == 1:
        return tied[0], None
    return break_tie(tied, current_engines, source_compatible_engine, settled_counts, query_id)


def _top_scorers(scores: dict[str, float]) -> list[str]:
    """Engines tied at the top of ``scores`` (length 1 when there is no tie)."""
    top = max(scores.values())
    return sorted(e for e, s in scores.items() if s == top)


def _tie_unresolved_note(tied: Iterable[str], winner: str) -> str:
    """Customer-facing fragment for an unresolved tie (#503 review item 4):
    display names only -- no raw engine code, no internal ADR-031
    reference, and the placeholder pick is never called "non-preferential"
    (it is named for what it is: a placeholder pending the customer's
    choice, not a decided rule)."""
    others = ", ".join(display_engine(e) for e in sorted(set(tied)) if e != winner)
    return (
        f" (scores tie with {others}; no routing rule decides between them; "
        f"listed under {display_engine(winner)} pending your choice)"
    )


def _phase2_reason(
    current_engine: str,
    winner: str,
    eligible_scores: dict[str, float],
    tie_kind: str | None,
    *,
    moved: bool,
    group: bool,
    original_tied: Iterable[str] | None = None,
) -> str:
    """Build a Phase 2 ``assignment_reason`` honestly (#503): engines named
    with ``display_engine``, no raw code and no internal ``ADR-031``
    reference, and rebuilt from the CURRENT verdict every time a tie is
    involved -- even when the engine does not change (a tie that clears
    while the query stays put must not keep reading an earlier "pending
    your choice" sentence written before it cleared). ``original_tied``
    (the engines this query was tied between before this phase ran) names
    why a now-cleared tie cleared, when it did -- which of those engines
    this phase removed from consideration -- rather than a generic "is now
    resolved" with no reason given."""
    winner_name = display_engine(winner)
    if moved:
        basis = (
            "co-dependency group moves together"
            if group
            else (
                f"{display_engine(current_engine)} scores "
                f"{eligible_scores.get(current_engine, 'n/a')}, {winner_name} scores "
                f"{eligible_scores[winner]}"
            )
        )
        tie_note = (
            _tie_unresolved_note(_top_scorers(eligible_scores), winner)
            if tie_kind == "tie_unresolved"
            else ""
        )
        return (
            f"reality check: consolidated from {display_engine(current_engine)} → "
            f"{winner_name} (no unique value — {basis}{tie_note})"
        )
    if tie_kind == "tie_unresolved":
        return (
            "reality check: no unique value elsewhere"
            f"{_tie_unresolved_note(_top_scorers(eligible_scores), winner)}"
        )
    removed = sorted(e for e in (original_tied or []) if e != winner and e not in eligible_scores)
    if removed:
        removed_text = ", ".join(display_engine(e) for e in removed)
        verb = "no longer qualifies" if len(removed) == 1 else "no longer qualify"
        return (
            f"reality check: {winner_name} is kept -- {removed_text} {verb} for this "
            "query, so the earlier scores tie is resolved"
        )
    return (
        f"reality check: {winner_name} is kept -- the earlier scores tie between "
        "engines is now resolved"
    )


def _place_settled_queries(
    query_assignments: list[dict],
    committed_engines: set[str],
    mandatory_query_ids: set[str],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    trace_by_query: dict[str, dict],
    source_compatible_engine: str | None,
    workload_justified: dict[str, bool | None],
    tie_unresolved_qids: set[str] | None = None,
    settled_counts: Mapping[str, int] | None = None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    held_query_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Phase 2 (#502 review): place every query on its highest-scoring
    engine within the settled committed set, using the SAME per-query
    scores (``trace_by_query``, built once in ``run_reality_check`` and
    shared with Phase 1) and the SAME, group-aware eligibility rule
    (``_group_eligible_engines``) Phase 1 used to decide which engines
    earn a place -- never the reality check's own, separate fit scorer,
    and never a different eligibility answer than Phase 1 already relied
    on when it decided an engine was safe to remove.

    A query whose engine survived Phase 1 stays there unless another
    eligible, surviving engine scores it strictly higher (see
    ``_pick_winner`` for the tie order). A co-dependency group moves as
    one unit, by the SAME group score the resolver itself compared when it
    first placed the group, onto an engine ``_group_eligible_engines`` says
    is eligible for every member -- Phase 1 never removes an engine while
    one of its groups has no such survivor (#502 review round 5, finding
    G), so this should never come up empty; if it somehow did, the group
    stays exactly where it is (the same "should not happen" outcome as an
    individual query with nothing eligible), never split apart to force a
    placement. Because every query is placed fresh against the final
    committed set, not incrementally against a list that can go stale, no
    query should ever end up on a removed engine; ``run_reality_check``
    asserts that invariant right after this call rather than trusting it
    silently.

    ``tie_unresolved_qids``/``tied_engines_by_qid`` (#503 review, both
    rounds) mark queries whose FIRST-ASSIGNMENT engine is only a
    placeholder pick from an unresolved tie: that placeholder is never
    passed to ``_pick_winner`` as a stability candidate (a placeholder pick
    is not "today's real engine," so it earns no stability credit), and
    such a query is RE-RESOLVED HERE AMONG THE ORIGINALLY TIED ENGINES
    FIRST (round 2, findings C1/C2): if one or more of them are still
    committed and eligible, the candidate pool is restricted to just those
    -- never opened up to the full committed set while a tied engine still
    survives. Only when NONE of the originally tied engines survive does
    the query fall back to the full committed set, placed on its
    best-scoring survivor there with an honest reason, same as any other
    consolidation. Either way, every query this bookkeeping touches gets a
    FRESH ``tied_engines``/reason from the verdict just computed (round 2,
    findings I2/I3): a tie that clears, even while the engine does not
    change, is never left reading a stale "pending your choice" sentence,
    and a tie that first appears here (two non-source-compatible engines
    newly tying for the top score during consolidation) is recorded too,
    not silently dropped because it was never tied before.

    ``held_query_ids`` (#588) are already part of ``mandatory_query_ids``
    here (they never move either); this set only names them correctly in a
    partial consolidation's ``retention_reason``.
    """
    tie_unresolved_qids = tie_unresolved_qids or set()
    tied_engines_by_qid = tied_engines_by_qid or {}
    held_query_ids = held_query_ids or set()
    revised = deepcopy(query_assignments)
    revised_by_id = {qa["query_id"]: qa for qa in revised}
    moves: dict[tuple[str, str], list[str]] = defaultdict(list)
    tie_unresolved_moves: dict[tuple[str, str], int] = defaultdict(int)

    def _refresh_tied_engines(qa: dict, tie_kind: str | None, scores: dict) -> None:
        """Set ``qa["tied_engines"]`` from the verdict just computed."""
        qa["tied_engines"] = _top_scorers(scores) if tie_kind == "tie_unresolved" else []

    def _candidate_pool(original_tied: list[str] | None) -> set[str]:
        """The engines Phase 2 may place this query/group on: the
        originally tied engines that are still committed, FIRST (#503) --
        the full committed set only once none of them survive at all."""
        if not original_tied:
            return committed_engines
        survivors = {e for e in original_tied if e in committed_engines}
        return survivors or committed_engines

    def _place_individual(qid: str, qa: dict) -> None:
        """Place one (ungrouped, or mandatory-stripped) query on its
        highest-scoring committed, eligible engine."""
        current_engine = qa["assigned_engine"]
        original_tied = tied_engines_by_qid.get(qid) if qid in tie_unresolved_qids else None
        eligible_engines = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            _candidate_pool(original_tied),
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        eligible_scores: dict[str, float] = {
            e: score
            for e in eligible_engines
            if (score := _resolver_score(trace_by_query, qid, e)) is not None
        }
        if not eligible_scores:
            return  # nothing eligible at all; leave it where it is (should not happen)
        # A placeholder pick from an unresolved tie earns no stability credit
        # (#503 review item 2): it is not "today's real engine," so it is
        # never passed as a stability candidate here.
        stability_candidates = set() if original_tied else {current_engine}
        winner, tie_kind = _pick_winner(
            eligible_scores, stability_candidates, source_compatible_engine, settled_counts, qid
        )
        tie_relevant = bool(original_tied) or tie_kind == "tie_unresolved"
        if tie_relevant:
            _refresh_tied_engines(qa, tie_kind, eligible_scores)
        if winner == current_engine:
            if tie_relevant:
                qa["assignment_reason"] = _phase2_reason(
                    current_engine,
                    winner,
                    eligible_scores,
                    tie_kind,
                    moved=False,
                    group=False,
                    original_tied=original_tied,
                )
            return
        qa["assigned_engine"] = winner
        qa["assignment_reason"] = _phase2_reason(
            current_engine,
            winner,
            eligible_scores,
            tie_kind,
            moved=True,
            group=False,
            original_tied=original_tied,
        )
        moves[(current_engine, winner)].append(qid)
        if tie_kind == "tie_unresolved":
            tie_unresolved_moves[(current_engine, winner)] += 1

    handled_groups: set[str] = set()
    for qid, payload in trace_by_query.items():
        qa = revised_by_id.get(qid)
        if qa is None or qid in mandatory_query_ids:
            continue  # unknown query, or protected by a mandatory signal override (ADR #296)

        group_info = payload.get("group")
        if group_info:
            group_key = group_info["key"]
            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            members = [
                (mqid, revised_by_id[mqid]["assigned_engine"])
                for mqid, mpayload in trace_by_query.items()
                if (mgroup := mpayload.get("group")) and mgroup["key"] == group_key
                if mqid in revised_by_id and mqid not in mandatory_query_ids
            ]
            if not members:
                continue
            # A tied group's members all carry the SAME tied_engines (the
            # resolver resolves a group's tie once, for every member --
            # ``assignment_resolver``'s Step 6b); use the group's own
            # trigger query's tied set as the group's.
            group_original_tied = (
                tied_engines_by_qid.get(qid) if qid in tie_unresolved_qids else None
            )
            # The same group-aware eligibility rule Phase 1 used to decide
            # this group's current engine was safe to keep or remove (#502
            # review round 5, finding G) -- not a second, Phase-2-only
            # notion of "eligible for the group" that could disagree with
            # Phase 1's.
            eligible_group_engines = _group_eligible_engines(
                qid,
                trace_by_query,
                current_engine_by_qid,
                _candidate_pool(group_original_tied),
                mandatory_query_ids,
                query_signals,
                query_map,
                query_capabilities,
                tied_engines_by_qid,
            )
            eligible = {
                e: s for e, s in group_info["scores"].items() if e in eligible_group_engines
            }
            if not eligible:
                # Should not happen: Phase 1 never removes an engine while
                # one of its co-dependency groups has no eligible survivor
                # for every member (finding G) -- so this group's current
                # engine is always still committed and always eligible for
                # it. Leave the group exactly where it is, the same
                # "should not happen" outcome ``_place_individual`` uses,
                # never split apart to force a placement.
                continue
            # A member's current engine earns stability credit only when it
            # is not itself a placeholder pick from an unresolved tie (#503
            # review items 2 and 3): two different members sitting on two
            # different, still-tied engines is not "the" current engine
            # either way -- ``break_tie`` reports that as unresolved on its
            # own once ``current_engines`` holds more than one tied engine.
            current_engines = {e for mqid, e in members if mqid not in tie_unresolved_qids}
            # Sorted member ids, not ``qid`` (whichever member happens to come
            # first in ``trace_by_query``'s own iteration order) -- the same
            # stable seed the resolver (``assignment_resolver.py``'s Step 6b)
            # and the floor's group tie-break use, so Phase 2 can never
            # re-pick between the same two tied engines just because of input
            # order (#503).
            group_seed = ",".join(sorted(mqid for mqid, _ in members))
            winner, tie_kind = _pick_winner(
                eligible, current_engines, source_compatible_engine, settled_counts, group_seed
            )
            group_tie_relevant = bool(group_original_tied) or tie_kind == "tie_unresolved"
            for member_qid, member_engine in members:
                member_qa = revised_by_id[member_qid]
                if group_tie_relevant:
                    _refresh_tied_engines(member_qa, tie_kind, eligible)
                if member_engine == winner:
                    if group_tie_relevant:
                        member_qa["assignment_reason"] = _phase2_reason(
                            member_engine,
                            winner,
                            eligible,
                            tie_kind,
                            moved=False,
                            group=True,
                            original_tied=group_original_tied,
                        )
                    continue
                member_qa["assigned_engine"] = winner
                member_qa["assignment_reason"] = _phase2_reason(
                    member_engine,
                    winner,
                    eligible,
                    tie_kind,
                    moved=True,
                    group=True,
                    original_tied=group_original_tied,
                )
                moves[(member_engine, winner)].append(member_qid)
                if tie_kind == "tie_unresolved":
                    tie_unresolved_moves[(member_engine, winner)] += 1
            continue

        _place_individual(qid, qa)

    final_counts: dict[str, int] = defaultdict(int)
    for qa in revised:
        final_counts[qa["assigned_engine"]] += 1

    # Queries that stayed on an engine that lost at least one query this
    # phase -- either because no committed engine scores them as well (a
    # capability/ownership gate, or genuinely the best fit) or because they
    # are protected by a mandatory signal override. Reported once per
    # ``from_engine``, the same set on every one of its consolidation
    # records, matching the existing convention for a partial move.
    native_qids_by_engine: dict[str, list[str]] = defaultdict(list)
    for qa in query_assignments:
        native_qids_by_engine[qa["assigned_engine"]].append(qa["query_id"])
    moved_from: dict[str, set[str]] = defaultdict(set)
    for (frm, _to), qids in moves.items():
        moved_from[frm].update(qids)

    consolidations: list[dict[str, Any]] = []
    for (frm, to), qids in sorted(moves.items()):
        is_full = final_counts.get(frm, 0) == 0
        retained_qids = (
            [] if is_full else sorted(set(native_qids_by_engine.get(frm, [])) - moved_from[frm])
        )
        retained_held = [q for q in retained_qids if q in held_query_ids]
        retained_mandatory = [
            q for q in retained_qids if q in mandatory_query_ids and q not in held_query_ids
        ]
        retained_other = [q for q in retained_qids if q not in mandatory_query_ids]
        n_tie_unresolved = tie_unresolved_moves.get((frm, to), 0)
        consolidations.append(
            {
                "from_engine": frm,
                "to_engine": to,
                "query_count": len(qids),
                "reason": (
                    f"{frm} provides no unique capabilities — {len(qids)} queries can be "
                    f"served by {to}"
                    + (f" ({len(retained_qids)} retained)" if retained_qids else "")
                    + (
                        f" ({n_tie_unresolved} of them tied on score, with no routing rule "
                        "to decide)"
                        if n_tie_unresolved
                        else ""
                    )
                ),
                "saved_cost_estimate": 0,
                "action": "full" if is_full else "partial",
                "queries_retained": retained_qids,
                "retention_reason": (
                    "; ".join(
                        r
                        for r, present in (
                            ("Mandatory signal queries stay on their engine", retained_mandatory),
                            (
                                "Corrected queries stay where the correction placed them",
                                retained_held,
                            ),
                            (
                                "No remaining engine scores these queries as well as this "
                                "one does",
                                retained_other,
                            ),
                        )
                        if present
                    )
                    if retained_qids
                    else None
                ),
                "workload_justified": workload_justified.get(frm),
            }
        )

    # Set the saved-cost estimate on the first full-removal record for an
    # engine that ends this run owning nothing at all (matches the existing
    # convention: the figure belongs to the engine being eliminated, counted
    # once, not once per destination it was split across).
    saved_for: dict[str, float] = {}
    for c in consolidations:
        if c["action"] == "full" and c["from_engine"] not in saved_for:
            saved_for[c["from_engine"]] = (
                ENGINE_BASE_COST.get(c["from_engine"], 100) + EXTRA_ENGINE_BURDEN_MONTHLY
            )
            c["saved_cost_estimate"] = saved_for[c["from_engine"]]

    # #502 review round 3: Phase 1 and Phase 2 must agree on eligibility --
    # verified here, not just assumed, since the earlier draft's two
    # different eligibility checks let a query silently stay on an engine
    # Phase 1 had already removed.
    stranded = sorted(
        qa["query_id"] for qa in revised if qa["assigned_engine"] not in committed_engines
    )
    if stranded:
        raise RuntimeError(
            f"reality check: {len(stranded)} queries ended on a removed engine "
            f"(Phase 1/Phase 2 eligibility disagreement): {stranded[:10]}"
        )

    return revised, consolidations


def run_reality_check(
    assignment: dict,
    triage: dict,
    analysis_outputs: dict[str, dict],
    collector_output: dict,
    query_capabilities: dict[str, list[str]] | None = None,
    source_engine: str | None = None,
    held_query_ids: Iterable[str] = (),
) -> dict:
    """Run the CTO-level reality check on the assignment.

    ``source_engine`` is the source database engine (e.g. ``"mysql"``); it picks
    the Aurora engine when both are committed. Defaults to the collector's.

    ``held_query_ids`` (#588) are queries that stay on their current engine:
    they keep that engine committed and are never moved, the same protection
    a signal override gets in Phase 1 and Phase 2, but they do not make the
    engine "mandatory" for the justification floors. Used by
    ``resettle_after_corrections`` for the queries a validation correction
    placed.

    Thinks like a pragmatic CTO: "Every additional database is operational
    burden. Does this engine EARN its place, or can another engine already
    in the stack handle these queries?"

    Returns a dict with:
      - revised_assignments: list of QueryAssignment dicts (possibly modified)
      - consolidations: list of consolidation decisions made
      - architectural_patterns: list of recommended patterns
      - recommendations: list of human-readable recommendations
      - unique_value_assessment: per-engine analysis of unique vs redundant queries
    """
    query_assignments = assignment.get("query_assignments", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in queries}
    signals = triage.get("signals", [])

    # Load query capabilities (hard architectural requirements)
    if query_capabilities is None:
        query_capabilities = triage.get("query_capabilities", {})

    query_signals = _build_query_signals(signals)
    resolved_source_engine = (
        source_database_engine(collector_output) if source_engine is None else source_engine
    )

    # Queries whose FIRST-ASSIGNMENT engine is only a placeholder pick from
    # an unresolved tie (#503): no ADR-031 rule, and no real tiebreak rule
    # either, decided these -- ``QueryAssignment.tied_engines`` is non-empty.
    tie_unresolved_qids: set[str] = {
        qa["query_id"] for qa in query_assignments if qa.get("tied_engines")
    }
    tied_engines_by_qid: dict[str, list[str]] = {
        qa["query_id"]: list(qa["tied_engines"])
        for qa in query_assignments
        if qa.get("tied_engines")
    }

    # Build engine query counts. A tied query counts as native evidence for
    # exactly the ONE engine the first assignment's placeholder already
    # credited it to (``assigned_engine``) -- never for every tied engine,
    # and never for none at all (#503: excluding a tied query from EVERY
    # engine's native set could leave an engine whose only queries were
    # tied uncommitted with no fallback -- crashing the invariant check
    # below when nothing else was eligible either -- or silently drop a
    # tied workload onto a much weaker committed engine when Phase 1 never
    # even evaluated either tied engine). The query still carries
    # ``tied_engines`` and stays visible to the customer either way; this
    # only decides which engine's justification it counts toward in Phase 1.
    engine_queries: dict[str, list[dict]] = defaultdict(list)
    for qa in query_assignments:
        engine_queries[qa["assigned_engine"]].append(qa)

    # Snapshot now, before any pass below mutates ``engine_queries`` (#475
    # review finding 3): each engine's first-assignment workload-justified
    # verdict (``None`` where no floor applies), carried onto every
    # consolidation record for that engine so a full consolidation of one
    # that never earned its place is never presented as a saving.
    workload_justified: dict[str, bool | None] = {
        engine: _workload_ever_justified(engine, list(qas), query_map, resolved_source_engine)
        for engine, qas in engine_queries.items()
    }

    # Build set of queries with mandatory signal overrides (cannot be consolidated)
    mandatory_query_ids: set[str] = {
        qa["query_id"] for qa in query_assignments if _is_signal_override(qa)
    }

    # Engines committed via mandatory signal overrides (e.g., OpenSearch for text_search)
    mandatory_committed_engines: set[str] = set()
    for qa in query_assignments:
        if qa["query_id"] in mandatory_query_ids:
            mandatory_committed_engines.add(qa["assigned_engine"])

    # Queries that never move (#588): the signal overrides above plus any
    # held query. Only the signal overrides make an engine mandatory.
    held = set(held_query_ids)
    fixed_query_ids = mandatory_query_ids | held

    # Re-run the resolver once, with a trace attached, so Phase 1 and Phase
    # 2 read the IDENTICAL per-query scores (#502 review: Phase 1 judging
    # engines by the reality check's own, separate scorer, rather than
    # this same trace, is exactly what produced results the scores did not
    # support). Pure and deterministic on the exact artifacts that produced
    # ``query_assignments`` -- see ``tests/e2e/routing_snapshot.py`` for the
    # same technique and its own consistency guard.
    from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
    from src.agents.referee.query_scoring import ScoringContext

    _trace = RoutingTrace()
    AssignmentResolver().resolve(triage, analysis_outputs, collector_output, routing_trace=_trace)
    trace_by_query = _trace.by_query
    # The one shared scoring context (#490/#549): the mandatory-engine
    # floor's absorber (``_find_best_absorber_for_query``) scores each
    # candidate engine directly with this (``score_query_for_engine``)
    # instead of reading ``trace_by_query`` -- one scoring call site, same
    # as ``routed_confidence.py``, rather than a second way of reaching the
    # identical number.
    scoring_ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector_output)
    # The resolver's OWN settled-workload snapshot (#503), persisted on the
    # trace rather than recomputed here from
    # the final v1 assignment: the resolver's snapshot is taken before the
    # Aurora fallback, the utility pin and signal overrides fill in more
    # queries, so recomputing it here from the fuller v1 picture could make
    # an otherwise-unchanged tie re-pick between the same two engines for no
    # real reason. Falls back to a v1-based recompute only for an assignment
    # produced before this was persisted (``_trace.settled_counts`` empty
    # with no tie to explain it).
    settled_counts: dict[str, int] = _trace.settled_counts or dict(
        Counter(
            qa["assigned_engine"]
            for qa in query_assignments
            if qa["query_id"] not in tie_unresolved_qids and qa.get("assigned_engine")
        )
    )
    # Each query's engine as it stands before either phase runs -- stable
    # throughout both, since neither phase mutates ``query_assignments``
    # itself (Phase 2 works on a ``deepcopy``). Shared by both phases'
    # group-aware eligibility checks (``_group_eligible_engines``, #502
    # review round 5, finding G) so a co-dependency group's members are
    # always looked up by the SAME current engine, even when they are
    # native to a different engine than the one being evaluated for removal.
    current_engine_by_qid = {qa["query_id"]: qa["assigned_engine"] for qa in query_assignments}

    # -------------------------------------------------------------------
    # Phase 1 (#502 review): settle which engines earn a place, judged on
    # each engine's own native queries alone, order-independent, repeated
    # to a fixed point. No engine is protected for being cheapest or
    # "primary" -- the only unconditional protections are a mandatory
    # signal override (ADR #296) and a query (or co-dependency group) no
    # other committed engine can serve at all, both handled inside
    # ``_evaluate_engine_removal`` itself.
    # -------------------------------------------------------------------
    max_rounds = len(engine_queries) + 2
    # Computed here, before Phase 1, so Phase 1's own destination tie-break
    # (``_removal_tiebreak_key``, #569) resolves a tie the same ADR-031
    # Decision 2 way Phase 2 and the absorber already do -- source-compatible
    # engine first, never left to pick whichever alternate a Python set
    # happened to iterate first.
    source_compatible_engine = SOURCE_ENGINE_TO_AURORA.get(resolved_source_engine)
    committed_engines, unique_value_assessment = _settle_committed_engines(
        engine_queries,
        fixed_query_ids,
        mandatory_committed_engines,
        trace_by_query,
        current_engine_by_qid,
        query_signals,
        query_map,
        query_capabilities,
        max_rounds,
        tied_engines_by_qid,
        source_compatible_engine,
        settled_counts,
    )

    # -------------------------------------------------------------------
    # Phase 2 (#502 review): place every query on its highest-scoring
    # engine within the settled set, using the SAME resolver scores and
    # the SAME eligibility rule Phase 1 used (ADR-031) -- never a query
    # ends on an engine Phase 1 removed, because placement happens once,
    # fresh, against the final set, not incrementally against a list that
    # can go stale.
    # -------------------------------------------------------------------
    revised, consolidations = _place_settled_queries(
        query_assignments,
        committed_engines,
        fixed_query_ids,
        current_engine_by_qid,
        query_signals,
        query_map,
        query_capabilities,
        trace_by_query,
        source_compatible_engine,
        workload_justified,
        tie_unresolved_qids,
        settled_counts,
        tied_engines_by_qid,
        held_query_ids=held,
    )
    lightweight_recommendations: list[dict] = []

    # -------------------------------------------------------------------
    # Mandatory-engine justification floor (#326, #167)
    #
    # A signal override makes an engine "mandatory" through Phase 1 and
    # Phase 2 above, which never move its queries -- that is how a $150+/mo
    # OpenSearch domain kept 4 LIKE/prefix admin queries at under 1 call/s in
    # a maintainer's run. This is the last gate:
    # a mandatory engine that still owns only its mandatory queries, and does
    # not meet its justification floor, is not exempt forever. OpenSearch
    # gets the dedicated floor from #326 (traffic, search depth, native
    # source support); any other mandatory engine is judged on cost share
    # alone (#167).
    #
    # OpenSearch's own floor runs whenever it is committed at all, not only
    # when a signal override made it "mandatory" (#475 review, finding 4
    # follow-up, measured on wordpress): once OpenSearch's own matched-pattern
    # score (finding 4's fix) lets it win ownership through Pass 0/2's
    # score-based consolidation alone -- no signal override involved -- this
    # loop would otherwise never run for it at all, reopening the exact gap
    # #475 closed by keeping that score low in the first place. A query that
    # reaches OpenSearch on fit score alone must still clear the same #326
    # bar as one that reaches it through the signal override; every other
    # mandatory engine's cost-share floor (#167) is unaffected and still only
    # runs for an actual signal override.
    # -------------------------------------------------------------------
    floor_scope = mandatory_committed_engines | ({"opensearch"} & committed_engines)
    for engine in sorted(floor_scope & committed_engines):
        # Grouped by actual CURRENT ownership, not by any one signal (#475
        # review, suggestion 1) -- deliberately different from the
        # resolver's own ``opensearch_justification`` call
        # (``assignment_resolver._build_signal_overrides``), which groups by
        # the signal's own candidate queries before any assignment exists.
        # This floor asks a later question: does the engine's workload AS IT
        # STANDS NOW (after absorption may have added or Pass 0/2 may have
        # removed queries) still earn its place -- so it has to look at
        # everything the engine currently owns, not just how it first got
        # there.
        current_qas = [qa for qa in revised if qa["assigned_engine"] == engine]
        if not current_qas:
            continue

        # verdicts: per-query (justified, reason) for opensearch (finding B1 --
        # one deep query must not be sunk by a shallow one sitting next to it,
        # and vice versa); a single engine-wide (justified, reason) for any
        # other mandatory engine, applied to every one of its queries.
        if engine == "opensearch":
            verdicts = opensearch_justification(current_qas, query_map, resolved_source_engine)
        else:
            cost_reason = _mandatory_cost_share_reason(engine, current_qas, len(query_assignments))
            verdicts = {
                qa["query_id"]: (cost_reason is None, cost_reason or "") for qa in current_qas
            }
        # A held query (#588) stays where it is even when it fails the floor.
        to_move_qas = [
            qa
            for qa in current_qas
            if not verdicts[qa["query_id"]][0] and qa["query_id"] not in held
        ]
        if not to_move_qas:
            continue

        floor_candidates = {e for e in committed_engines if e != engine}
        if not floor_candidates:
            continue  # nowhere to send them (#335): leave the engine as owner

        floor_placement: dict[str, list[tuple[dict, str]]] = defaultdict(list)
        floor_unplaced: list[dict] = []
        to_move_by_id = {qa["query_id"]: qa for qa in to_move_qas}
        handled_floor_groups: set[str] = set()
        for qa in to_move_qas:
            qid = qa["query_id"]
            group_info = trace_by_query.get(qid, {}).get("group")
            if group_info:
                # #502 review round 6, finding 3: route a grouped query
                # through ``_group_eligible_engines`` too, so a
                # co-dependency group fails the floor and moves as ONE
                # unit here, the same rule Phase 1 and Phase 2 already
                # apply -- never split by picking each member's own best
                # absorber independently.
                group_key = group_info["key"]
                if group_key in handled_floor_groups:
                    continue
                handled_floor_groups.add(group_key)
                members = [
                    mqid
                    for mqid, mpayload in trace_by_query.items()
                    if (mg := mpayload.get("group")) and mg["key"] == group_key
                ]
                member_qas = [to_move_by_id[m] for m in members if m in to_move_by_id]
                if len(member_qas) != len(members):
                    # Not every member of this group is failing the floor
                    # on ``engine`` right now -- moving only part of a
                    # group is never allowed, so leave the whole group
                    # where it is rather than split it.
                    floor_unplaced.extend(member_qas)
                    continue
                eligible = _group_eligible_engines(
                    qid,
                    trace_by_query,
                    current_engine_by_qid,
                    floor_candidates,
                    fixed_query_ids,
                    query_signals,
                    query_map,
                    query_capabilities,
                )
                if not eligible:
                    floor_unplaced.extend(member_qas)
                    continue

                group_scores = {
                    e: group_info["scores"].get(e, _resolver_score(trace_by_query, qid, e)) or 0
                    for e in eligible
                }
                top_group_score = max(group_scores.values())
                tied_group = sorted(e for e, s in group_scores.items() if s == top_group_score)
                tie_note = ""
                tie_kind: str | None = None
                if len(tied_group) == 1:
                    winner = tied_group[0]
                else:
                    # No ADR-031 rule prefers one engine over another by name
                    # or by how many queries it already has (#503): the same
                    # ``break_tie`` the resolver and ``_pick_winner`` use.
                    # Sorted member ids (#503): a stable seed, not whichever
                    # member happens to come first in ``trace_by_query``'s
                    # own iteration order.
                    winner, tie_kind = break_tie(
                        tied_group,
                        set(),
                        source_compatible_engine,
                        settled_counts,
                        ",".join(sorted(members)),
                    )
                    tie_note = (
                        _tie_unresolved_note(tied_group, winner)
                        if tie_kind == "tie_unresolved"
                        else " (tie: source-compatible engine)"
                    )
                winner_fit = round(group_scores[winner])
                reason = f"fit={winner_fit}, co-dependency group moves together{tie_note}"
                # #503: a tie this floor pass resolves (or decides for real)
                # is recorded, same as Phase 2.
                for member_qa in member_qas:
                    member_qa["tied_engines"] = tied_group if tie_kind == "tie_unresolved" else []
                    floor_placement[winner].append((member_qa, reason))
                continue

            absorber = _find_best_absorber_for_query(
                qa,
                floor_candidates,
                engine,
                query_signals,
                query_map,
                engine_queries,
                set(),
                "",
                scoring_ctx,
                query_capabilities,
                source_compatible_engine,
                settled_counts,
            )
            if absorber is None:
                floor_unplaced.append(qa)
                continue
            qa["tied_engines"] = absorber.get("tied_engines", [])
            floor_placement[absorber["target_engine"]].append((qa, absorber["reason"]))

        moved_ids = {qa["query_id"] for placed in floor_placement.values() for qa, _ in placed}
        if not moved_ids:
            # #502 review round 6, finding 2: every query that failed the
            # floor also had no eligible destination (``floor_unplaced``
            # covers all of ``to_move_qas``) -- record WHY the engine is
            # kept rather than silently leaving it unchanged with no trace
            # of this gate having run at all.
            if floor_unplaced:
                assessment = unique_value_assessment.setdefault(engine, {})
                assessment["floor_unplaced_queries"] = sorted(
                    qa["query_id"] for qa in floor_unplaced
                )
                n = len(floor_unplaced)
                assessment["floor_unplaced_reason"] = (
                    f"kept: no eligible engine can serve {n} "
                    f"quer{'y' if n == 1 else 'ies'} that failed "
                    f"{display_engine(engine)}'s justification floor"
                )
            continue

        for target_engine, placed in floor_placement.items():
            for qa, fit_reason in placed:
                _, this_reason = verdicts[qa["query_id"]]
                for rqa in revised:
                    if rqa["query_id"] == qa["query_id"]:
                        rqa["assigned_engine"] = target_engine
                        rqa["assignment_reason"] = (
                            f"reality check: consolidated from {display_engine(engine)} → "
                            f"{display_engine(target_engine)} (justification floor "
                            f"— {this_reason}; {fit_reason})"
                        )
                        rqa.pop("signal_override", None)

        is_full = len(moved_ids) == len(current_qas)
        # One combined reason for the consolidation record: the distinct
        # per-query reasons among the queries this pass actually moved, not
        # every reason seen across the whole (possibly larger, partly kept)
        # engine -- a query that stayed never contributes text here.
        moved_reasons = sorted(
            {verdicts[qa["query_id"]][1] for qa in to_move_qas if qa["query_id"] in moved_ids}
        )
        floor_reason = "; ".join(moved_reasons)
        floor_entry_reason = (
            f"{display_engine(engine)} does not meet its justification floor — {floor_reason}"
        )
        for i, (target_engine, placed) in enumerate(floor_placement.items()):
            # moved_queries() (reality_check_request.py) recomputes membership
            # from (from_engine, to_engine) alone, so two consolidation records
            # for the same pair would double-count the same queries. Merge into
            # an existing record for this pair instead of adding a second one
            # -- but only append this pass's own reason, scoped to the queries
            # it actually moved, never re-describing a record (or a share of
            # one) this pass had no part in (review of #375, finding 5).
            existing = next(
                (
                    c
                    for c in consolidations
                    if c["from_engine"] == engine and c["to_engine"] == target_engine
                ),
                None,
            )
            if existing is not None:
                existing["query_count"] += len(placed)
                existing["reason"] = (
                    f"{existing['reason']} | {_queries(len(placed))} of these also move "
                    f"because {floor_entry_reason}"
                )
                if is_full and i == 0:
                    existing["saved_cost_estimate"] = existing.get("saved_cost_estimate", 0) + (
                        ENGINE_BASE_COST.get(engine, 0) + EXTRA_ENGINE_BURDEN_MONTHLY
                    )
                continue
            consolidations.append(
                {
                    "from_engine": engine,
                    "to_engine": target_engine,
                    "query_count": len(placed),
                    "reason": floor_entry_reason,
                    "saved_cost_estimate": (
                        (ENGINE_BASE_COST.get(engine, 0) + EXTRA_ENGINE_BURDEN_MONTHLY)
                        if is_full and i == 0
                        else 0
                    ),
                    "action": "full" if is_full else "partial",
                    "queries_retained": (
                        [qa["query_id"] for qa in floor_unplaced] if floor_unplaced else []
                    ),
                    "retention_reason": (
                        "Queries require capabilities no other committed engine provides"
                        if floor_unplaced
                        else None
                    ),
                    "workload_justified": workload_justified.get(engine),
                }
            )
        assessment = unique_value_assessment.setdefault(engine, {})
        assessment["cost_justification"] = floor_reason
        assessment["cost_justification_query_ids"] = sorted(moved_ids)
        if is_full:
            committed_engines.discard(engine)
            engine_queries[engine] = [
                qa for qa in engine_queries.get(engine, []) if qa["query_id"] not in moved_ids
            ]

    # Reconcile before Pass 3/4 (#475 review follow-up): this floor now runs
    # for OpenSearch whenever it is committed, not only when it is
    # "mandatory" (see above), so a query Pass 2 just placed on OpenSearch
    # can be moved AGAIN by this same floor within this one call -- a
    # two-hop move (e.g. aurora_mysql -> opensearch -> dynamodb) that no
    # single consolidation record describes, which left a stale, orphaned
    # "aurora_mysql -> opensearch" record ``moved_queries()``
    # (``reality_check_request.py``) could never resolve back to any
    # query (0 found for its claimed count). ``reconcile_consolidations``
    # is designed for exactly this (Pass 2, Pass 1 and later steps all
    # moving queries and editing records on their own, #218) -- it already
    # ran once, later, via the handler's own ``_settle_records``, but only
    # for the ``none``/``bedrock`` llm_mode paths that reach it; ``external``
    # mode writes its LLM request from this function's own return value
    # first, so it never saw settled records before. Running it here as
    # well is idempotent (reconciling an already-reconciled list is a
    # no-op) and makes every mode see the same settled records.
    consolidations = reconcile_consolidations(
        query_assignments,
        revised,
        consolidations,
        unique_value_assessment,
    )

    # Pass 3: Detect architectural patterns
    patterns = _detect_architectural_patterns(revised, committed_engines, engine_queries)

    # Pass 4: Build recommendations
    recommendations = _build_recommendations(
        revised,
        consolidations,
        patterns,
        engine_queries,
        _engine_infra_cost(analysis_outputs),
        unique_value_assessment,
    )

    # #502 review: Phase 1+2 above replace the old Aurora-absorption pass
    # (a small engine Aurora can serve is just one case the general
    # settle-then-place design already covers) -- this key stays, reporting
    # the same facts in the general case rather than fabricating an
    # Aurora-specific mechanism that no longer runs.
    final_counts: dict[str, int] = defaultdict(int)
    for qa in revised:
        final_counts[qa["assigned_engine"]] += 1
    source_aurora_engine = SOURCE_ENGINE_TO_AURORA.get(resolved_source_engine)
    aurora_engine_name = (
        source_aurora_engine
        if source_aurora_engine in committed_engines
        else next((e for e in AURORA_ENGINES if e in committed_engines), "")
    )
    return {
        "revised_assignments": revised,
        "consolidations": consolidations,
        "architectural_patterns": patterns,
        "recommendations": recommendations,
        "unique_value_assessment": unique_value_assessment,
        "lightweight_recommendations": lightweight_recommendations,
        "aurora_absorption": {
            "aurora_engine": aurora_engine_name,
            "engines_eliminated": sorted(set(engine_queries) - committed_engines),
            "engines_reduced": sorted(
                e
                for e in committed_engines
                if final_counts.get(e, 0) < len(engine_queries.get(e, []))
            ),
            "absorbed_count": sum(
                c["query_count"] for c in consolidations if c["to_engine"] == aurora_engine_name
            ),
        },
    }


def resettle_after_corrections(
    revised_assignments: list[dict],
    triage: dict,
    analysis_outputs: dict[str, dict],
    collector_output: dict,
    source_engine: str | None = None,
    held_query_ids: Iterable[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Settle the engines again after validation corrections (#166, #588).

    A correction can bring back an engine the reality check removed, so the
    committed set it settled against has changed. This runs the same review
    again on the corrected assignment (``run_reality_check``: Phase 1 settle,
    Phase 2 placement and the justification floors), so an engine is removed
    when the committed engines can serve its queries, and each moved query
    goes to its highest-scoring remaining engine, by the same per-query
    scores and eligibility gate as the first run (ADR-031 Decision 7). It
    replaces the Aurora-only absorption pass and its own thresholds.

    ``held_query_ids`` are the corrected queries: they stay where the
    correction put them, so the engine a correction restored is not removed
    again. The customer's own picks (``customer_override``) are handled the
    way the first run handles them (``run_reality_check_deterministic``):
    left out of the review and put back unchanged afterwards, so a pick
    never commits an engine that other queries could then move onto.
    ``source_engine`` defaults to the collector's source database engine.

    Note this can move queries that were not corrected onto the engine a
    correction brought back, when it scores them higher.

    Returns (updated_assignments, new_consolidations). Assignments are copied.
    """
    candidates = [qa for qa in revised_assignments if not qa.get("customer_override")]
    result = run_reality_check(
        {"query_assignments": deepcopy(candidates)},
        triage,
        analysis_outputs,
        collector_output,
        source_engine=source_engine,
        held_query_ids=held_query_ids,
    )
    revised_by_id = {qa["query_id"]: qa for qa in result["revised_assignments"]}
    updated = [revised_by_id.get(qa["query_id"]) or deepcopy(qa) for qa in revised_assignments]
    return updated, result["consolidations"]


def _build_query_signals(signals: list[dict]) -> dict[str, list[str]]:
    """Map query_id → triage signal names."""
    query_signals: dict[str, list[str]] = defaultdict(list)
    for signal in signals:
        for qid in signal.get("query_ids", []):
            query_signals[qid].append(signal.get("signal", ""))
    return query_signals


def _is_signal_override(qa: dict) -> bool:
    """True when a triage signal forced this query to its engine (#151).

    Assignments written before ``signal_override`` existed carry the signal
    only in ``assignment_reason``; fall back to that for legacy artifacts.
    """
    if "signal_override" in qa:
        return bool(qa["signal_override"])
    return str(qa.get("assignment_reason", "")).startswith("signal override")


def _find_best_absorber_for_query(
    qa: dict,
    committed_engines: set[str],
    source_engine: str,
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    engine_queries: dict[str, list[dict]],
    mandatory_committed_engines: set[str],
    primary_engine: str,
    ctx: ScoringContext,
    query_capabilities: dict[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
) -> dict | None:
    """Find the best committed engine to absorb a single query.

    Ranks by: the resolver's own score (highest wins,
    ``query_scoring.score_query_for_engine`` against the one shared ``ctx``
    -- #502 review round 5, finding N3: the mandatory-engine justification
    floor, the only caller of this function, used the reality check's own
    ``_engine_fit_score`` here, a second scorer disagreeing with the one
    Phase 1 and Phase 2 already use; #490/#549 then removed every other
    separate scorer in this module too, so every reality-check decision now
    reads the one shared score), with mandatory secondary
    engines getting a tiebreaker bonus (data already flowing via sync -- a
    real operational reason, not a preference of convenience).

    A tie that survives even that -- same fit score, same mandatory-
    secondary status -- is never decided by table overlap, how many
    queries the engine already has, or its name (#503): it goes through
    ``break_tie``, the same ADR-031 Decision 2 tie-break the resolver's
    ``break_owner_tie`` and this module's ``_pick_winner`` use, so the
    source-compatible engine wins first and anything still tied is
    reported as ``tie_unresolved`` in the returned reason, not silently
    picked.

    Respects the serviceability gate (``query_scoring.score_query_for_engine``'s
    own hard capability gate against ``ctx.query_capabilities``): only
    considers engines that can serve the query's hard requirements.

    No score floor (#502 review): a query being consolidated moves to its
    highest-scoring remaining engine that can serve it, even if that scores
    it a little lower than the engine it is leaving -- accepting a slightly
    worse fit in exchange for fewer engines is the point of this CTO review.
    """
    from src.agents.referee.query_scoring import score_query_for_engine

    qid = qa["query_id"]
    query = query_map.get(qid, qa)
    query_tables = query_map.get(qid, {}).get("tables_accessed", [])
    candidates: list[dict] = []

    for target_engine in sorted(committed_engines):
        if target_engine == source_engine:
            continue

        # Write gate (#296): a cache never owns a query, and an engine that is not
        # a system of record never owns a write; a specialist (OpenSearch) absorbs
        # only the queries its specialty serves
        if not may_absorb(target_engine, qid, query, query_signals, query_capabilities):
            continue

        # Review of #375, finding B2: a join consolidated away from a
        # non-Aurora engine (DocumentDB, OpenSearch) never lands on DynamoDB,
        # unless a denormalised design is explicitly proposed for it -- no
        # such proposal mechanism exists yet, so this is unconditional for
        # now. A join that was already on DynamoDB, or arrived there from
        # Aurora (a key-scoped 2-table join a v1 fit score chose on its own
        # merits), is unaffected; this only blocks a *consolidation* onto it.
        if (
            target_engine == "dynamodb"
            and source_engine not in AURORA_ENGINES
            and _query_has_join(query_map.get(qid, {}))
        ):
            continue

        # #502 review round 5, finding N3 / #490/#549: ranked by the
        # resolver's own score, not a second, separate scorer -- ``None``
        # means ``score_query_for_engine``'s own hard exclusion/capability
        # gate already ruled this engine out for this query (``.gate_kind``
        # names which, if a caller needs it), so there is nothing to absorb
        # it with.
        #
        # #490 review (round 2): this used to also drop a candidate scoring
        # exactly 0 unless the reality check's own, now-deleted
        # ``_can_engine_serve_query`` passed -- reachable (not dead code: a
        # round-1 review response claimed otherwise and was corrected) via
        # that function's *signal-gated* branch, which returned ``False``
        # outright whenever a triage signal demanded a capability the old
        # ``ENGINE_CAPABILITIES`` table didn't list for the candidate,
        # skipping its own basic-CRUD fallback entirely. That second check
        # is not restored, for each capability it could gate on:
        #   - ``complex_joins``: still excluded -- ``score_query_for_engine``
        #     gates it the same way today, through the real per-query
        #     ``complex_joins`` capability (``capability_registry``), not a
        #     private table.
        #   - ``referential_integrity``, ``transactions`` (dynamodb/
        #     documentdb), ``text_search`` (aurora, via ``inverted_index``):
        #     no longer excluded. None is a structural impossibility
        #     (ADR-031 Decision 2 Step 2); ``transactions`` hard-excluding
        #     DynamoDB was itself an ADR-031 Decision 3 violation in the old
        #     code (DynamoDB's transaction support is scored by shape, never
        #     excluded). Pinned by ``test_resolver_reality_check_agreement.
        #     py``'s ``TestAbsorptionBehaviorChanges`` for every case above,
        #     plus the general "a 0 that clears the real gate is a
        #     candidate, not an exclusion" rule.
        result = score_query_for_engine(query, target_engine, ctx)
        if result.score is None:
            continue
        fit = round(result.score)

        # Check table overlap with target
        target_tables = set()
        for tqa in engine_queries.get(target_engine, []):
            target_tables.update(query_map.get(tqa["query_id"], {}).get("tables_accessed", []))
        overlap = set(query_tables) & target_tables

        is_mandatory_secondary = (
            target_engine in mandatory_committed_engines and target_engine != primary_engine
        )

        reason = (
            f"fit={fit}, data synced for {', '.join(sorted(overlap))}"
            if overlap
            else f"fit={fit}, {target_engine} can serve this pattern"
        )

        candidates.append(
            {
                "target_engine": target_engine,
                "fit_score": fit,
                "is_mandatory_secondary": is_mandatory_secondary,
                "reason": reason,
            }
        )

    if not candidates:
        return None

    # Highest fit score wins, mandatory-secondary next (data already
    # synced, a real reason -- #502 review round 5, finding N3). A tie that
    # survives both is an ADR-031 Decision 2 tie (#503): resolved by
    # ``break_tie``, never by table overlap, how many queries the engine
    # already has, or its name.
    def _rank(c: dict) -> tuple[int, bool]:
        return (-c["fit_score"], not c["is_mandatory_secondary"])

    best_rank = min(_rank(c) for c in candidates)
    tied = [c for c in candidates if _rank(c) == best_rank]
    if len(tied) == 1:
        return {**tied[0], "tied_engines": []}

    tied_engine_names = [c["target_engine"] for c in tied]
    winner_engine, tie_kind = break_tie(
        tied_engine_names, set(), source_compatible_engine, settled_counts, qid
    )
    winner = next(c for c in tied if c["target_engine"] == winner_engine)
    tie_note = (
        _tie_unresolved_note(tied_engine_names, winner_engine)
        if tie_kind == "tie_unresolved"
        else " (tie: source-compatible engine)"
    )
    # #503: a tie the absorber itself resolves (or decides for real) is
    # recorded, same as Phase 1/2.
    return {
        **winner,
        "reason": f"{winner['reason']}{tie_note}",
        "tied_engines": sorted(tied_engine_names) if tie_kind == "tie_unresolved" else [],
    }


def _suggest_lightweight_for_orphans(
    unserviceable_qas: list[dict],
    total_movable: int,
    query_capabilities: dict[str, list[str]],
    source_engine: str,
) -> dict | None:
    """Suggest a lightweight alternative if orphan set is small and uniform.

    Returns a dict suitable for LightweightRecommendation if:
    - Orphan count is <= 10% of total movable queries
    - All orphans share a single common capability requirement
    Otherwise returns None (keep the full engine).
    """
    if not unserviceable_qas or total_movable == 0:
        return None

    orphan_ratio = len(unserviceable_qas) / total_movable
    if orphan_ratio > 0.10:
        return None

    # Check if all orphans share a single capability
    all_caps: set[str] = set()
    for qa in unserviceable_qas:
        caps = query_capabilities.get(qa["query_id"], [])
        all_caps.update(caps)

    if len(all_caps) != 1:
        return None

    shared_cap = next(iter(all_caps))
    alt = suggest_lightweight_alternative(shared_cap)
    if not alt:
        return None

    return {
        "capability": shared_cap,
        "service": alt["service"],
        "query_ids": [qa["query_id"] for qa in unserviceable_qas],
        "pattern": alt["pattern"],
        "cost_profile": alt["cost_profile"],
        "replaces_engine": source_engine,
        "limitations": alt["limitations"],
    }


def _detect_architectural_patterns(
    assignments: list[dict],
    committed_engines: set[str],
    engine_queries: dict[str, list[dict]],
) -> list[dict]:
    """Detect which architectural patterns apply to this multi-engine setup."""
    patterns: list[dict] = []

    # Get engines that actually have queries after consolidation
    active_engines: dict[str, int] = defaultdict(int)
    for qa in assignments:
        active_engines[qa["assigned_engine"]] += 1

    if len(active_engines) < 2:
        return patterns

    # Find the primary engine (most queries)
    primary = max(active_engines, key=lambda e: active_engines[e])
    secondaries = [e for e in active_engines if e != primary and active_engines[e] > 0]

    # Check for CQRS pattern
    # Primary handles writes, secondary handles reads/search
    primary_has_writes = any(qa["assigned_engine"] == primary for qa in assignments)

    read_secondaries = [e for e in secondaries if e in ("opensearch", "elasticache", "dynamodb")]
    if primary_has_writes and read_secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["CQRS"])
        pattern["applies_to"] = {
            "write_engine": primary,
            "read_engines": read_secondaries,
        }
        patterns.append(pattern)

    # Check for Materialized View pattern
    if "opensearch" in secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["MATERIALIZED_VIEW"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "view_engine": "opensearch",
        }
        patterns.append(pattern)

    # If 3+ engines, suggest Polyglot Persistence
    if len(active_engines) >= 3:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["POLYGLOT_PERSISTENCE"])
        pattern["applies_to"] = {
            "engines": list(active_engines.keys()),
        }
        patterns.append(pattern)
    elif len(active_engines) == 2 and primary != "dynamodb":
        # Event sourcing when using non-serverless primary
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["EVENT_SOURCING"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "target_engines": secondaries,
        }
        patterns.append(pattern)

    return patterns


def patterns_for_assignment(query_assignments: list[dict]) -> list[dict]:
    """Architectural patterns for the engines that carry in-scope queries."""
    in_scope = [
        qa for qa in query_assignments if qa.get("in_scope", True) and qa.get("assigned_engine")
    ]
    return _detect_architectural_patterns(in_scope, set(), {})


def refresh_patterns_and_recommendations(result: dict) -> None:
    """Recompute ``architectural_patterns`` and ``recommendations`` from the final assignment.

    Patterns are first detected before the LLM corrections, the post-correction re-settle,
    the sanity sweep and the customer-override restore, any of which can still move
    queries; the output must describe the assignment it ships with, so no pattern names
    an engine that ends up with no in-scope query (#202). Only in-scope queries count.
    Mutates ``result``.
    """
    patterns = patterns_for_assignment(result["revised_assignments"])
    result["architectural_patterns"] = patterns
    result["recommendations"] = _build_recommendations(
        result["revised_assignments"],
        result["consolidations"],
        patterns,
        {},
        _engine_infra_cost(result.get("analysis_outputs")),
        result.get("unique_value_assessment"),
    )


def _queries(n: int, engine: str = "") -> str:
    """``"1 query"`` / ``"3 queries"`` (``"3 documentdb queries"`` with ``engine``).

    Local on purpose: the report layer's ``plural_noun`` sits above the agents, and
    importing it here would invert the layering.
    """
    noun = "query" if n == 1 else "queries"
    return f"{n} {engine} {noun}" if engine else f"{n} {noun}"


def reconcile_consolidations(
    before_assignments: list[dict],
    after_assignments: list[dict],
    consolidations: list[dict],
    unique_value_assessment: dict[str, dict] | None = None,
) -> list[dict]:
    """Make the consolidation records describe the net per-query moves (#218).

    Pass 2, the LLM corrections, the post-correction re-settle, the sanity sweep and
    the customer-override restore each move queries and edit records on their own,
    so the records drifted from the assignment (unrecorded moves, overstated counts,
    savings for an engine that stays). This derives the moves from the before/after
    assignment and fits the records to them:

    - one record per (from, to) pair with a net move; its ``query_count`` is that move,
      so applying the records to the before distribution gives the after distribution;
    - a move with no record gets one; a record with no net move is dropped;
    - ``action`` is ``full`` only when the source engine had an in-scope query in the
      input and ends with none, and only then are savings claimed, once per engine;
    - a record whose count, action or retained queries changed gets a reason that
      states the final move.

    ``unique_value_assessment`` (optional) carries each engine's
    ``cost_justification`` (#326, #167) and ``cost_justification_query_ids``:
    the justification-floor reason, and exactly which queries the floor
    itself moved, which this pass would otherwise discard whenever it
    rewrites a reason to describe the final move. The reason is appended
    only to a (src, dst) record whose own net-moved queries overlap that id
    set -- a record for a different move the floor never touched (a
    different target engine, or queries that moved for an unrelated reason)
    does not get the floor's text (a review of #375 found exactly this: the
    floor's reason attached to a 119-query record and to a 29-query record
    neither of which the floor had any part in).

    Record order is kept; new records follow. Inputs are not mutated.
    """
    before = {qa["query_id"]: qa["assigned_engine"] for qa in before_assignments}
    after_engine = {qa["query_id"]: qa["assigned_engine"] for qa in after_assignments}
    # The single rule ("no in-scope query remains") lives in one place
    # (src.shared.engine_elimination, #235); ``had_in_scope`` and ``remaining``
    # are its two halves, kept as separate names here because ``remaining``'s
    # per-engine counts are also used below for the "stays for N queries" text.
    had_in_scope = set(in_scope_distribution(before_assignments))
    remaining: dict[str, int] = defaultdict(int, in_scope_distribution(after_assignments))

    moves: dict[tuple[str, str], int] = {}
    moved_qids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for qa in after_assignments:
        src = before.get(qa["query_id"])
        dst = qa["assigned_engine"]
        if src and src != dst:
            moves[(src, dst)] = moves.get((src, dst), 0) + 1
            moved_qids[(src, dst)].add(qa["query_id"])

    recorded_saving: dict[str, float] = {}
    first_record: dict[tuple[str, str], dict] = {}
    # Engine-level ``workload_justified`` verdict (#475 review round 2,
    # finding A), read off whichever input record for that ``from_engine``
    # happens to carry it -- every record a creation site builds for the
    # same engine already carries the identical, once-computed verdict
    # (``_workload_ever_justified``), so the first one found is as good as
    # any. ``None`` (no entry at all) stays the default below: not every
    # engine has a floor to judge it against.
    workload_justified_by_engine: dict[str, bool | None] = {}
    for c in consolidations:
        src = c["from_engine"]
        recorded_saving[src] = max(recorded_saving.get(src, 0), c.get("saved_cost_estimate") or 0)
        first_record.setdefault((src, c["to_engine"]), c)
        if src not in workload_justified_by_engine and "workload_justified" in c:
            workload_justified_by_engine[src] = c["workload_justified"]
    ordered = [p for p in first_record if p in moves] + [p for p in moves if p not in first_record]

    reconciled: list[dict] = []
    savings_claimed: set[str] = set()
    for src, dst in ordered:
        count = moves[(src, dst)]
        full = src in had_in_scope and remaining.get(src, 0) == 0
        action = "full" if full else "partial"
        record = dict(
            first_record.get((src, dst))
            or {"from_engine": src, "to_engine": dst, "queries_retained": []}
        )
        prior_retained = list(record.get("queries_retained") or [])
        retained = [] if full else [q for q in prior_retained if after_engine.get(q) == src]
        if (
            record.get("query_count") != count
            or record.get("action", "full") != action
            or retained != prior_retained
            or not record.get("reason")
        ):
            moved = f"{_queries(count, src)} moved to {dst}"
            if full:
                record["reason"] = f"{moved}; no in-scope {src} query remains"
            elif src not in had_in_scope:
                record["reason"] = f"Partial consolidation: {moved}; {src} had no in-scope query"
            else:
                record["reason"] = (
                    f"Partial consolidation: {moved}; {src} stays for {_queries(remaining[src])}"
                )
            engine_assessment = (unique_value_assessment or {}).get(src, {})
            justification = engine_assessment.get("cost_justification")
            justification_qids = engine_assessment.get("cost_justification_query_ids")
            floor_overlap = (
                moved_qids[(src, dst)]
                if justification_qids is None
                else moved_qids[(src, dst)] & set(justification_qids)
            )
            if justification and floor_overlap:
                # Say how many of this record's own queries the floor actually
                # moved, not just the floor's reason text on its own -- a review
                # of #375 found the wording reading as if the floor's reason
                # applied to every query in the record (e.g. "119 ... moved to
                # aurora_postgresql ... (no search predicate ...)"), when the
                # floor itself only moved a handful of those 119.
                record["reason"] = (
                    f"{record['reason']} ({_queries(len(floor_overlap))} of these, "
                    f"moved by the justification floor: {justification})"
                )
        never_justified = workload_justified_by_engine.get(src) is False
        saved = 0.0
        # No saving when ``src`` was never justified by its own
        # FIRST-ASSIGNMENT workload (#475 review round 2, finding A/
        # suggestion): claiming a dollar figure here is exactly the
        # "Removes $X/mo" claim ``_build_recommendations`` already refuses
        # to make for this engine, and this field also reaches the
        # handler's own log line and the context graph
        # (``src/graph/populators.py``) directly, not only that prose.
        if full and src not in savings_claimed and not never_justified:
            savings_claimed.add(src)
            saved = recorded_saving.get(src) or (
                ENGINE_BASE_COST.get(src, 100) + EXTRA_ENGINE_BURDEN_MONTHLY
            )
        record.update(
            {
                "query_count": count,
                "action": action,
                "saved_cost_estimate": saved,
                "queries_retained": retained,
                "retention_reason": record.get("retention_reason") if retained else None,
                # Copied from the engine-level verdict, not just whatever
                # ``first_record`` happened to carry (#475 review round 2,
                # finding A): a (src, dst) pair this function synthesizes
                # fresh -- no input record ever described that exact
                # pair -- would otherwise have no ``workload_justified`` at
                # all, so ``never_justified_engines`` (synthesis_grounding.py)
                # would miss it and the TCO could list a saving again while
                # the prose already refuses to claim one.
                "workload_justified": workload_justified_by_engine.get(src),
            }
        )
        reconciled.append(record)
    return reconciled


def format_pattern_recommendation(p: dict) -> str:
    """Render one architectural pattern as a ``Recommended pattern: ...`` line.

    Shared with synthesis, which regenerates these lines from the effective assignment.
    """
    applies = p.get("applies_to", {})
    if "write_engine" in applies:
        return (
            f"Recommended pattern: {p['name']}. "
            f"Use {applies['write_engine']} for all write operations and "
            f"{', '.join(applies.get('read_engines', []))} for specialized reads. "
            f"{p['description']}"
        )
    if "source_engine" in applies and "view_engine" in applies:
        return (
            f"Recommended pattern: {p['name']}. "
            f"{applies['source_engine']} is the source of truth; "
            f"{applies['view_engine']} maintains a search-optimized projection. "
            f"{p['description']}"
        )
    return f"Recommended pattern: {p['name']}. {p['description']}"


def _engine_infra_cost(analysis_outputs: dict[str, dict] | None) -> dict[str, float]:
    """Each engine's own analysed ``cost_estimate.monthly_cost_usd`` (#375 review).

    This is the same real, per-engine infrastructure figure ``tco_analysis``
    builds its ``cost_breakdown`` from (``build_tco_analysis`` in
    synthesis_report.py reads the identical field) -- not
    ``ENGINE_BASE_COST``/``EXTRA_ENGINE_BURDEN_MONTHLY``, which is an internal
    operational-overhead heuristic used only to decide *whether* an engine is
    worth questioning (the mandatory-engine cost-share floor), never a real
    dollar figure to show a customer. An engine missing a cost estimate (or
    missing from ``analysis_outputs`` entirely) is simply absent from the
    returned dict; callers must handle that, not assume every engine has one.
    """
    costs: dict[str, float] = {}
    for engine, artifacts in (analysis_outputs or {}).items():
        monthly = ((artifacts or {}).get("cost_estimate") or {}).get("monthly_cost_usd")
        if isinstance(monthly, (int, float)):
            costs[engine] = float(monthly)
    return costs


def _build_recommendations(
    assignments: list[dict],
    consolidations: list[dict],
    patterns: list[dict],
    original_engine_queries: dict[str, list[dict]],
    engine_infra_cost: dict[str, float] | None = None,
    unique_value_assessment: dict[str, dict] | None = None,
) -> list[str]:
    """Build human-readable recommendations.

    ``engine_infra_cost`` (``_engine_infra_cost``'s output) grounds the
    infrastructure-cost figure for a fully-eliminated engine in that engine's
    own analysed cost estimate -- the same figure ``tco_analysis`` is built
    from -- rather than a heuristic never meant to be shown to a customer. A
    review of #375 found a flat ``ENGINE_BASE_COST + EXTRA_ENGINE_BURDEN_MONTHLY``
    placeholder shown as "Saves ~$450/mo", with no entry anywhere in the
    deliverables' own facts to trace it to (OpenSearch's real infrastructure
    cost on that same sample is $240.96/mo, not $450); clearly labeling the
    placeholder as "not the infrastructure cost" in an earlier revision still
    read as ungrounded, since the number itself still appeared nowhere in
    facts. The real figure, when available, replaces it; when an engine has
    no analysed cost estimate, the sentence stays purely qualitative with no
    dollar figure at all, rather than fall back to the placeholder.

    A consolidation whose ``workload_justified`` is ``False`` (#475 review,
    finding 3; set by ``_workload_ever_justified``) never claims a saving at
    all, even when a real cost estimate exists: none of that engine's
    FIRST-ASSIGNMENT queries ever cleared the #326 floor, so its removal is
    not an infrastructure saving -- the engine was never justified by the
    workload to begin with.

    ``unique_value_assessment`` (optional, #502 review round 6, finding 4)
    surfaces any Phase 1 ``tie_unresolved`` pair here too, in the
    human-readable text a customer actually reads -- not just the internal
    ``unique_value_assessment`` dict. A tie is NOT a consolidation (neither
    engine moves, nothing changed), so it gets no ``Consolidation`` record:
    every other record upholds the invariant that the queries it names
    actually moved (``len(moved_queries(c, ...)) == c["query_count"]``,
    enforced in ``tests/unit/agents/referee/test_reality_check_llm_seam.py``),
    and a tie, by definition, moves none.
    """
    recs = []
    engine_infra_cost = engine_infra_cost or {}

    # Consolidation recommendations
    for c in consolidations:
        saved = c.get("saved_cost_estimate", 0)
        source = c["from_engine"]
        if c.get("action") == "partial":
            # The source engine stays, so no cluster is avoided (#218).
            outcome = (
                f"No operational savings are claimed: this move does not remove {source} "
                "from the architecture."
            )
        elif c.get("workload_justified") is False:
            # #475 review, finding 3: none of this engine's FIRST-ASSIGNMENT
            # queries ever cleared the #326 floor (real search depth plus
            # traffic or corpus size) -- it was never justified by the
            # workload, so removing it now is not an infrastructure saving to
            # claim, even though a real cost estimate exists for it.
            outcome = (
                f"No saving is claimed: {source} was never justified by the workload "
                "-- none of its first-assignment queries clear the real-search-depth "
                "plus traffic-or-result-size floor a standing deployment needs, so no "
                "infrastructure cost was ever justified for it."
            )
        elif saved:
            real_cost = engine_infra_cost.get(source)
            if real_cost:
                # #380 review: name the figure plainly instead of pointing at an
                # internal field name (``tco_analysis``'s ``cost_breakdown``) --
                # the number itself (``engine_infra_cost``, the same one
                # ``build_tco_analysis`` carries into ``eliminated_engine_costs``
                # for this engine) is the only thing a customer needs here.
                outcome = (
                    f"Removes ${real_cost:,.2f}/mo of infrastructure cost ({source}'s own "
                    "analysed estimate before removal), plus a qualitative reduction in "
                    "operational overhead -- one fewer engine's worth of team expertise, "
                    f"monitoring, backups and failover to maintain -- by avoiding a dedicated "
                    f"{source} cluster."
                )
            else:
                outcome = (
                    f"Avoids running a dedicated {source} cluster: one fewer engine's worth of "
                    "team expertise, monitoring, backups and failover to maintain. No specific "
                    f"infrastructure-cost figure is available for {source} to show here."
                )
        else:
            outcome = f"{source} leaves the architecture."
        recs.append(
            f"Consolidated {_queries(c['query_count'])} from {source} → "
            f"{c['to_engine']}: {c['reason']}. {outcome}"
        )

    # Pattern recommendations
    recs.extend(format_pattern_recommendation(p) for p in patterns)

    # Phase 1 ties (#502 review round 6, finding 4): stated here, not just
    # in unique_value_assessment, since this is the text a customer reads.
    tied_engines = sorted(
        e for e, info in (unique_value_assessment or {}).items() if info.get("tie_unresolved")
    )
    for engine_a, engine_b in itertools.combinations(tied_engines, 2):
        recs.append(
            f"{display_engine(engine_a)} and {display_engine(engine_b)} are tied on "
            "operational cost per unique query, and no rule yet exists to decide between "
            "two such engines -- both are kept rather than one being removed arbitrarily."
        )

    if not recs:
        recs.append("No consolidation opportunities found — current assignment is optimal.")

    return recs
