"""Aggregate benchmark case runs. Infra failures never count as wrong answers."""

from __future__ import annotations

from dataclasses import dataclass

from benchmarks.runner.models import Outcome, ScoreResult


@dataclass
class CaseRun:
    case_id: str
    outcome: Outcome
    score: ScoreResult | None  # present only when outcome == SCORED


def build_report(runs: list[CaseRun]) -> dict:
    """Aggregate over SCORED cases only; quarantine throttled/errored/skipped."""
    scored = [r for r in runs if r.outcome is Outcome.SCORED and r.score is not None]
    throttled = [r for r in runs if r.outcome is Outcome.THROTTLED]
    errored = [r for r in runs if r.outcome is Outcome.ERRORED]
    skipped = [r for r in runs if r.outcome is Outcome.SKIPPED]

    total_scored_q = sum(r.score.scored_count for r in scored)
    total_ok_q = sum(r.score.acceptable_count for r in scored)
    agg = sum(r.score.acceptable_accuracy for r in scored) / len(scored) if scored else 0.0

    complete = not throttled and not errored
    banner = ""
    if not complete:
        bad = [r.case_id for r in throttled + errored]
        banner = (
            f"⚠️  INCOMPLETE RUN — {len(bad)}/{len(runs)} cases did not produce a "
            f"scorable result (throttled/errored).\n"
            f"    Scores below cover {len(scored)} scored cases only.\n"
            f"    Re-run before trusting these numbers: {', '.join(bad)}"
        )

    return {
        "complete": complete,
        "banner": banner,
        "scored_cases": len(scored),
        "throttled_cases": len(throttled),
        "errored_cases": len(errored),
        "skipped_cases": len(skipped),
        "total_scored_queries": total_scored_q,
        "total_acceptable_queries": total_ok_q,
        "aggregate_acceptable_accuracy": round(agg, 4),
        "per_case": [
            {
                "case_id": r.case_id,
                "outcome": r.outcome.value,
                "acceptable_accuracy": (round(r.score.acceptable_accuracy, 4) if r.score else None),
            }
            for r in runs
        ],
    }
