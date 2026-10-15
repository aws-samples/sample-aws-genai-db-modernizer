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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from src.agents.referee.capability_registry import can_engine_serve_capability
from src.agents.referee.engine_exclusions import check_exclusions
from src.contracts.analysis_output import ScoreBreakdown
from src.shared.capability_labels import capability_label
from src.shared.catalog.patterns import (
    anti_pattern_penalties,
    capability_exempt_notes,
    capability_redesign_notes,
    capability_redesign_penalties,
    capability_signal_engines,
)
from src.shared.engine_capabilities import PATTERN_ONLY_ENGINES
from src.shared.engine_names import display_engine
from src.tools.analysis.scoring import DEFAULT_WEIGHTS, compute_confidence

ANTI_PATTERN_PENALTIES: dict[str, int] = anti_pattern_penalties()
CAPABILITY_SIGNAL_ENGINES: dict[str, frozenset[str]] = capability_signal_engines()
CAPABILITY_REDESIGN_PENALTIES: dict[str, dict[str, int]] = capability_redesign_penalties()
CAPABILITY_REDESIGN_NOTES: dict[str, str] = capability_redesign_notes()
CAPABILITY_EXEMPT_NOTES: dict[str, str] = capability_exempt_notes()


@dataclass(frozen=True)
class QueryPatternModel:
    """The per-query pattern dimension (ADR-031 Decision 14, Amendment 5, #728). Shadow only.

    ``pattern_scores``: ``query_id -> engine -> pattern score`` from the query's own
    access-pattern class (``src.shared.catalog.access_classes``). ``weights``:
    ``(engine, table_id) -> dimension weights`` the engine's analysis used for that
    table, read from ``analysis.json`` (``src.tools.analysis.score_weights``; the
    referee never reads the decision trace, ADR-017 Decision 3); a missing pair uses
    ``DEFAULT_WEIGHTS``. ``identity``: the attribution control -- every covered query
    keeps each table's own pattern score, so only the per-query table rule differs
    from today. Built by :mod:`src.agents.referee.access_pattern_shadow`; no
    production routing path builds one yet, so routing is unchanged.
    """

    pattern_scores: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    weights: Mapping[tuple[str, str], Mapping[str, float]] = field(default_factory=dict)
    identity: bool = False


def _query_pattern_confidence(
    query: Mapping, engine: str, analysis: Mapping, pattern_score: int, model: QueryPatternModel
) -> int:
    """The base score with the pattern dimension taken from the query's own class.

    For each of the query's own rated tables, the table's complexity, performance
    and cost dimensions are kept and its pattern dimension is replaced by
    ``pattern_score``, weighted as the engine's analysis weighted that table; the
    base is the mean over those tables. No all-tables fallback: a query with no rated
    table of its own scores 0. A pattern-only engine (``PATTERN_ONLY_ENGINES``) whose
    catalog gives the class no score scores 0, its existing "own pattern or 0" rule.
    """
    if not model.identity and engine in PATTERN_ONLY_ENGINES and pattern_score <= 0:
        return 0
    table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
    scores = []
    for table_id in query.get("tables_accessed") or []:
        rec = table_recs.get(table_id)
        if rec is None or not rec.get("score_breakdown"):
            continue
        breakdown = dict(rec["score_breakdown"])
        if not model.identity:
            breakdown["pattern_match_score"] = pattern_score
        weights = dict(model.weights.get((engine, table_id)) or DEFAULT_WEIGHTS)
        scores.append(compute_confidence(ScoreBreakdown(**breakdown), weights))
    return int(sum(scores) / len(scores)) if scores else 0


