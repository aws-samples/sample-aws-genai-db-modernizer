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
from typing import Literal, overload

from src.agents.referee.capability_registry import (
    can_engine_serve_capability,
    triage_query_capabilities,
)
from src.agents.referee.engine_exclusions import check_exclusions
from src.agents.referee.query_cost import QueryCost, QueryCostModel
from src.agents.referee.query_pattern import QueryPatternModel
from src.agents.referee.substring_match import scan_adjustments
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
from src.tools.analysis.scoring import DEFAULT_WEIGHTS, compute_confidence, weighted_confidence
from src.tools.sql.query_facts import source_engine_of

ANTI_PATTERN_PENALTIES: dict[str, int] = anti_pattern_penalties()
CAPABILITY_SIGNAL_ENGINES: dict[str, frozenset[str]] = capability_signal_engines()
CAPABILITY_REDESIGN_PENALTIES: dict[str, dict[str, int]] = capability_redesign_penalties()
CAPABILITY_REDESIGN_NOTES: dict[str, str] = capability_redesign_notes()
CAPABILITY_EXEMPT_NOTES: dict[str, str] = capability_exempt_notes()


@overload
def compute_query_confidence(
    query_id: str,
    query: Mapping,
    engine: str,
    analysis: Mapping,
    *,
    pattern_model: QueryPatternModel,
    cost_score: int,
    exact: Literal[False] = False,
) -> int: ...


@overload
def compute_query_confidence(
    query_id: str,
    query: Mapping,
    engine: str,
    analysis: Mapping,
    *,
    pattern_model: QueryPatternModel,
    cost_score: int,
    exact: Literal[True],
) -> float: ...


def compute_query_confidence(
    query_id: str,
    query: Mapping,
    engine: str,
    analysis: Mapping,
    *,
    pattern_model: QueryPatternModel,
    cost_score: int,
    exact: bool = False,
) -> float:
    """Base confidence (0-100) for a query->engine pair (ADR-031 Decision 2 step 4).

    ``exact`` (#1003) keeps the fractions routing drops: each table's weighted sum
    and their mean, unrounded. Routing never passes it; a pending engine choice
    reads it to show, and under ``--auto`` decide, a near-tie.

    The pattern dimension is the query's own access-pattern class score on ``engine``
    (``pattern_model.pattern_score``, ADR-031 Decision 14 Amendment 5, #728), never
    its tables' pattern scores: a table's ``pattern_match_score`` and
    ``confidence_score`` are not read for a classified query at all, so no other
    query on the same table, and no pattern found on it, can move this query's
    pattern dimension (#784, #885). The cost dimension is the query's own cost on
    ``engine`` (``cost_score``, :meth:`ScoringContext.cost_score`, ADR-031 Decision 5,
    #494): its share of the engine's standing cost and its own variable cost, never
    its tables' engine-blind cost band. Complexity and performance are still each of
    the query's own tables' (``tables_accessed``, #302), combined with the weights
    that engine's analysis used for the table; the base is the mean over those
    tables. No all-tables fallback: a
    classified query with no rated table of its own scores 0. A pattern-only engine
    (``PATTERN_ONLY_ENGINES``, #475) whose catalog gives the query's class no score
    scores 0: a table-wide classification earned by another query is no evidence
    about this one. Per-query pattern evidence is the class score itself, inside the
    weighted pattern dimension, never a flat bonus on top (maintainer decision, PR
    #500 review round 1).

    A statement with no access-pattern class -- ``utility`` (it reads or writes no
    table the facade can see: ``SET``, ``EXEC`` of a procedure, ``SELECT fn(...)``)
    or ``unparsed`` (a data statement the SQL facade cannot read) -- has no class
    score, so it keeps the pre-switch rule (:func:`_unclassified_confidence`), as
    ADR-031 Decision 14 Amendment 5 states: its tables' confidence, their cost band
    included. ``cost_score`` is not read for it.
    """
    if not pattern_model.has_class_score(query):
        return _unclassified_confidence(query_id, query, engine, analysis, exact=exact)
    pattern_score = pattern_model.pattern_score(query, engine)
    if engine in PATTERN_ONLY_ENGINES and pattern_score <= 0:
        return 0
    table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
    scores = []
    for table_id in query.get("tables_accessed") or []:
        rec = table_recs.get(table_id)
        if rec is None or not rec.get("score_breakdown"):
            continue
        breakdown = dict(rec["score_breakdown"])
        breakdown["pattern_match_score"] = pattern_score
        breakdown["cost_score"] = cost_score
        weights = dict(pattern_model.weights.get((engine, table_id)) or DEFAULT_WEIGHTS)
        score = ScoreBreakdown(**breakdown)
        scores.append(
            weighted_confidence(score, weights) if exact else compute_confidence(score, weights)
        )
    return _mean(scores, exact)


