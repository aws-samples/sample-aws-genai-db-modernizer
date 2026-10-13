"""The resolver's per-query, per-engine score (ADR-031 Decision 2 steps 2-5),
shared so every caller that needs to know how well an engine fits one query
uses the exact same arithmetic.

:func:`score_query_for_engine` composes the whole pipeline -- the hard
exclusion gate, the hard capability gate, the base confidence
(:func:`compute_query_confidence`) and the anti-pattern penalty
(:func:`build_anti_pattern_map`), clamped at 0 -- into the one function
``AssignmentResolver.resolve()``'s own Step 4 and every OTHER per-query
scorer should call (:mod:`src.agents.referee.cache_overlay`'s
``fallback_owner``, #549; the reality check's own per-query fit, #490).

:class:`ScoringContext` is what makes that one function trustworthy: its
``analysis_outputs``/``anti_pattern_map``/``query_capabilities`` fields all
default to empty, so a caller building one by hand -- as ``fallback_owner``
did in #549's first round, with no anti-pattern map and a capability map
detected from bare text with no triage signals -- can silently drop a gate
or the penalty and still get a number back, no error, just a wrong one (#549
round-2 review: this is how both a missing-capability repro and a
missing-penalty repro slipped through round 1's fix). :meth:`ScoringContext.
from_pipeline` is the one constructor every production caller should use
instead: it always builds the anti-pattern map, and always prefers triage's
own (collector-signal-aware) ``query_capabilities`` over a bare-text guess.
The plain dataclass constructor stays available for a test that wants to
isolate a single gate or dimension; it should never appear in production
code.

Every function here is pure (no state, no I/O): each reads only the
arguments it is given. Moved out of ``assignment_resolver.py`` into its own
module (rather than imported from there) because ``cache_overlay.py`` is
itself imported by ``assignment_resolver.py`` -- a module that needs this
scoring and is needed by one that already defines it would be a circular
import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.referee.capability_registry import can_engine_serve_capability
from src.agents.referee.engine_exclusions import check_exclusions
from src.shared.capability_labels import capability_label
from src.shared.engine_capabilities import (
    ACID_TRANSACTION_ENGINES,
    FUZZY_SEARCH_ENGINES,
    PATTERN_ONLY_ENGINES,
    TEXT_SEARCH_ENGINES,
)
from src.shared.engine_names import display_engine


def compute_query_confidence(
    query_id: str,
    query: Mapping,
    engine: str,
    analysis: Mapping,
) -> int:
    """Compute base confidence (0–100) for a query→engine pair.

    The query's own ``tables_accessed`` decide the base score (#302):
    a table-level recommendation only ever informs a query through
    tables THAT QUERY itself touches, never through a pattern's full
    ``table_ids`` list, which can span every table any query matching
    that pattern type touches anywhere in the schema (a broad pattern
    like "pagination" matches most queries in a workload, so its own
    table list is close to the whole schema, not this one query's
    tables).

    Per-query pattern evidence is not added here (maintainer decision,
    PR #500 review round 1): a flat bonus is not itself per-query
    evidence, and it double-counts the pattern dimension the table
    confidence already weighs in (40% by default, ADR-017/ADR-031). A
    near-universal, low-confidence pattern (DynamoDB's
    ``low-frequency-read``, matched by most of a workload) would still
    trigger the same flat bonus as a precise, high-confidence one, which
    favours whichever engine happens to have the broadest catch-all
    pattern rather than reflecting real fit. Per-query pattern evidence
    is coming with the engine-neutral detectors (#491), inside the
    weighted pattern dimension rather than on top of it.

    ``PATTERN_ONLY_ENGINES`` (#475) keep their own, unchanged rule,
    untouched by this fix: a table-wide classification earned by a
    different query is not evidence about this one at all for these
    engines, so they score their own matched pattern directly, or 0 --
    never a ``tables_accessed`` average. Using ``tables_accessed`` as
    these engines' base too would reintroduce #475's table-wide
    classification bleed (OpenSearch classifies a whole table as
    SEARCH/TIMESERIES the moment any one query against it matches,
    which says nothing about a neighboring plain lookup on the same
    table).
    """
    table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
    wa = analysis.get("workload_analysis", {})

    if engine in PATTERN_ONLY_ENGINES:
        pattern_tables = list(
            dict.fromkeys(
                t
                for pattern in wa.get("patterns_detected", [])
                if query_id in (pattern.get("query_ids") or [])
                for t in (pattern.get("table_ids") or [])
            )
        )
        if not pattern_tables:
            return 0
        matched_scores = [
            table_recs[t].get("confidence_score", 0) for t in pattern_tables if t in table_recs
        ]
        return int(sum(matched_scores) / len(matched_scores)) if matched_scores else 0

    # The query's own tables_accessed decide the base score -- no
    # pattern-match bonus on top (see the docstring above).
    query_tables = query.get("tables_accessed", [])
    matched_scores = [
        table_recs[t].get("confidence_score", 0) for t in query_tables if t in table_recs
    ]
    if matched_scores:
        return int(sum(matched_scores) / len(matched_scores))

    # Last resort, when the query has no tables of its own at all (or
    # none the analysis rated): average across every table
    # recommendation, unchanged from before #302.
    if table_recs:
        scores = [r.get("confidence_score", 0) for r in table_recs.values()]
        return int(sum(scores) / len(scores))
    return 0


# ---------------------------------------------------------------------------
# Anti-pattern penalty (ADR-031 Decision 2 step 5)
# ---------------------------------------------------------------------------

# Analysis anti-pattern types that indicate an engine is the WRONG fit for a query.
# When a query appears in one of these anti-patterns for an engine, that engine's
# score is penalized for this specific query.
ANTI_PATTERN_PENALTIES: dict[str, int] = {
    # NoSQL engine anti-patterns (queries wrong for DynamoDB/DocumentDB)
    "text_search": 40,  # DynamoDB can't do full-text search at all
    "wildcard-search": 40,  # LIKE '%term%' is not DynamoDB's job
    "full-text-search": 40,  # tsvector/MATCH AGAINST — same reasoning
    "regex-search": 40,  # REGEXP/RLIKE/~ — same reasoning
    "fuzzy-search": 40,  # pg_trgm similarity() — same reasoning
    "complex-aggregation": 50,  # Heavy penalty — better engines exist, but DynamoDB can pre-compute
    "complex_aggregation": 50,  # Alias (some analysis agents use underscore)
    "multi-index-joins": 25,
    "acid-transactions": 20,
    "frequent-writes": 15,
    # Aurora anti-patterns (queries wrong for Aurora — better on purpose-built engines)
    "high-frequency-pk-lookup": 30,  # DynamoDB does this better at scale
    "simple-cache-read": 25,  # ElastiCache does this at microsecond latency
    "no-relational-need": 20,  # DynamoDB can serve simple KV without relational overhead
    "single-access-pattern-table": 15,  # DynamoDB is cheaper for simple patterns
    "high-volume-text-search": 35,  # OpenSearch purpose-built for this
}

# Which ANTI_PATTERN_PENALTIES keys are capability statements rather than pure
# workload-fit anti-patterns: "this query needs X" versus "this engine is a
# worse fit for this query than a purpose-built one". The "patterns detected
# by other engines penalise OTHER engines" step in ``build_anti_pattern_map``
# must not apply a capability key's penalty to an engine that has the
# capability natively -- a capability signal says nothing about whether THIS
# engine is a good fit, only that engines without the capability need one.
# Mapped to ``src.shared.engine_capabilities`` so this stays the single fact
# every caller agrees on, instead of a second, possibly-drifted copy.
#
# ``acid-transactions``: Aurora PostgreSQL, Aurora MySQL and DocumentDB run
# multi-row/-document transactions natively (#477).
#
# ``wildcard-search``, ``full-text-search`` and ``regex-search``: OpenSearch's
# os-01..os-03 patterns all ask the same question -- can this engine run a
# text/pattern-search-shaped query at all, even as an unindexed scan -- so
# all three share ``TEXT_SEARCH_ENGINES`` (#480). ``fuzzy-search`` (os-04)
# asks a narrower one -- does this engine have a similarity/fuzzy-match
# operator at all -- which only Aurora PostgreSQL's ``pg_trgm`` answers yes
# to, so it gets its own ``FUZZY_SEARCH_ENGINES``. OpenSearch itself is
# still worth the move when traffic, data size or search depth justify it
# (#326); that is a separate, untouched mechanism (``high-volume-text-search``
# above is Aurora's own self-detected anti-pattern for exactly that case).
CAPABILITY_SIGNAL_ENGINES: dict[str, frozenset[str]] = {
    "acid-transactions": ACID_TRANSACTION_ENGINES,
    "wildcard-search": TEXT_SEARCH_ENGINES,
    "full-text-search": TEXT_SEARCH_ENGINES,
    "regex-search": TEXT_SEARCH_ENGINES,
    "fuzzy-search": FUZZY_SEARCH_ENGINES,
}

# Reduced penalty for an engine that lacks a capability outright but has a
# documented, bounded way to redesign around it, so the cost stays visible
# instead of collapsing to the same full "wrong engine" penalty as an engine
# with no path at all (#477). DynamoDB's own ``TransactWriteItems`` (no
# interactive BEGIN/COMMIT, each item usable at most once per transaction,
# <=100 items, <=4 MB, one account/Region, 2x the write capacity of the same
# writes done without it) or an idempotent multi-step flow both work, at a
# real engineering cost; 10 sits below ``acid-transactions``'s own full 20.
#
# The search patterns have no entry here (#480): every engine without the
# relevant capability set (``TEXT_SEARCH_ENGINES`` or ``FUZZY_SEARCH_ENGINES``)
# has no bounded redesign to point to the way ``TransactWriteItems`` is one
# -- they keep the full 40.
CAPABILITY_REDESIGN_PENALTIES: dict[str, dict[str, int]] = {
    "acid-transactions": {"dynamodb": 10},
}

# The reason text for a reduced redesign penalty: plain language, no raw
# engine id, worded so it reads as "this engine needs extra work", never as
# "this engine already relies on transactions". ``{engine}`` is replaced
# with the engine's display name (e.g. "DynamoDB") -- see ``resolve()`` for
# where this is attached (only to the engine the note is about, and only if
# that engine is the one the query is actually assigned to).
CAPABILITY_REDESIGN_NOTES: dict[str, str] = {
    "acid-transactions": (
        "Uses transactions: moving it to {engine} needs a redesign "
        "({engine} transactions, up to 100 items, or retry-safe steps)"
    ),
}

# The reason text for a capability EXEMPTION (decision-trace note, #480):
# unlike a redesign note, this fires when the engine the query ended up on
# is the one with the capability, so it reads as "nothing to see here, this
# engine already runs it" rather than "this needs extra work". ``{engine}``
# is replaced with the engine's display name the same way
# ``CAPABILITY_REDESIGN_NOTES`` is. Shared by all four search patterns --
# the wording fits each of them (LIKE, regex, full-text or fuzzy, all
# "search patterns"). ``acid-transactions`` has no entry: that exemption
# stays silent, unchanged from before #480.
_TEXT_SEARCH_EXEMPT_NOTE = (
    "{engine} runs this search pattern natively: not penalized. OpenSearch "
    "is worth the move only when traffic, data size or search depth justify "
    "a dedicated search engine"
)
CAPABILITY_EXEMPT_NOTES: dict[str, str] = {
    "wildcard-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "full-text-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "regex-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "fuzzy-search": _TEXT_SEARCH_EXEMPT_NOTE,
}


def _capability_scoped_penalty(
    pattern_type: str, engine: str, penalty: int
) -> tuple[int, str | None]:
    """Scope a capability-shaped anti-pattern penalty to engines that need it.

    Returns ``(scoped_penalty, note)``. A pure workload-fit anti-pattern (not
    in ``CAPABILITY_SIGNAL_ENGINES``) returns ``penalty`` unchanged. For a
    capability signal: an engine with the capability is exempt (penalty 0),
    plus a note from ``CAPABILITY_EXEMPT_NOTES`` when one is configured for
    that pattern type; an engine without the capability but with a
    documented redesign path (``CAPABILITY_REDESIGN_PENALTIES``) gets that
    smaller penalty plus a note; every other engine keeps the full
    configured penalty.
    """
    capable_engines = CAPABILITY_SIGNAL_ENGINES.get(pattern_type)
    if capable_engines is None:
        return penalty, None
    if engine in capable_engines:
        exempt_template = CAPABILITY_EXEMPT_NOTES.get(pattern_type)
        exempt_note = (
            exempt_template.format(engine=display_engine(engine)) if exempt_template else None
        )
        return 0, exempt_note
    redesign_penalty = CAPABILITY_REDESIGN_PENALTIES.get(pattern_type, {}).get(engine)
    if redesign_penalty is not None:
        note_template = CAPABILITY_REDESIGN_NOTES.get(pattern_type)
        note = note_template.format(engine=display_engine(engine)) if note_template else None
        return redesign_penalty, note
    return penalty, None


def build_anti_pattern_map(
    analysis_outputs: Mapping[str, Mapping],
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """Build (query_id, engine) → penalty map from analysis anti-patterns.

    When a query appears in an anti-pattern for an engine, its confidence
    score for that engine gets reduced by the penalty amount. This ensures
    queries that are fundamentally wrong for an engine (e.g., text search
    on DynamoDB) don't get assigned there just because the TABLE average
    is high.

    A capability signal (``CAPABILITY_SIGNAL_ENGINES``) is scoped through
    ``_capability_scoped_penalty`` before being recorded, whether it was
    self-detected or picked up from the "patterns detected by other
    engines" step below, so an engine that has the capability is never
    penalised for it. Returns the penalty map plus a
    ``(query_id, engine) → note`` map carrying the human-readable reason
    for a reduced "needs a redesign" penalty, or for a capability
    exemption that has a note configured (``CAPABILITY_EXEMPT_NOTES``),
    so a caller can surface it in the query's assignment reason
    (``QueryAssignment.assignment_reason``, ``AssignmentResolver.resolve()``'s
    Step 6c). An exempt note is only kept when no OTHER anti-pattern has
    already put a real (non-zero) penalty on the same ``(query_id, engine)``
    pair -- once one does, that penalty's own note (or lack of one) replaces
    it, the same "biggest penalty wins" rule the non-zero branch below
    already applies, so the note never claims an engine was unpenalised when
    it actually lost points to a different anti-pattern.
    """
    penalties: dict[tuple[str, str], int] = {}
    notes: dict[tuple[str, str], str] = {}

    def _record(key: tuple[str, str], pattern_type: str, raw_penalty: int) -> None:
        _qid, target_engine = key
        scoped_penalty, note = _capability_scoped_penalty(pattern_type, target_engine, raw_penalty)
        if scoped_penalty == 0:
            if note and key not in penalties:
                notes.setdefault(key, note)
            return
        if scoped_penalty >= penalties.get(key, 0):
            penalties[key] = scoped_penalty
            if note:
                notes[key] = note
            else:
                notes.pop(key, None)

    for engine, analysis in analysis_outputs.items():
        wa = analysis.get("workload_analysis", {})
        for ap in wa.get("anti_patterns_detected") or []:
            ap_type = ap.get("anti_pattern_type", "")
            penalty = ANTI_PATTERN_PENALTIES.get(ap_type, 0)
            if penalty == 0:
                continue
            for qid in ap.get("query_ids") or []:
                _record((qid, engine), ap_type, penalty)

        # Also check patterns_detected from OTHER engines as positive signals
        # (e.g., if OpenSearch detects wildcard-search pattern for a query,
        # that's a signal the query belongs there)
        for pattern in wa.get("patterns_detected") or []:
            pattern_type = pattern.get("pattern_type", "")
            # If a pattern type matches an anti-pattern penalty name,
            # penalize OTHER engines for those queries
            penalty = ANTI_PATTERN_PENALTIES.get(pattern_type, 0)
            if penalty == 0:
                continue
            for qid in pattern.get("query_ids", []):
                for other_engine in analysis_outputs:
                    if other_engine != engine:
                        _record((qid, other_engine), pattern_type, penalty)

    return penalties, notes


# ---------------------------------------------------------------------------
# The whole per-query, per-engine score (ADR-031 Decision 2 steps 2-5, #549)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringContext:
    """Everything :func:`score_query_for_engine` needs beyond the one
    ``(query, engine)`` pair it is scoring.

    ``analysis_outputs``: ``{engine: analysis}``, the same dict
    ``AssignmentResolver.resolve()`` is handed.

    ``anti_pattern_map``/``anti_pattern_notes``: ``(query_id, engine) ->
    penalty``/``-> note``, both from :func:`build_anti_pattern_map`.

    ``query_capabilities``: ``query_id -> required capability list``, when
    known. Looked up with a ``[]`` default -- this function never runs
    capability detection itself, so a query absent from the mapping scores
    as if it needs nothing.

    Every field defaults to empty, which is deliberately dangerous: a
    half-built context (say, the anti-pattern map left empty) still scores
    without error, just wrongly -- the #549 round-1 fix built exactly that,
    with no anti-pattern map at all and a capability map detected from bare
    text with no triage signals, and both gaps shipped unnoticed until
    round 2 compared it against the resolver by hand. :meth:`from_pipeline`
    is the one constructor that always fills in the first three; use it in
    every production code path. The bare defaults exist only so a test can
    isolate a single gate or dimension without wiring up the rest.
    """

    analysis_outputs: Mapping[str, Mapping] = field(default_factory=dict)
    anti_pattern_map: Mapping[tuple[str, str], int] = field(default_factory=dict)
    anti_pattern_notes: Mapping[tuple[str, str], str] = field(default_factory=dict)
    query_capabilities: Mapping[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_pipeline(
        cls,
        analysis_outputs: Mapping[str, Mapping],
        triage: Mapping | None = None,
        collector: Mapping | None = None,
    ) -> ScoringContext:
        """The one constructor every production caller should use (#549 round 2).

        Always builds the anti-pattern map and notes (:func:`build_anti_pattern_map`)
        from ``analysis_outputs`` -- there is no valid reason to score without
        the penalty step, so this is never optional here the way the bare
        dataclass fields are.

        The capability map is ``triage``'s own ``query_capabilities`` when
        ``triage`` HAS that key at all -- even an explicit ``{}`` (triage ran
        its detection and genuinely found nothing) is trusted as-is, never
        recomputed, because every real ``triage.triage()`` output always
        carries this key (:class:`TriageResult`'s own default, #549 round-2
        review). Only when the key is truly absent (a ``triage`` with no
        ``query_capabilities`` field at all -- a legacy artifact, or no
        triage available) does this fall back, and then with ``collector``
        given, by running ``triage.detect_query_capabilities`` with
        whatever ``TriageSignal``\\ s ``triage`` itself already has (``[]``
        when it has none either) -- the same collector-signal-aware
        detection a real ``query_capabilities`` entry would carry
        (``complex_joins``, ``inverted_index``, ...), from the SAME signals
        this ``triage`` already decided apply, never a fresh re-detection
        that could find a different signal set than what is actually in
        this ``triage``. Using a bare-text guess with no signals at all here
        instead is how #549's first round still let ``fallback_owner`` hand
        a query to an engine the resolver had already ruled out. With
        neither ``triage`` nor ``collector``, the capability map stays
        empty -- the gate has nothing to check against, so it never
        excludes anyone; this is the one intentional gap :meth:`from_pipeline`
        allows, for a caller that genuinely has no triage artifact or
        collector output on hand.
        """
        anti_pattern_map, anti_pattern_notes = build_anti_pattern_map(analysis_outputs)
        if triage is not None and "query_capabilities" in triage:
            query_capabilities: Mapping[str, list[str]] = dict(triage["query_capabilities"] or {})
        elif collector is not None:
            from src.agents.referee.triage import TriageSignal, detect_query_capabilities

            raw_signals = (triage or {}).get("signals") or []
            signals = [
                TriageSignal(
                    signal=s.get("signal", ""),
                    targets=list(s.get("targets") or []),
                    evidence=s.get("evidence", ""),
                    query_ids=list(s.get("query_ids") or []),
                    table_ids=list(s.get("table_ids") or []),
                    query_count=s.get("query_count", 0),
                )
                for s in raw_signals
            ]
            query_capabilities = dict(detect_query_capabilities(dict(collector), signals))
        else:
            query_capabilities = {}
        return cls(
            analysis_outputs=analysis_outputs,
            anti_pattern_map=anti_pattern_map,
            anti_pattern_notes=anti_pattern_notes,
            query_capabilities=query_capabilities,
        )


GATE_KIND_HARD_EXCLUSION = "hard_exclusion"
GATE_KIND_CAPABILITY = "capability"
# The two literal values ``EngineScore.gate_kind`` ever carries, named here
# once so a caller that needs the full set (``src.tools.validation.
# customer_text_guard``'s snake_case-reason-code detector, #587 round 4)
# imports it instead of retyping the strings -- a new gate kind added below
# is then picked up automatically, with no second edit anywhere else.
GATE_KINDS: tuple[str, ...] = (GATE_KIND_HARD_EXCLUSION, GATE_KIND_CAPABILITY)


@dataclass(frozen=True)
class EngineScore:
    """The result of scoring one ``(query, engine)`` pair (#549 round 2).

    ``score`` is ``None`` when ``engine`` cannot serve the query at all (a
    hard exclusion or a missing required capability) -- never a 0 a caller
    could mistake for "scores poorly" and still compare against a real,
    serviceable score; a caller comparing several engines' scores should
    drop a ``None`` candidate from contention entirely, not treat it as the
    lowest bid.

    ``gate_reason`` is the human-readable reason text ONLY when ``score`` is
    ``None`` -- customer-facing prose (``display_engine``, the exclusion's own
    customer-facing ``customer_message``, a capability label, never the raw
    engine id, an internal ``[rule_id]``/``[capability]`` tag or
    ``description``, the exclusion's technical, log-only reason -- #572) --
    so a caller never has to re-run
    ``check_exclusions``/``can_engine_serve_capability`` a second time just
    to find out WHY, the way ``resolve()`` itself did before this (#549
    round-2 review, Suggestion 1).

    ``gate_kind`` is the same fact as ``gate_reason``, structured
    (``"hard_exclusion"`` or ``"capability"``, else ``None``) rather than
    prose -- so a caller that only needs to know WHICH gate fired (Step 7c's
    own routing-trace ladder currently finds this by matching substrings of
    ``gate_reason`` instead, a second copy of the gate order this type
    exists to retire -- #549 round-3 review, Suggestion 4a) never has to
    parse ``gate_reason`` to get it.
    """

    score: float | None
    gate_reason: str | None = None
    gate_kind: str | None = None


def score_query_for_engine(query: Mapping, engine: str, ctx: ScoringContext) -> EngineScore:
    """The resolver's adjusted score for ``(query, engine)``, as an
    :class:`EngineScore` -- never ``None`` itself; check ``.score``.

    The hard exclusion gate (``check_exclusions``), then the hard capability
    gate (``ctx.query_capabilities``/``can_engine_serve_capability``) --
    either one gates ``.score`` to ``None`` with the reason text in
    ``.gate_reason`` (ADR-031 Decision 2 steps 2-3). Past both: the base
    per-query confidence (:func:`compute_query_confidence`), minus
    ``ctx.anti_pattern_map``'s penalty for this ``(query_id, engine)`` pair,
    clamped at 0 (ADR-031 Decision 2 steps 4-5) -- exactly
    ``AssignmentResolver.resolve()``'s own Step 4 arithmetic, so every caller
    of this function and ``resolve()`` itself agree on one score for a given
    ``(query, engine, ctx)`` without either side re-deriving it a second way.
    """
    query_id = str(query.get("query_id") or "")
    query_text = query.get("query_text", "")
    exclusion = check_exclusions(query_id, query_text, engine)
    if exclusion:
        return EngineScore(
            score=None,
            gate_reason=(
                f"excluded from {display_engine(engine)}: it {exclusion.customer_message}"
            ),
            gate_kind=GATE_KIND_HARD_EXCLUSION,
        )
    required_caps = ctx.query_capabilities.get(query_id, [])
    if required_caps and not can_engine_serve_capability(engine, required_caps):
        return EngineScore(
            score=None,
            gate_reason=(
                f"{display_engine(engine)} lacks required capability: "
                f"{', '.join(capability_label(c) for c in required_caps)}"
            ),
            gate_kind=GATE_KIND_CAPABILITY,
        )
    analysis = ctx.analysis_outputs.get(engine) or {}
    base = compute_query_confidence(query_id, query, engine, analysis)
    penalty = ctx.anti_pattern_map.get((query_id, engine), 0)
    return EngineScore(score=float(max(0, base - penalty)))
