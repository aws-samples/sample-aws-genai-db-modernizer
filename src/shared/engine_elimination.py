"""Single definition of "eliminated engine" (#235): no in-scope query remains on it, in one place.

Before this module existed, three places decided independently whether an
engine counted as eliminated, and two of them disagreed with the third:

- The reality-check handler's own ``eliminated_engines``
  (``reality_check_handler.write_reality_check_result``, used to drop stale
  per-query warnings for a consolidated-away engine) counted ANY query still
  assigned to the engine, in scope or out.
- The UI's ``eliminatedEngines`` (``AssignmentGate.js``) derived the same
  thing from the keys of ``before_distribution``/``after_distribution``,
  which count ANY query for the same reason.
- Synthesis grounding (``synthesis_grounding.eliminated_engines``) and the
  consolidation records (``reality_check_records.consolidations_from_moves``, #218)
  already counted only in-scope queries, per the rule: "An engine is
  eliminated when no in-scope query remains on it."

An engine left with only out-of-scope queries was therefore shown as active
by the handler and the UI, but eliminated by synthesis and the consolidation
records. This module is the one place that answers the question, computed
from the reality check's own settled result (the assignment it started
from, and the revised assignments it produced), so every consumer agrees.

``src.shared`` must not depend on ``src.agents`` (the dependency runs the
other way everywhere in this codebase), so this stays pure functions over
plain dicts -- no import of the reality-check or assignment modules.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping


def in_scope_distribution(query_assignments: Iterable[Mapping]) -> dict[str, int]:
    """Per-engine count of in-scope queries only.

    A query with no ``in_scope`` key is treated as in scope: the field was
    added after query assignments without it already existed.
    """
    counts: dict[str, int] = defaultdict(int)
    for qa in query_assignments:
        if not qa.get("in_scope", True):
            continue
        engine = qa.get("assigned_engine")
        if engine:
            counts[engine] += 1
    return dict(counts)


def eliminated_engines(
    before_assignments: Iterable[Mapping],
    after_assignments: Iterable[Mapping],
) -> set[str]:
    """The single rule (#235): no in-scope query remains on the engine.

    ``before_assignments`` is the per-query assignment list the reality check
    started from; ``after_assignments`` is its revised assignments (or any
    later assignment's ``query_assignments``, when a caller wants to re-check
    the rule against a more current state). An engine that never had an
    in-scope query in ``before_assignments`` is never reported as eliminated
    here -- it was never part of the active architecture to begin with,
    whatever ``after_assignments`` holds for it.
    """
    before_scope = in_scope_distribution(before_assignments)
    after_scope = in_scope_distribution(after_assignments)
    return {engine for engine in before_scope if after_scope.get(engine, 0) == 0}