def _mean(values: list, exact: bool) -> float:
    """The mean of ``values`` (0 when empty), its fraction dropped unless ``exact``."""
    if not values:
        return 0
    mean = sum(values) / len(values)
    return mean if exact else int(mean)


def _unclassified_confidence(
    query_id: str, query: Mapping, engine: str, analysis: Mapping, *, exact: bool = False
) -> float:
    """The base score of a statement with no access-pattern class (``utility`` or
    ``unparsed``): the rule every query had before the switch (#302, #475), kept only
    for statements whose access pattern the facade cannot read.

    A pattern-only engine scores the tables of the patterns that matched this query
    itself, or 0; any other engine the mean confidence of the query's own tables, or,
    when it has none the analysis rated (``EXEC`` of a procedure, ``SELECT fn(...)``),
    the mean over every table the analysis rated.
    """
    table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
    if engine in PATTERN_ONLY_ENGINES:
        wa = analysis.get("workload_analysis", {})
        pattern_tables = list(
            dict.fromkeys(
                t
                for pattern in wa.get("patterns_detected", [])
                if query_id in (pattern.get("query_ids") or [])
                for t in (pattern.get("table_ids") or [])
            )
        )
        matched = [
            table_recs[t].get("confidence_score", 0) for t in pattern_tables if t in table_recs
        ]
        return _mean(matched, exact)
    own = [
        table_recs[t].get("confidence_score", 0)
        for t in query.get("tables_accessed", [])
        if t in table_recs
    ]
    if own:
        return _mean(own, exact)
    if table_recs:
        every = [r.get("confidence_score", 0) for r in table_recs.values()]
        return _mean(every, exact)
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


