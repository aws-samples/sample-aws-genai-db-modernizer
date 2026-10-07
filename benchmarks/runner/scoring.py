"""Acceptable-set scoring for the assignment stage (stage-agnostic core shape)."""

from __future__ import annotations

from benchmarks.runner.models import QueryVerdict, ScoreResult


def score_assignment(actual: dict[str, str], expected: dict[str, dict]) -> ScoreResult:
    """Score {query_id: assigned_engine} against {query_id: {acceptable:[...], ideal?}}.

    - Only queries present in `expected` are scored (coverage).
    - A query in the key but missing from `actual` scores as not-acceptable
      (assigned=None) — a missing assignment is a miss, not a skip.
    - Queries in `actual` but absent from the key are reported in `unmatched`,
      never scored.
    """
    per_query: list[QueryVerdict] = []
    acceptable_count = 0
    ideal_defined = 0
    ideal_count = 0

    for qid, key in expected.items():
        assigned = actual.get(qid)
        acceptable_set = key.get("acceptable", [])
        is_acceptable = assigned is not None and assigned in acceptable_set
        ideal = key.get("ideal")
        if ideal is not None:
            ideal_defined += 1
        is_ideal = ideal is not None and assigned == ideal
        if is_acceptable:
            acceptable_count += 1
        if is_ideal:
            ideal_count += 1
        per_query.append(QueryVerdict(qid, assigned, is_acceptable, is_ideal))

    unmatched = sorted(set(actual) - set(expected))

    return ScoreResult(
        scored_count=len(expected),
        acceptable_count=acceptable_count,
        ideal_defined_count=ideal_defined,
        ideal_count=ideal_count,
        per_query=per_query,
        unmatched=unmatched,
    )
