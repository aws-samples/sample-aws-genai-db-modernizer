# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-engine consistency guard for the engine analyses (#1994).

An engine analysis can flag a query as better served by another engine: an anti-pattern
whose catalog entry names that engine (``better_served_by`` in its pattern file, ADR-031
Decision 8). When engine A's analysis points a query at engine B and B's analysis points
the same query back at A, the two notes contradict each other, and a deliverable that
quoted both would say the query belongs on each engine.

This guard runs before synthesis builds anything from the analyses:

1. **Detect** (:func:`find_contradictions`): every query two analyses point at each
   other's engine. Deterministic, read from the analyses and the catalog only.
2. **Settle** (:func:`settle`): routing already decided the query, so the outcome is the
   engine the latest assignment gives it (``assigned_engine``): one of the pair, or a
   third engine. Only a query the assignment does not hold falls back to the single
   scorer, ``query_scoring.score_query_for_engine`` with ``ScoringContext.from_pipeline``,
   on the two engines: the higher score wins, a gated engine loses to a scored one, and
   an exact tie goes through ``tie_break.break_tie`` with the engines the assignment uses
   and the settled workload counts (ADR-031 Decision 12).
3. **Reconcile** (:func:`reconcile`): a note that points the query at an engine other
   than the outcome contradicts it, so the query leaves that anti-pattern entry (the
   entry goes when no query is left). When the outcome is one of the pair, the other
   engine's note, which points at it, stays: the deliverables carry that one statement.
   When it is a third engine, both notes go: neither engine's advice describes the plan.

The guard never routes and never rewrites a stored artifact; it only stops the notes
from disagreeing with the routing outcome. Each contradiction on the bundled samples and
the benchmark cases is a rule-catalog defect, listed by
``tests/e2e/test_analysis_consistency_defects.py`` (expected empty).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.shared.catalog.patterns import patterns

TIE_RULE_SCORE = "score"
# The outcome came from the assignment: one of the pair, or a third engine.
RULE_ASSIGNED = "assigned"
RULE_ASSIGNED_THIRD = "assigned_third_engine"


@dataclass(frozen=True)
class Claim:
    """One analysis's note that a query is better served by another engine."""

    engine: str
    query_id: str
    better_engine: str
    pattern_type: str


@dataclass(frozen=True)
class Contradiction:
    """Two analyses pointing one query at each other's engine, and how it settled.

    ``claims`` are the two notes, sorted by engine. ``winner`` is the outcome (``None``
    until :func:`settle` runs): the query's ``assigned_engine`` (``rule`` is
    ``"assigned"``, or ``"assigned_third_engine"`` when it is neither of the pair), else
    the single scorer's pick (``"score"`` or the tie kind ``break_tie`` reported, with
    ``scores`` the scorer's value per engine, ``None`` for a gated engine).
    """

    query_id: str
    claims: tuple[Claim, Claim]
    winner: str | None = None
    scores: Mapping[str, float | None] = field(default_factory=dict)
    rule: str | None = None

    @property
    def engines(self) -> tuple[str, str]:
        return (self.claims[0].engine, self.claims[1].engine)

    def as_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "engines": list(self.engines),
            "patterns": {c.engine: c.pattern_type for c in self.claims},
            "winner": self.winner,
            "scores": dict(self.scores),
            "rule": self.rule,
        }


def _anti_patterns(analysis: Mapping | None) -> list[Mapping]:
    workload = (analysis or {}).get("workload_analysis") or {}
    return [ap for ap in workload.get("anti_patterns_detected") or [] if isinstance(ap, Mapping)]


def _better_served_by(pattern_type: str, engine: str) -> tuple[str, ...]:
    entry = next((p for p in patterns() if p.id == pattern_type), None)
    support = entry.engines.get(engine) if entry else None
    if support is None or support.kind != "anti_pattern":
        return ()
    return support.better_served_by


def analysis_claims(analyses: Mapping[str, Mapping | None]) -> list[Claim]:
    """Every (query, better engine) note the analyses carry, in a stable order."""
    claims: set[Claim] = set()
    for engine, analysis in analyses.items():
        for ap in _anti_patterns(analysis):
            pattern_type = str(ap.get("anti_pattern_type") or "")
            targets = [t for t in _better_served_by(pattern_type, engine) if t != engine]
            for qid in ap.get("query_ids") or []:
                for target in targets:
                    claims.add(Claim(engine, str(qid), target, pattern_type))
    return sorted(claims, key=lambda c: (c.query_id, c.engine, c.better_engine, c.pattern_type))


def find_contradictions(analyses: Mapping[str, Mapping | None]) -> list[Contradiction]:
    """Every query two analyses point at each other's engine (unsettled)."""
    by_query: dict[str, list[Claim]] = {}
    for claim in analysis_claims(analyses):
        by_query.setdefault(claim.query_id, []).append(claim)
    found: list[Contradiction] = []
    for qid, claims in sorted(by_query.items()):
        seen: set[tuple[str, str]] = set()
        for a in claims:
            for b in claims:
                if a.engine >= b.engine:
                    continue
                if a.better_engine == b.engine and b.better_engine == a.engine:
                    pair = (a.engine, b.engine)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    found.append(Contradiction(qid, (a, b)))
    return found