def merge_substring_scan(
    penalties: Mapping[tuple[str, str], int],
    notes: Mapping[tuple[str, str], str],
    collector: Mapping,
    engines: Iterable[str],
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
    """``penalties``/``notes`` with the substring-match scan penalty merged in (#493).

    The scan penalty and its notes come from one place
    (:func:`src.agents.referee.substring_match.scan_adjustments`): a ``scan`` engine
    loses the pattern's penalty on a leading-wildcard query whose scan matters, and
    every engine the pattern describes gets its reason as a note. Merged by the same
    "biggest penalty wins" rule as :func:`build_anti_pattern_map`: a bigger penalty
    already on the pair keeps its own note; otherwise the substring-match note replaces
    any zero-penalty exemption note (it is the more specific reason for this query).
    """
    merged_penalties = dict(penalties)
    merged_notes = dict(notes)
    scan_penalties, scan_notes = scan_adjustments(collector, engines)
    for key, note in scan_notes.items():
        existing = merged_penalties.get(key, 0)
        scan = scan_penalties.get(key, 0)
        if scan and scan >= existing:
            merged_penalties[key] = scan
            merged_notes[key] = note
        elif not existing:
            merged_notes[key] = note
    return merged_penalties, merged_notes


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
    # The per-query pattern dimension (ADR-031 Decision 14 Amendment 5, #728): each
    # query's own access-pattern class. :meth:`from_pipeline` builds it from the
    # collector's schema and the analysis outputs' weights; the bare default has no
    # schema keys, for a test that isolates another gate or dimension.
    pattern_model: QueryPatternModel = field(default_factory=QueryPatternModel)
    # The collector's source engine (``metadata.source_database.engine``): the
    # dialect the exclusion gate reads each query's facts in (#893), the same
    # one triage's capability detection reads them in. :meth:`from_pipeline`
    # fills it from ``collector``; empty reads them in the generic dialect.
    source_engine: str = ""
    # The per-query cost dimension (ADR-031 Decision 5, #494): the collection's
    # workload and row widths. :meth:`from_pipeline` builds it from the collector; the
    # bare default has no workload, so each scored query carries an engine's whole
    # standing cost.
    cost_model: QueryCostModel = field(default_factory=QueryCostModel)

    def servable_queries(self, engine: str) -> frozenset[str]:
        """The workload's query ids ``engine`` could serve: past its hard exclusion
        and capability gates (Decision 2 step 2), and, for an engine that is not a
        system of record, not a write or a locking read (step 3's rule; a cache serves
        the reads it fronts although it owns none). A model's flag is not counted
        (Decision 11), so a flag never moves the standing share of any other query."""
        from src.agents.referee.cache_overlay import (
            NON_SYSTEM_OF_RECORD_ENGINES,
            is_locking_read,
            is_write_query,
        )

        def serves(query: Mapping) -> bool:
            if engine in NON_SYSTEM_OF_RECORD_ENGINES and (
                is_write_query(query) or is_locking_read(query, self.source_engine)
            ):
                return False
            return _structural_gate(query, engine, self) is None

        return self.cost_model.servable(engine, serves)

    def query_cost(self, query: Mapping, engine: str) -> QueryCost:
        """``query``'s estimated monthly cost on ``engine`` (``query_cost``)."""
        return self.cost_model.cost(query, engine, self.servable_queries(engine))

    def cost_score(self, query: Mapping, engine: str) -> int:
        """``query``'s cost dimension on ``engine`` (0-100, ADR-031 Decision 5)."""
        return self.cost_model.score(query, engine, self.servable_queries(engine))

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
        (``complex_joins``, ``text_pattern_match``, ...), from the SAME signals
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
        if collector is not None:
            anti_pattern_map, anti_pattern_notes = merge_substring_scan(
                anti_pattern_map, anti_pattern_notes, collector, analysis_outputs.keys()
            )
        if triage is not None and "query_capabilities" in triage:
            # Legacy capability ids mapped (a triage.json saved before #493).
            query_capabilities: Mapping[str, list[str]] = triage_query_capabilities(triage)
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
            pattern_model=QueryPatternModel.from_pipeline(collector, analysis_outputs),
            source_engine=source_engine_of(collector),
            cost_model=QueryCostModel.from_collector(collector),
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


def score_query_for_engine(
    query: Mapping, engine: str, ctx: ScoringContext, *, exact: bool = False
) -> EngineScore:
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

    ``exact=True`` (#1003) is the same score before its fractions are dropped
    (:func:`compute_query_confidence`): the deciding fact of a pending engine
    choice, never a routing input.
    """
    query_id = str(query.get("query_id") or "")
    gated = _structural_gate(query, engine, ctx)
    if gated is not None:
        return gated
    if engine in ctx.flag_gaps.get(query_id, frozenset()):
        return EngineScore(
            score=None,
            gate_reason=f"{display_engine(engine)} was flagged as unable to serve this query",
            gate_kind=GATE_KIND_MODEL_FLAG,
        )
    analysis = ctx.analysis_outputs.get(engine) or {}
    cost_score = ctx.cost_score(query, engine)
    base = (
        compute_query_confidence(
            query_id,
            query,
            engine,
            analysis,
            pattern_model=ctx.pattern_model,
            cost_score=cost_score,
            exact=True,
        )
        if exact
        else compute_query_confidence(
            query_id,
            query,
            engine,
            analysis,
            pattern_model=ctx.pattern_model,
            cost_score=cost_score,
        )
    )
    penalty = ctx.anti_pattern_map.get((query_id, engine), 0)
    return EngineScore(score=float(max(0, base - penalty)))


def _structural_gate(query: Mapping, engine: str, ctx: ScoringContext) -> EngineScore | None:
    """The hard exclusion and capability gates (ADR-031 Decision 2 step 2) for
    ``(query, engine)``: the gated :class:`EngineScore`, or ``None`` when neither fires."""
    query_id = str(query.get("query_id") or "")
    query_text = query.get("query_text", "")
    exclusion = check_exclusions(query_id, query_text, engine, ctx.source_engine)
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
    return None