def compute_query_confidence(
    query_id: str,
    query: Mapping,
    engine: str,
    analysis: Mapping,
    *,
    pattern_model: QueryPatternModel | None = None,
) -> int:
    """Compute base confidence (0–100) for a query→engine pair.

    With ``pattern_model`` covering ``query_id`` (shadow only, #728), the pattern
    dimension is the query's own class score instead of its tables' patterns
    (:func:`_query_pattern_confidence`); otherwise the rule below is unchanged.

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
    if pattern_model is not None and query_id in pattern_model.pattern_scores:
        pattern_score = int(pattern_model.pattern_scores[query_id].get(engine, 0))
        return _query_pattern_confidence(query, engine, analysis, pattern_score, pattern_model)

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

# The anti-pattern penalty table and its capability scoping come from the pattern catalog
# (``src/catalog/patterns/<pattern_type>.toml``, ADR-031 Decision 8, #492), built under
# the names this module has always used (see the imports above):
#
# - ``ANTI_PATTERN_PENALTIES``: pattern type -> points an engine loses on a query that
#   shows it (each file's ``penalty``, plus ``penalty_aliases``).
# - ``CAPABILITY_SIGNAL_ENGINES``: the penalty keys that are capability statements ("this
#   query needs X") rather than workload-fit anti-patterns, mapped to the engines that
#   have the capability natively (``native`` entries): the "patterns detected by other
#   engines" step in ``build_anti_pattern_map`` never penalises those engines (#477,
#   #480). The sets are ``src.shared.engine_capabilities``'s, read from the same files.
# - ``CAPABILITY_REDESIGN_PENALTIES``: the reduced penalty for an engine that lacks the
#   capability but has a documented, bounded redesign (``redesign`` entries; none today:
#   DynamoDB's transactions are native since ADR-031 Decision 3 is applied by shape, and a
#   write too wide for one transaction is the ``wide-transaction`` anti-pattern, #493).
# - ``CAPABILITY_REDESIGN_NOTES`` / ``CAPABILITY_EXEMPT_NOTES``: the decision-trace
#   sentences for a reduced penalty and for an exemption (``{engine}`` is the engine's
#   display name). ``acid-transactions`` has no exemption note: that exemption stays
#   silent, unchanged from before #480.


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
    # ``query_id -> engines a model flagged as unable to serve it`` (ADR-031
    # Decision 11, as amended 2026-10-14, #647): a capability gap for that one
    # query only, applied as the third gate in :func:`score_query_for_engine`.
    # Empty unless a model flag was accepted (``src.agents.referee.model_flags``),
    # so a run without a model never sees it.
    flag_gaps: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # The per-query pattern dimension (#728, shadow only): ``None`` everywhere in
    # production, so the score is today's. Only the shadow builder sets one.
    pattern_model: QueryPatternModel | None = None

    def with_flag_gaps(self, gaps: Mapping[str, Iterable[str]]) -> ScoringContext:
        """A copy whose ``flag_gaps`` also carries ``gaps`` (merged per query)."""
        merged: dict[str, frozenset[str]] = dict(self.flag_gaps)
        for qid, engines in gaps.items():
            merged[qid] = merged.get(qid, frozenset()) | frozenset(engines)
        return replace(self, flag_gaps=merged)

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
GATE_KIND_MODEL_FLAG = "model_flag"
# The two literal values ``EngineScore.gate_kind`` ever carries, named here
# once so a caller that needs the full set (``src.tools.validation.
# customer_text_guard``'s snake_case-reason-code detector, #587 round 4)
# imports it instead of retyping the strings -- a new gate kind added below
# is then picked up automatically, with no second edit anywhere else.
GATE_KINDS: tuple[str, ...] = (GATE_KIND_HARD_EXCLUSION, GATE_KIND_CAPABILITY, GATE_KIND_MODEL_FLAG)
# The ownership gate (ADR-031 Decision 2 step 3, ``cache_overlay.can_own``): an
# engine that is not a system of record owns no write and no locking read. It is
# not an ``EngineScore.gate_kind`` (the score is kept); candidate ladders and the
# customer gate record it under this name (#812).
GATE_KIND_WRITE = "write_gate"


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
    ``.gate_reason`` (ADR-031 Decision 2 steps 2-3). Then a model flag
    accepted for this query (``ctx.flag_gaps``, ADR-031 Decision 11 as amended
    2026-10-14) gates the flagged engine the same way. Past all three: the base
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
    if engine in ctx.flag_gaps.get(query_id, frozenset()):
        return EngineScore(
            score=None,
            gate_reason=f"{display_engine(engine)} was flagged as unable to serve this query",
            gate_kind=GATE_KIND_MODEL_FLAG,
        )
    analysis = ctx.analysis_outputs.get(engine) or {}
    base = compute_query_confidence(
        query_id, query, engine, analysis, pattern_model=ctx.pattern_model
    )
    penalty = ctx.anti_pattern_map.get((query_id, engine), 0)
    return EngineScore(score=float(max(0, base - penalty)))
