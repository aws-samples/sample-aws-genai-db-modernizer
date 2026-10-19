"""Acceptable-set scoring for the assignment stages, and anti_pattern verdict scoring."""

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


def score_anti_pattern(actual: dict[str, dict], expected: dict[str, dict]) -> ScoreResult:
    """Score the anti_pattern stage (#1084): the pipeline's verdict against the key's.

    ``actual`` is ``{query_id: {"shows": bool, "engine": str | None}}``: whether the
    pipeline flagged the query with the case's anti-pattern, and its final engine.
    ``expected`` is ``{query_id: {"shows": bool, "acceptable"?: [...], "ideal"?: str}}``.

    A query is correct when the verdicts agree and, if the key gives an acceptable
    set, the final engine is in it. ``metrics`` reports the two halves apart:
    ``verdict_accuracy`` over every keyed query, ``engine_accuracy`` over the keyed
    queries with an acceptable set. A keyed query the stage did not return is a
    miss on both; a returned query absent from the key is ``unmatched``.
    """
    per_query: list[QueryVerdict] = []
    correct = ideal_defined = ideal_count = 0
    verdict_ok = engine_scored = engine_ok = 0

    for qid, key in expected.items():
        if not isinstance(key.get("shows"), bool):
            raise ValueError(f"{qid}: an anti-pattern key needs 'shows': true or false")
        got = actual.get(qid)
        engine = got.get("engine") if got else None
        same_verdict = got is not None and got.get("shows") is key["shows"]
        acceptable_set = key.get("acceptable")
        in_set = True
        if acceptable_set:
            engine_scored += 1
            in_set = engine is not None and engine in acceptable_set
            engine_ok += in_set
        verdict_ok += same_verdict
        is_correct = same_verdict and in_set
        correct += is_correct
        ideal = key.get("ideal")
        if ideal is not None:
            ideal_defined += 1
        is_ideal = ideal is not None and engine == ideal
        ideal_count += is_ideal
        per_query.append(QueryVerdict(qid, engine, is_correct, is_ideal))

    metrics = {"verdict_accuracy": verdict_ok / len(expected) if expected else 0.0}
    if engine_scored:
        metrics["engine_accuracy"] = engine_ok / engine_scored
    return ScoreResult(
        scored_count=len(expected),
        acceptable_count=correct,
        ideal_defined_count=ideal_defined,
        ideal_count=ideal_count,
        per_query=per_query,
        unmatched=sorted(set(actual) - set(expected)),
        metrics=metrics,
    )
