"""The resolver's optional per-query routing trace (#497)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping


class RoutingTrace:
    """Opt-in sink for ``AssignmentResolver.resolve()``'s per-query score ladder (#497).

    Pass an instance as ``resolve(..., routing_trace=...)`` to have the resolver record,
    for every query, each candidate engine's score -- always ``scores[qid][engine]``,
    the value ``resolve()`` itself used, never a recompute -- plus ``base`` (a fresh,
    pure ``compute_query_confidence`` call) and ``penalty`` as diagnostics, and ``adj``
    when the used score disagrees with ``max(0, base - penalty)`` (there should never
    be a gap, but a future change could open one; recording it, rather than raising,
    means the snapshot's diff is what surfaces it instead of a crash), or the hard
    exclusion/capability/write gate that zeroed it instead. Also records the engine
    ``resolve()`` picked and why, the co-dependency group a query belonged to (if any,
    via ``record_group``) and the tie-break rule that decided it (if any, via
    ``record_tie``). Default is ``routing_trace=None`` (nothing recorded): every method
    here only reads values ``resolve()`` already computed (or recomputes the pure,
    side-effect-free ``compute_query_confidence`` as a diagnostic), so passing a
    ``routing_trace`` never changes ``scores``, ``assigned`` or the returned
    ``Assignment``. Built for ``tests/e2e/routing_snapshot.py``; see that module for
    the schema of what gets recorded.
    """

    def __init__(self) -> None:
        self.by_query: dict[str, dict] = {}
        # query_id -> {"key": <12-hex group key>, "scores": {engine: group_score}}
        self.group_by_query: dict[str, dict] = {}
        # query_id -> the structured tie_kind ("source_compatible" | "tie_unresolved")
        # break_owner_tie resolved it with (#503: a customer-facing sentence should
        # never be the thing a test parses to find out what happened, so this stores
        # the structured kind directly rather than break_owner_tie's prose "why").
        self.tie_break_by_query: dict[str, str] = {}
        # Every non-tied query's engine, snapshotted once at Step 6b (#503) --
        # persisted here so the reality check reuses the SAME settled-workload
        # counts the resolver's own tie-break used, instead of recomputing a
        # different one later from the final v1 assignment.
        self.settled_counts: dict[str, int] = {}

    def record(self, query_id: str, payload: dict) -> None:
        self.by_query[query_id] = payload

    def record_group(self, group: Iterable[str], group_score: Mapping[str, float]) -> None:
        """A co-dependency group's membership and every candidate's group score.

        Called once per group in ``resolve()``'s Step 5, before a tie (if any) is
        resolved -- ``group_score`` is every candidate engine's score for the whole
        group (the mean ``resolve()`` actually compared), not just the winner's.
        """
        members = sorted(group)
        key = hashlib.sha256(",".join(members).encode()).hexdigest()[:12]
        payload = {"key": key, "scores": {e: round(s, 2) for e, s in group_score.items()}}
        for qid in members:
            self.group_by_query[qid] = payload

    def record_tie(self, query_id: str, tie_kind: str) -> None:
        """The structured ``tie_kind`` ``break_owner_tie`` resolved ``query_id`` with."""
        self.tie_break_by_query[query_id] = tie_kind

    def record_settled_counts(self, counts: Mapping[str, int]) -> None:
        """The resolver's own settled-workload snapshot (#503) -- see the
        attribute's own docstring."""
        self.settled_counts = dict(counts)