def settle(
    contradictions: Iterable[Contradiction],
    queries: Mapping[str, Mapping],
    ctx: object | None,
    *,
    assigned: Mapping[str, str] | None = None,
    current_engines: Iterable[str] = (),
    settled: Mapping[str, int] | None = None,
    source_engine: str = "",
) -> list[Contradiction]:
    """Each contradiction with its outcome.

    ``assigned`` maps a query id to its ``assigned_engine`` in the latest assignment; that
    engine is the outcome whenever the query has one. Otherwise ``ctx`` (a
    ``query_scoring.ScoringContext`` built with ``from_pipeline``) scores the pair, and an
    exact tie reads ``current_engines`` (the engines the assignment uses) and ``settled``
    (``tie_break.settled_counts``). A query with neither stays unsettled (``winner`` None).
    """
    from src.agents.referee.query_scoring import score_query_for_engine
    from src.agents.referee.tie_break import break_tie
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    source_aurora = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    routed = assigned or {}
    engines_in_use = set(current_engines)
    out: list[Contradiction] = []
    for c in contradictions:
        engine = routed.get(c.query_id)
        if engine:
            rule = RULE_ASSIGNED if engine in c.engines else RULE_ASSIGNED_THIRD
            out.append(Contradiction(c.query_id, c.claims, engine, {}, rule))
            continue
        query = queries.get(c.query_id)
        if query is None or ctx is None:
            out.append(c)
            continue
        scores: dict[str, float | None] = {
            e: score_query_for_engine(query, e, ctx, exact=True).score  # type: ignore[arg-type]
            for e in c.engines
        }
        scored = {e: v for e, v in scores.items() if v is not None}
        if not scored:
            out.append(Contradiction(c.query_id, c.claims, None, scores, None))
            continue
        top = max(scored.values())
        tied = sorted(e for e, v in scored.items() if v == top)
        if len(tied) == 1:
            (winner,), rule = tied, TIE_RULE_SCORE
        else:
            winner, rule = break_tie(tied, engines_in_use, source_aurora, settled or {}, c.query_id)
        out.append(Contradiction(c.query_id, c.claims, winner, scores, rule))
    return out


def reconcile(
    analyses: Mapping[str, Mapping | None], settled: Iterable[Contradiction]
) -> dict[str, Mapping | None]:
    """``analyses`` with each settled contradiction's contradicting notes removed.

    A note that points the query at an engine other than the outcome contradicts it: the
    query leaves that anti-pattern entry, and the entry goes when it has no query left.
    With the outcome one of the pair, that is the outcome engine's own note; with a third
    engine, both notes. Every other note is kept. The input is not changed.
    """
    drop: dict[tuple[str, str], set[str]] = {}
    for c in settled:
        if c.winner is None:
            continue
        for claim in c.claims:
            if claim.better_engine != c.winner:
                drop.setdefault((claim.engine, claim.pattern_type), set()).add(c.query_id)
    if not drop:
        return dict(analyses)
    out: dict[str, Mapping | None] = {}
    for engine, analysis in analyses.items():
        if not analysis or not any(e == engine for e, _ in drop):
            out[engine] = analysis
            continue
        copied = copy.deepcopy(dict(analysis))
        workload = copied.get("workload_analysis") or {}
        kept: list = []
        for ap in workload.get("anti_patterns_detected") or []:
            gone = drop.get((engine, str(ap.get("anti_pattern_type") or "")), set())
            ids = [q for q in ap.get("query_ids") or [] if q not in gone]
            if gone and not ids and ap.get("query_ids"):
                continue
            if gone:
                ap = {**ap, "query_ids": ids}
            kept.append(ap)
        workload["anti_patterns_detected"] = kept
        out[engine] = copied
    return out


def check_analysis_consistency(
    analyses: Mapping[str, Mapping | None],
    collector: Mapping | None,
    triage: Mapping | None,
    assignment: Mapping | None,
) -> tuple[dict[str, Mapping | None], list[Contradiction]]:
    """Detect, settle and reconcile in one step: ``(reconciled analyses, contradictions)``."""
    found = find_contradictions(analyses)
    if not found:
        return dict(analyses), []
    from src.agents.referee.aurora_choice import source_database_engine
    from src.agents.referee.query_scoring import ScoringContext
    from src.agents.referee.routine_units import RoutineFollowers
    from src.agents.referee.tie_break import settled_counts
    from src.agents.referee.write_groups import assignment_sources_of_truth

    queries = {
        str(q.get("query_id")): q
        for q in ((collector or {}).get("queries") or {}).get("query_patterns") or []
        if isinstance(q, Mapping)
    }
    qas = [
        qa for qa in (assignment or {}).get("query_assignments") or [] if isinstance(qa, Mapping)
    ]
    assigned = {
        str(qa.get("query_id")): str(qa.get("assigned_engine"))
        for qa in qas
        if qa.get("query_id") and qa.get("assigned_engine")
    }
    ctx = None
    if any(c.query_id not in assigned for c in found):
        # Only a query routing did not place is scored here (the fallback).
        ctx = ScoringContext.from_pipeline(
            {e: a for e, a in analyses.items() if a}, triage, collector
        )
        if assignment:
            ctx = ctx.with_sources_of_truth(assignment_sources_of_truth(assignment))
    counts = settled_counts(qas, exclude=RoutineFollowers.from_collector(collector).routine_of)
    settled = settle(
        found,
        queries,
        ctx,
        assigned=assigned,
        current_engines=set(assigned.values()),
        settled=counts,
        source_engine=source_database_engine(collector or {}),
    )
    return reconcile(analyses, settled), settled
