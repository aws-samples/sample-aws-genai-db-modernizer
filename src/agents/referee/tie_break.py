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
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

TieKind = str  # "source_compatible" | "stability" | "tie_unresolved"


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
        return source_compatible_engine, "source_compatible"
    staying = sorted(set(tied) & current_engines)
    if len(staying) == 1:
        return staying[0], "stability"
    winner, _ = pick_unresolved_tie_placeholder(tied, settled_counts or {}, query_id)
    return winner, "tie_unresolved"
