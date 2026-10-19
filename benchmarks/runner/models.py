"""Data models for benchmark scoring and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    SCORED = "scored"
    THROTTLED = "throttled"
    ERRORED = "errored"
    SKIPPED = "skipped"


@dataclass
class QueryVerdict:
    query_id: str
    assigned: str | None  # None if the agent produced no assignment for this query
    acceptable: bool
    ideal_match: bool


@dataclass
class ScoreResult:
    scored_count: int
    acceptable_count: int
    ideal_defined_count: int
    ideal_count: int
    per_query: list[QueryVerdict]
    unmatched: list[str]  # query ids assigned by the agent but absent from the key
    # Stage-specific accuracies beside the main one, e.g. the anti_pattern stage's
    # ``verdict_accuracy`` and ``engine_accuracy`` (#1084); reported per case.
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def acceptable_accuracy(self) -> float:
        return self.acceptable_count / self.scored_count if self.scored_count else 0.0

    @property
    def ideal_accuracy(self) -> float:
        return self.ideal_count / self.ideal_defined_count if self.ideal_defined_count else 0.0
