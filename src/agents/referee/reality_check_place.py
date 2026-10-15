"""Reality check Phase 2: place every query on its best settled engine (#502)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    best_other,
    ledger_for,
)
from src.agents.referee.reality_check_records import engine_changes
from src.agents.referee.reality_check_rules import (
    _group_eligible_engines,
    _move_scores_text,
    _pick_winner,
    _resolver_score,
    _tie_unresolved_note,
    _top_scorers,
    _trace_ladder,
    tie_reason_code,
)
from src.contracts.move_ledger import MoveEvidence, MovePass, ReasonCode
from src.shared.engine_names import display_engine

# The rules of Decision 12's placeholder: the tie stayed unresolved.
_PLACEHOLDER_TIE_RULES = frozenset(
    {ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD, ReasonCode.TIE_PLACEHOLDER_HASH}
)


def _phase2_reason(
    current_engine: str,
    winner: str,
    eligible_scores: dict[str, float],
    tie_kind: str | None,
    *,
    moved: bool,
    group: bool,
    original_tied: Iterable[str] | None = None,
    before_score: float | None = None,
) -> str:
    """Build a Phase 2 ``assignment_reason`` honestly (#503): engines named
    with ``display_engine``, no raw code and no internal ``ADR-031``
    reference, and rebuilt from the CURRENT verdict every time a tie is
    involved -- even when the engine does not change (a tie that clears
    while the query stays put must not keep reading an earlier "pending
    your choice" sentence written before it cleared). ``original_tied``
    (the engines this query was tied between before this phase ran) names
    why a now-cleared tie cleared, when it did -- which of those engines
    this phase removed from consideration -- rather than a generic "is now
    resolved" with no reason given."""
    winner_name = display_engine(winner)
    if moved:
        scores_text = _move_scores_text(
            current_engine, before_score, winner, eligible_scores.get(winner)
        )
        basis = f"co-dependency group moves together; {scores_text}" if group else scores_text
        tie_note = (
            _tie_unresolved_note(_top_scorers(eligible_scores), winner)
            if tie_kind == "tie_unresolved"
            else ""
        )
        return (
            f"reality check: consolidated from {display_engine(current_engine)} → "
            f"{winner_name} (no unique value — {basis}{tie_note})"
        )
    if tie_kind == "tie_unresolved":
        return (
            "reality check: no unique value elsewhere"
            f"{_tie_unresolved_note(_top_scorers(eligible_scores), winner)}"
        )
    removed = sorted(e for e in (original_tied or []) if e != winner and e not in eligible_scores)
    if removed:
        removed_text = ", ".join(display_engine(e) for e in removed)
        verb = "no longer qualifies" if len(removed) == 1 else "no longer qualify"
        return (
            f"reality check: {winner_name} is kept -- {removed_text} {verb} for this "
            "query, so the earlier scores tie is resolved"
        )
    return (
        f"reality check: {winner_name} is kept -- the earlier scores tie between "
        "engines is now resolved"
    )


def _place_settled_queries(
    query_assignments: list[dict],
    committed_engines: set[str],
    mandatory_query_ids: set[str],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    trace_by_query: dict[str, dict],
    source_compatible_engine: str | None,
    workload_justified: dict[str, bool | None],
    tie_unresolved_qids: set[str] | None = None,
    settled_counts: Mapping[str, int] | None = None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    ledger: MoveLedger | None = None,
) -> tuple[list[dict], list[dict]]:
    """Phase 2 (#502 review): place every query on its highest-scoring
    engine within the settled committed set, using the SAME per-query
    scores (``trace_by_query``, built once in ``run_reality_check`` and
    shared with Phase 1) and the SAME, group-aware eligibility rule
    (``_group_eligible_engines``) Phase 1 used to decide which engines
    earn a place -- never the reality check's own, separate fit scorer,
    and never a different eligibility answer than Phase 1 already relied
    on when it decided an engine was safe to remove.

    A query whose engine survived Phase 1 stays there unless another
    eligible, surviving engine scores it strictly higher (see
    ``_pick_winner`` for the tie order). A co-dependency group moves as
    one unit, by the SAME group score the resolver itself compared when it
    first placed the group, onto an engine ``_group_eligible_engines`` says
    is eligible for every member -- Phase 1 never removes an engine while
    one of its groups has no such survivor (#502 review round 5, finding
    G), so this should never come up empty; if it somehow did, the group
    stays exactly where it is (the same "should not happen" outcome as an
    individual query with nothing eligible), never split apart to force a
    placement. Because every query is placed fresh against the final
    committed set, not incrementally against a list that can go stale, no
    query should ever end up on a removed engine; ``run_reality_check``
    asserts that invariant right after this call rather than trusting it
    silently.

    ``tie_unresolved_qids``/``tied_engines_by_qid`` (#503 review, both
    rounds) mark queries whose FIRST-ASSIGNMENT engine is only a
    placeholder pick from an unresolved tie: that placeholder is never
    passed to ``_pick_winner`` as a stability candidate (a placeholder pick
    is not "today's real engine," so it earns no stability credit), and
    such a query is RE-RESOLVED HERE AMONG THE ORIGINALLY TIED ENGINES
    FIRST (round 2, findings C1/C2): if one or more of them are still
    committed and eligible, the candidate pool is restricted to just those
    -- never opened up to the full committed set while a tied engine still
    survives. Only when NONE of the originally tied engines survive does
    the query fall back to the full committed set, placed on its
    best-scoring survivor there with an honest reason, same as any other
    consolidation. Either way, every query this bookkeeping touches gets a
    FRESH ``tied_engines``/reason from the verdict just computed (round 2,
    findings I2/I3): a tie that clears, even while the engine does not
    change, is never left reading a stale "pending your choice" sentence,
    and a tie that first appears here (two non-source-compatible engines
    newly tying for the top score during consolidation) is recorded too,
    not silently dropped because it was never tied before.

    Every engine change goes through ``move_ledger.apply_to_assignment`` on
    ``ledger`` (#616; a fresh one from ``query_assignments`` when ``None``):
    pass ``settle`` when Phase 1 removed the query's engine, ``place`` when
    another kept engine scores it higher, with the before and after scores and
    the runner-up from the same scores this phase compared. A tie this phase
    clears or opens while the engine stays is a Move too (``from_engine ==
    to_engine``), so replaying the Moves rebuilds ``tied_engines`` as well.
    """
    tie_unresolved_qids = tie_unresolved_qids or set()
    tied_engines_by_qid = tied_engines_by_qid or {}
    revised = deepcopy(query_assignments)
    if ledger is None:
        ledger = ledger_for(revised)
    revised_by_id = {qa["query_id"]: qa for qa in revised}
    # This phase's Moves start here on the ledger; its records are built from
    # them (#616), not from a count kept beside them.
    first_move = len(ledger.moves)

    def _record(
        qa: dict,
        winner: str,
        scores: dict[str, float],
        tie_kind: str | None,
        tie_seed: str,
        *,
        tie_relevant: bool,
        score_before: float | None,
        group: dict | None = None,
    ) -> None:
        """Set ``qa``'s engine (and, when a tie is involved, its tie) through the ledger.

        Records nothing when neither the engine nor the tie changes. A tie
        that is not involved keeps the query's current ``tied_engines``.
        """
        current = qa["assigned_engine"]
        new_tied = (
            (_top_scorers(scores) if tie_kind == "tie_unresolved" else []) if tie_relevant else None
        )
        if winner == current and (
            new_tied is None or new_tied == list(qa.get("tied_engines") or [])
        ):
            # Neither the engine nor the tie changes: no Move and no write.
            return
        removed = current not in committed_engines
        # One rule for every reality-check pass (move_ledger contract 1.1): an
        # engine change is coded by why the query left, with a deciding tie in
        # evidence; a tie-only change is coded by the tie's verdict.
        tie_rule = (
            tie_reason_code(tie_kind, _top_scorers(scores), settled_counts, tie_seed)
            if tie_kind is not None
            else None
        )
        if winner == current:
            reason = tie_rule if new_tied and tie_rule is not None else ReasonCode.TIE_CLEARED
        elif removed:
            reason = ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE
        else:
            reason = ReasonCode.HIGHEST_SCORE
        apply_to_assignment(
            ledger,
            qa,
            winner,
            pass_=MovePass.SETTLE if removed else MovePass.PLACE,
            reason_code=reason,
            score_before=score_before,
            score_after=scores.get(winner),
            runner_up=best_other(scores, winner),
            evidence=MoveEvidence(
                candidates=_trace_ladder(trace_by_query, qa["query_id"]),
                group_scores=dict(scores) if group else {},
                tie_rule=tie_rule,
            ),
            group_key=group["key"] if group else None,
            tied_engines=new_tied,
        )

    def _candidate_pool(original_tied: list[str] | None) -> set[str]:
        """The engines Phase 2 may place this query/group on: the
        originally tied engines that are still committed, FIRST (#503) --
        the full committed set only once none of them survive at all."""
        if not original_tied:
            return committed_engines
        survivors = {e for e in original_tied if e in committed_engines}
        return survivors or committed_engines

    def _place_individual(qid: str, qa: dict) -> None:
        """Place one (ungrouped, or mandatory-stripped) query on its
        highest-scoring committed, eligible engine."""
        current_engine = qa["assigned_engine"]
        original_tied = tied_engines_by_qid.get(qid) if qid in tie_unresolved_qids else None
        eligible_engines = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            _candidate_pool(original_tied),
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        eligible_scores: dict[str, float] = {
            e: score
            for e in eligible_engines
            if (score := _resolver_score(trace_by_query, qid, e)) is not None
        }
        if not eligible_scores:
            return  # nothing eligible at all; leave it where it is (should not happen)
        # A placeholder pick from an unresolved tie earns no stability credit
        # (#503 review item 2): it is not "today's real engine," so it is
        # never passed as a stability candidate here.
        stability_candidates = set() if original_tied else {current_engine}
        winner, tie_kind = _pick_winner(
            eligible_scores, stability_candidates, source_compatible_engine, settled_counts, qid
        )
        tie_relevant = bool(original_tied) or tie_kind == "tie_unresolved"
        _record(
            qa,
            winner,
            eligible_scores,
            tie_kind,
            qid,
            tie_relevant=tie_relevant,
            score_before=_resolver_score(trace_by_query, qid, current_engine),
        )
        if winner == current_engine:
            if tie_relevant:
                qa["assignment_reason"] = _phase2_reason(
                    current_engine,
                    winner,
                    eligible_scores,
                    tie_kind,
                    moved=False,
                    group=False,
                    original_tied=original_tied,
                )
            return
        qa["assignment_reason"] = _phase2_reason(
            current_engine,
            winner,
            eligible_scores,
            tie_kind,
            moved=True,
            group=False,
            original_tied=original_tied,
            before_score=_resolver_score(trace_by_query, qid, current_engine),
        )

    handled_groups: set[str] = set()
    for qid, payload in trace_by_query.items():
        qa = revised_by_id.get(qid)
        if qa is None or qid in mandatory_query_ids:
            continue  # unknown query, or protected by a mandatory signal override (ADR #296)

        group_info = payload.get("group")
        if group_info:
            group_key = group_info["key"]
            if group_key in handled_groups:
                continue
            handled_groups.add(group_key)
            members = [
                (mqid, revised_by_id[mqid]["assigned_engine"])
                for mqid, mpayload in trace_by_query.items()
                if (mgroup := mpayload.get("group")) and mgroup["key"] == group_key
                if mqid in revised_by_id and mqid not in mandatory_query_ids
            ]
            if not members:
                continue
            # A tied group's members all carry the SAME tied_engines (the
            # resolver resolves a group's tie once, for every member --
            # ``assignment_resolver``'s Step 6b); use the group's own
            # trigger query's tied set as the group's.
            group_original_tied = (
                tied_engines_by_qid.get(qid) if qid in tie_unresolved_qids else None
            )
            # The same group-aware eligibility rule Phase 1 used to decide
            # this group's current engine was safe to keep or remove (#502
            # review round 5, finding G) -- not a second, Phase-2-only
            # notion of "eligible for the group" that could disagree with
            # Phase 1's.
            eligible_group_engines = _group_eligible_engines(
                qid,
                trace_by_query,
                current_engine_by_qid,
                _candidate_pool(group_original_tied),
                mandatory_query_ids,
                query_signals,
                query_map,
                query_capabilities,
                tied_engines_by_qid,
            )
            eligible = {
                e: s for e, s in group_info["scores"].items() if e in eligible_group_engines
            }
            if not eligible:
                # Should not happen: Phase 1 never removes an engine while
                # one of its co-dependency groups has no eligible survivor
                # for every member (finding G) -- so this group's current
                # engine is always still committed and always eligible for
                # it. Leave the group exactly where it is, the same
                # "should not happen" outcome ``_place_individual`` uses,
                # never split apart to force a placement.
                continue
            # A member's current engine earns stability credit only when it
            # is not itself a placeholder pick from an unresolved tie (#503
            # review items 2 and 3): two different members sitting on two
            # different, still-tied engines is not "the" current engine
            # either way -- ``break_tie`` reports that as unresolved on its
            # own once ``current_engines`` holds more than one tied engine.
            current_engines = {e for mqid, e in members if mqid not in tie_unresolved_qids}
            # Sorted member ids, not ``qid`` (whichever member happens to come
            # first in ``trace_by_query``'s own iteration order) -- the same
            # stable seed the resolver (``assignment_resolver.py``'s Step 6b)
            # and the floor's group tie-break use, so Phase 2 can never
            # re-pick between the same two tied engines just because of input
            # order (#503).
            group_seed = ",".join(sorted(mqid for mqid, _ in members))
            winner, tie_kind = _pick_winner(
                eligible, current_engines, source_compatible_engine, settled_counts, group_seed
            )
            group_tie_relevant = bool(group_original_tied) or tie_kind == "tie_unresolved"
            for member_qid, member_engine in members:
                member_qa = revised_by_id[member_qid]
                # The group's verdict placed every member (#845): a member that
                # carries a tie of its own gets the group's tie (none when the
                # score decided), never its earlier one, even when the trigger
                # member had no tie.
                member_tied = list(member_qa.get("tied_engines") or [])
                member_tie_relevant = group_tie_relevant or bool(member_tied)
                _record(
                    member_qa,
                    winner,
                    eligible,
                    tie_kind,
                    group_seed,
                    tie_relevant=member_tie_relevant,
                    score_before=group_info["scores"].get(member_engine),
                    group=group_info,
                )
                if member_engine == winner:
                    if member_tie_relevant:
                        member_qa["assignment_reason"] = _phase2_reason(
                            member_engine,
                            winner,
                            eligible,
                            tie_kind,
                            moved=False,
                            group=True,
                            original_tied=group_original_tied or member_tied,
                        )
                    continue
                member_qa["assignment_reason"] = _phase2_reason(
                    member_engine,
                    winner,
                    eligible,
                    tie_kind,
                    moved=True,
                    group=True,
                    original_tied=group_original_tied or member_tied,
                    before_score=group_info["scores"].get(member_engine),
                )
            continue

        _place_individual(qid, qa)

    final_counts: dict[str, int] = defaultdict(int)
    for qa in revised:
        final_counts[qa["assigned_engine"]] += 1

    # Queries that stayed on an engine that lost at least one query this
    # phase -- either because no committed engine scores them as well (a
    # capability/ownership gate, or genuinely the best fit) or because they
    # are protected by a mandatory signal override. Reported once per
    # ``from_engine``, the same set on every one of its consolidation
    # records, matching the existing convention for a partial move.
    native_qids_by_engine: dict[str, list[str]] = defaultdict(list)
    for qa in query_assignments:
        native_qids_by_engine[qa["assigned_engine"]].append(qa["query_id"])
    phase_moves = ledger.moves[first_move:]
    moves = engine_changes(phase_moves)
    # An engine change an unresolved exact tie decided: its destination is the
    # placeholder pick (``evidence.tie_rule`` a ``tie.placeholder.*`` rule).
    # Not ``tied_engines``: a Move's tie is the tie it leaves, while this
    # counts the moves a tie decided (#845 clears a score-placed group's ties).
    tie_unresolved_moves = Counter(
        (m.from_engine, m.to_engine)
        for m in phase_moves
        if m.evidence.tie_rule in _PLACEHOLDER_TIE_RULES
        and m.from_engine is not None
        and m.from_engine != m.to_engine
    )
    moved_from: dict[str, set[str]] = defaultdict(set)
    for (frm, _to), qids in moves.items():
        moved_from[frm].update(qids)

    consolidations: list[dict[str, Any]] = []
    for (frm, to), qids in sorted(moves.items()):
        is_full = final_counts.get(frm, 0) == 0
        retained_qids = (
            [] if is_full else sorted(set(native_qids_by_engine.get(frm, [])) - moved_from[frm])
        )
        retained_mandatory = [q for q in retained_qids if q in mandatory_query_ids]
        retained_other = [q for q in retained_qids if q not in mandatory_query_ids]
        n_tie_unresolved = tie_unresolved_moves.get((frm, to), 0)
        consolidations.append(
            {
                "from_engine": frm,
                "to_engine": to,
                "query_count": len(qids),
                "reason": (
                    f"{display_engine(frm)} provides no unique capabilities — {len(qids)} "
                    f"queries can be served by {display_engine(to)}"
                    + (f" ({len(retained_qids)} retained)" if retained_qids else "")
                    + (
                        f" ({n_tie_unresolved} of them tied on score, with no routing rule "
                        "to decide)"
                        if n_tie_unresolved
                        else ""
                    )
                ),
                "saved_cost_estimate": 0,
                "action": "full" if is_full else "partial",
                "queries_retained": retained_qids,
                "retention_reason": (
                    "; ".join(
                        r
                        for r, present in (
                            ("Mandatory signal queries stay on their engine", retained_mandatory),
                            (
                                "No remaining engine scores these queries as well as this "
                                "one does",
                                retained_other,
                            ),
                        )
                        if present
                    )
                    if retained_qids
                    else None
                ),
                "workload_justified": workload_justified.get(frm),
            }
        )

    # No saving figure is set here (#793): ``consolidations_from_moves`` stores each
    # removed engine's analysed cost, once, on the engine's first record.

    # #502 review round 3: Phase 1 and Phase 2 must agree on eligibility --
    # verified here, not just assumed, since the earlier draft's two
    # different eligibility checks let a query silently stay on an engine
    # Phase 1 had already removed.
    stranded = sorted(
        qa["query_id"] for qa in revised if qa["assigned_engine"] not in committed_engines
    )
    if stranded:
        raise RuntimeError(
            f"reality check: {len(stranded)} queries ended on a removed engine "
            f"(Phase 1/Phase 2 eligibility disagreement): {stranded[:10]}"
        )

    return revised, consolidations
