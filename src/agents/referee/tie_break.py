"""Shared ADR-031 Decision 2 tie-break for an exact top-score tie (#503).

ADR-031 Decision 2: "On an exact tie, the source-compatible relational
engine wins; no other engine preference exists." Three places in the
routing code pick a winner among engines tied for the same top score for a
query (or, for a co-dependency group, the group): the first assignment's
owner tie-break (``assignment_resolver.break_owner_tie``), the reality
check's placement step (``reality_check._pick_winner``), and the reality
check's mandatory-engine-floor absorber (``reality_check.
_find_best_absorber_for_query`` and its co-dependency-group counterpart).
Before #503 each of those three decided a *non-Aurora* tie its own way --
table traffic, query count, or alphabetical name -- none of which is a
reason ADR-031 recognizes for preferring one engine over another: they
describe how busy an engine already is, not how well it serves this query.
This module is the one place that logic lives now, so the three callers
cannot drift back out of agreement with each other or with ADR-031.

The rule, in order:

1. the source-compatible engine (the Aurora engine matching the source
   database's dialect), when it is one of the tied engines --
   ``tie_kind="source_compatible"`` (ADR-031 Decision 2);
2. otherwise, an engine already serving the query today (or, for a
   co-dependency group, serving any of its members today), when it is one
   of the tied engines -- a tie is not "strictly higher," so it is never
   an opportunity for some other engine to pick up a query it did not
   already have -- ``tie_kind="stability"``. The first assignment has no
   "today" to be stable about (nothing is assigned yet), so
   ``current_engines`` is always empty there and this branch never fires;
   it only ever fires in the reality check, where a query already has an
   assigned engine before this tie is resolved. If a co-dependency group's
   members sit on two (or more) DIFFERENT engines that are both still
   tied, there is no single "the" current engine to be stable about --
   that is reported as unresolved too (rule 3), never settled by picking
   whichever of those current engines happens to sort first by name;
3. otherwise there is no ADR-031 rule for this tie: ``tie_kind=
   "tie_unresolved"`` is reported honestly, and ``pick_unresolved_tie_
   placeholder`` below -- a maintainer-decided placeholder, not an
   ADR-031 rule -- still returns a deterministic engine so the query has
   somewhere to sit pending the customer's choice.

Table traffic, how many queries an engine already has, and an engine's
name are never inputs to this decision -- not as the deciding factor, and
not as a quieter tiebreaker buried under a different name. Drift between
runs on the same ADR-031 rule set is a bug; drift between a routing change
and a prior run because a real rule changed is expected and reported, not
suppressed here.

:func:`settled_counts` (#550, #549) computes rule 1's settled-workload-share
input from a finished list of query assignments, for a caller with no live
resolver snapshot to reuse directly.

:func:`source_compatible_engine_survives` (#600, ADR-031 Decision 13)
carries rule 1 over to the reality check's choice of which engine to REMOVE:
Phase 1 keeps a source-compatible engine that scores every one of its
queries equal to or higher than its best other committed engine (an exact
tie, or a margin too small to count as unique value), unless that other
engine is cheaper by both thresholds below. When the source-compatible
engine scores lower on any one of its queries, the existing unique-value and
burden rules apply unchanged, and standing cost can still remove it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

TieKind = str  # one of TIE_KINDS, below

# The three literal values ``break_tie`` returns as ``tie_kind``, named here
# once so a caller that needs the full set (today, only
# ``src.tools.validation.customer_text_guard``'s snake_case-reason-code
# detector, #587 round 4: a known INTERNAL code is never confused with a
# column name that happens to precede a colon in model-written prose, e.g.
# "ordered by meta_id: one Query per comment_id") imports it instead of
# retyping the three strings -- a new tie_kind added to ``break_tie`` below is
# then picked up automatically, with no second edit anywhere else.
TIE_KIND_SOURCE_COMPATIBLE = "source_compatible"
TIE_KIND_STABILITY = "stability"
TIE_KIND_UNRESOLVED = "tie_unresolved"
TIE_KINDS: tuple[str, ...] = (TIE_KIND_SOURCE_COMPATIBLE, TIE_KIND_STABILITY, TIE_KIND_UNRESOLVED)

# Maintainer decision 2026-10-13 (#600, ADR-031 Decision 13): the
# source-compatible engine is not removed in favour of an engine that scores
# its queries no better, unless that engine is at least this much cheaper in
# standing cost, as a share of the source-compatible engine's standing cost ...
CHEAPER_ENGINE_MIN_SAVING_SHARE = 0.40
# ... AND saves at least this much a month (USD). Both must hold; each is
# inclusive at its boundary.
CHEAPER_ENGINE_MIN_SAVING_MONTHLY = 100.0


def cheaper_engine_displaces_source_compatible(
    source_compatible_cost: float, other_cost: float
) -> bool:
    """Whether ``other_cost`` is cheap enough, against the source-compatible
    engine's ``source_compatible_cost`` (both monthly standing costs), to take
    its place (#600, ADR-031 Decision 13).

    True only when the saving is at least ``CHEAPER_ENGINE_MIN_SAVING_SHARE``
    of the source-compatible engine's cost AND at least
    ``CHEAPER_ENGINE_MIN_SAVING_MONTHLY`` dollars.
    """
    if source_compatible_cost <= 0:
        return False
    saving = source_compatible_cost - other_cost
    return (
        saving >= CHEAPER_ENGINE_MIN_SAVING_MONTHLY
        and saving / source_compatible_cost >= CHEAPER_ENGINE_MIN_SAVING_SHARE
    )


def source_compatible_engine_survives(
    source_compatible_cost: float,
    best_alternative_costs_per_query: Iterable[Iterable[float]],
) -> bool:
    """The one rule for keeping the source-compatible engine in Phase 1 (#600,
    ADR-031 Decision 13).

    The caller has already established that the source-compatible engine
    scores every one of its queries equal to or higher than any alternative.
    ``best_alternative_costs_per_query`` holds, per query, the standing costs
    of that query's best-scoring alternatives. The engine is removed only when
    EVERY query has at least one such alternative that
    :func:`cheaper_engine_displaces_source_compatible`; otherwise it survives.
    """
    return not all(
        any(
            cheaper_engine_displaces_source_compatible(source_compatible_cost, cost)
            for cost in costs
        )
        for costs in best_alternative_costs_per_query
    )


def settled_counts(query_assignments: Iterable[Any]) -> dict[str, int]:
    """Each engine's share of the SETTLED workload (ADR-031 Decision 12), from
    a finished list of query assignments rather than a live resolver run (#550,
    #549).

    Counts a query toward its ``assigned_engine`` only when its own
    ``tied_engines`` is empty -- never a ``tie_unresolved`` query, and never
    another placeholder pick -- the exact rule
    :func:`pick_unresolved_tie_placeholder` already applies to the resolver's
    own in-flight ``settled_counts`` snapshot (``assignment_resolver.py``'s
    Step 6b). A caller that already has that live snapshot (the resolver
    itself, mid-``resolve()``) should keep using it directly; this helper is
    for every other caller that only has a finished assignment list to work
    from -- the Reality Check LLM-correction re-run, the consolidation
    validator's sanity sweep and corrections, ``derive_table_assignments``'s
    global fallback -- so they compute the SAME settled count from it,
    instead of a raw ``Counter`` over every current assignment (which
    includes placeholder ties and reality-check moves, and so is not
    "settled" at all).

    Each entry in ``query_assignments`` may be a ``QueryAssignment`` or a
    plain dict (the same dual shape ``assignment_resolver.retained_engine_for``
    already accepts), so this works on raw collector-shaped assignments and
    on validated contracts alike.
    """
    counts: dict[str, int] = {}
    for qa in query_assignments:
        if isinstance(qa, Mapping):
            engine = qa.get("assigned_engine")
            tied = qa.get("tied_engines")
        else:
            engine = getattr(qa, "assigned_engine", None)
            tied = getattr(qa, "tied_engines", None)
        if engine and not tied:
            counts[engine] = counts.get(engine, 0) + 1
    return counts


def _settled_workload_leaders(tied: list[str], settled_counts: Mapping[str, int]) -> list[str]:
    """``tied`` engines with the highest ``settled_counts`` value (ties kept)."""
    counts = {e: settled_counts.get(e, 0) for e in tied}
    top = max(counts.values())
    return sorted(e for e in tied if counts[e] == top)


def pick_unresolved_tie_placeholder(
    tied: list[str],
    settled_counts: Mapping[str, int],
    query_id: str,
) -> tuple[str, bool]:
    """Placeholder pick for a tie ADR-031 does not resolve (#503).

    THIS IS A PLACEHOLDER, not an ADR-031 rule: ADR-031 Decision 2 says
    nothing about how to pick among engines tied with no source-compatible
    engine and no stable "today" to fall back on. A maintainer decided this
    specific placeholder on 2026-10-12 (ADR-031 Decision 12, PR #545 review)
    so the query still lands somewhere deterministic while it waits for the
    customer's actual choice; a future ADR-031 amendment may replace it.

    The pick, in order:

    1. the tied engine that already carries more of the SETTLED workload --
       ``settled_counts[engine]``, the number of queries this run decided
       WITHOUT a tie (never a ``tie_unresolved`` query, and never another
       placeholder pick, so this count cannot be influenced by how other
       ties in the same run happen to resolve, which would make the result
       depend on evaluation order). The reality check's whole job is a CTO
       consolidation review that prefers fewer, larger engines over more,
       smaller ones -- so when a tie must still be put somewhere, putting
       it with the engine that already does more of the real work is
       consolidation-consistent, not a stylistic preference for one engine's
       name or brand;
    2. otherwise (the settled-workload count is ALSO exactly tied, including
       the common case where neither tied engine has any settled workload
       yet) a stable hash of the query id and the candidate engine name --
       deterministic for a given query and tied set, but not a fixed
       preference for one engine's name across every query or every
       workload the way an alphabetical sort is.

    Returns ``(winner, by_workload_share)``: ``by_workload_share`` is
    ``True`` when rule 1 decided it, ``False`` when rule 2 (the hash) did --
    so a caller building a customer-facing sentence can say truthfully which
    basis decided THIS placeholder, instead of always crediting "workload."
    """
    leaders = _settled_workload_leaders(tied, settled_counts)
    if len(leaders) == 1:
        return leaders[0], True
    ranked = sorted(
        leaders, key=lambda engine: hashlib.sha256(f"{query_id}:{engine}".encode()).hexdigest()
    )
    return ranked[0], False


def break_tie(
    tied: list[str],
    current_engines: set[str],
    source_compatible_engine: str | None,
    settled_counts: Mapping[str, int] | None = None,
    query_id: str = "",
) -> tuple[str, TieKind]:
    """Resolve an exact tie among ``tied`` (expected length >= 2); see the
    module docstring for the three-rule order.

    ``settled_counts`` and ``query_id`` feed ``pick_unresolved_tie_
    placeholder`` when rule 3 fires; both are optional (default empty/``""``)
    so a caller that does not yet track settled workload share still gets a
    deterministic, if coarser, placeholder pick.

    Returns ``(winner, tie_kind)``. Callers that also need to detect
    *whether* there is a tie at all (``_pick_winner`` does; the resolver's
    ``break_owner_tie`` and the reality check's absorber callers are only
    ever invoked once a tie is already known to exist) do that themselves
    before calling this function -- it always resolves one.
    """
    if source_compatible_engine and source_compatible_engine in tied:
        return source_compatible_engine, TIE_KIND_SOURCE_COMPATIBLE
    staying = sorted(set(tied) & current_engines)
    if len(staying) == 1:
        return staying[0], TIE_KIND_STABILITY
    winner, _ = pick_unresolved_tie_placeholder(tied, settled_counts or {}, query_id)
    return winner, TIE_KIND_UNRESOLVED
