"""Reality check records, derived from the move ledger (#218, #616).

Every engine change the reality check makes is a Move (``move_ledger.apply``),
so its records are built from the Moves rather than counted beside them:

- ``consolidations``: :func:`consolidations_from_moves` replays the check's
  Moves on its input and fits one record to each net (from, to) move. A pass
  still hands over its own records, for what a Move does not carry: the pass's
  customer-facing reason text (a Move carries a rule id, not prose), the
  queries a partial consolidation retained and why (a retained query did not
  change, so it has no Move), and the engine's first-assignment
  ``workload_justified`` verdict (an engine fact, taken before any Move). The
  saving is the removed engine's analysed cost (#793, #824), an engine fact as
  well, so it is passed in (``engine_costs``) and counted once per engine. A
  pass's own (from, to) counts come from its Moves too (:func:`engine_changes`).
- ``sweep_decisions``: a ``removed`` verdict and its records come from the
  sweep's Moves (:func:`sweep_outcome`). A ``kept`` verdict is the sweep
  deciding not to move anything, so there is no Move to derive it from: it is
  built where the sweep decides it.
- ``flag_decisions``: a ``moved`` verdict's destination and scores are its
  Move's, and the records a flag re-routes out of are found from the model-flag
  Moves (:func:`flag_moves_by_query`). A ``kept`` or ``ignored`` flag moved
  nothing, and its model text (category, explanation) is not routing evidence
  a Move should carry, so those stay where the flag is applied.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from src.agents.referee.move_ledger import replay
from src.contracts.move_ledger import Move, MovePass
from src.shared.engine_elimination import in_scope_distribution
from src.shared.engine_names import display_engine


def _queries(n: int, engine: str = "") -> str:
    """``"1 query"`` / ``"3 queries"`` (``"3 documentdb queries"`` with ``engine``).

    Local on purpose: the report layer's ``plural_noun`` sits above the agents, and
    importing it here would invert the layering.
    """
    noun = "query" if n == 1 else "queries"
    return f"{n} {engine} {noun}" if engine else f"{n} {noun}"


def engine_changes(moves: Iterable[Move]) -> dict[tuple[str, str], list[str]]:
    """Each (from, to) engine pair's moved query ids, in ``seq`` order.

    Only Moves that change an engine count: a first assignment (no
    ``from_engine``) and a Move that changes only the tie (``from_engine ==
    to_engine``) move nothing between engines.
    """
    pairs: dict[tuple[str, str], list[str]] = {}
    for move in sorted(moves, key=lambda m: m.seq):
        if move.from_engine is not None and move.from_engine != move.to_engine:
            pairs.setdefault((move.from_engine, move.to_engine), []).append(move.query_id)
    return pairs


def flag_moves_by_query(moves: Iterable[Move]) -> dict[str, tuple[str, str]]:
    """Each query a model flag re-routed: ``(the engine it left, where it went)``."""
    return {
        m.query_id: (m.from_engine, m.to_engine)
        for m in sorted(moves, key=lambda m: m.seq)
        if m.pass_ is MovePass.MODEL_FLAG and m.from_engine is not None
    }


def sweep_outcome(engine: str, count: int, moves: Iterable[Move]) -> tuple[list[dict], dict]:
    """The sanity sweep's records and ``removed`` verdict for ``engine``, from its Moves.

    ``moves`` are the Moves the sweep recorded while removing ``engine`` (only
    those leaving ``engine`` count); ``count`` is how many queries the engine
    had when the sweep evaluated it. One record per destination, in name order
    (each destination was already decided per query, by score and tie-break).
    """
    moved_counts = Counter(
        m.to_engine
        for m in moves
        if m.pass_ is MovePass.SWEEP and m.from_engine == engine and m.to_engine != engine
    )
    destinations = sorted(moved_counts)
    single_destination = len(destinations) == 1
    records: list[dict] = [
        {
            "from_engine": engine,
            "to_engine": to_engine,
            "query_count": moved_counts[to_engine],
            "reason": (
                f"Sanity sweep: {display_engine(engine)} had only {count} queries, "
                "each with a serviceable committed engine; "
                f"{moved_counts[to_engine]} scored best on {display_engine(to_engine)}"
            ),
            # The saving is set when the records are fitted to the Moves (#793).
            "saved_cost_estimate": 0,
            "action": "full" if single_destination else "partial",
            "queries_retained": [],
            "retention_reason": None,
        }
        for to_engine in destinations
    ]
    # One "removed" verdict per engine, not per destination: the
    # per-destination split is on the records (#589).
    dest_text = "; ".join(f"{moved_counts[d]} to {display_engine(d)}" for d in destinations)
    decision: dict = {
        "engine": engine,
        "decision": "removed",
        "reason": f"{count} queries, each with a serviceable committed engine: {dest_text}",
        "blocking_query_ids": [],
        "blocking_rule": None,
    }
    return records, decision


def consolidations_from_moves(
    before_assignments: list[dict],
    moves: Iterable[Move],
    consolidations: list[dict],
    unique_value_assessment: Mapping[str, Mapping] | None = None,
    engine_costs: Mapping[str, float] | None = None,
    *,
    engines_in_use: Iterable[str] = (),
) -> list[dict]:
    """The consolidation records of a reality check, from its Moves (#616).

    ``before_assignments`` is the assignment the check started from and
    ``moves`` the Moves it recorded (in ``seq`` order; a Move for a query not in
    ``before_assignments`` is ignored). Each query's final engine is the Moves
    replayed on ``before_assignments``, so no "after" list is needed and the
    records cannot disagree with the ledger. ``consolidations`` are the passes'
    own records, read only for what a Move does not carry (see the module
    docstring); the fitting rules are :func:`_fit_records`'s.
    """
    start = {
        qa["query_id"]: (qa["assigned_engine"], sorted(qa.get("tied_engines") or []))
        for qa in before_assignments
    }
    final = replay(moves, start=start)
    return _fit_records(
        before_assignments,
        [
            (qa["query_id"], final[qa["query_id"]][0], qa.get("in_scope", True))
            for qa in before_assignments
        ],
        consolidations,
        unique_value_assessment,
        engine_costs,
        engines_in_use,
    )


def _fit_records(
    before_assignments: list[dict],
    after: list[tuple[str, str, bool]],
    consolidations: list[dict],
    unique_value_assessment: Mapping[str, Mapping] | None,
    engine_costs: Mapping[str, float] | None,
    engines_in_use: Iterable[str] = (),
) -> list[dict]:
    """Fit the consolidation records to the net per-query moves (#218).

    :func:`consolidations_from_moves` is the only caller: ``after`` is each
    query's ``(query_id, engine, in_scope)`` once the check's Moves are
    replayed on ``before_assignments``.

    Pass 2, the LLM corrections, the post-correction re-settle, the sanity sweep and
    the customer-override restore each move queries and edit records on their own,
    so the records drifted from the assignment (unrecorded moves, overstated counts,
    savings for an engine that stays). This derives the moves from the before/after
    assignment and fits the records to them:

    - one record per (from, to) pair with a net move; its ``query_count`` is that move,
      so applying the records to the before distribution gives the after distribution;
    - a move with no record gets one; a record with no net move is dropped;
    - ``action`` is ``full`` only when the source engine had an in-scope query in the
      input and ends with none, and only then are savings claimed, once per engine;
    - a record whose count, action or retained queries changed gets a reason that
      states the final move;
    - a partial record lists the customer-pinned queries its source engine keeps in
      ``queries_retained`` and says so in its reason (#193), so it never reads as if
      the engine kept only queries the check chose to leave.

    ``unique_value_assessment`` (optional) carries each engine's
    ``cost_justification`` (#326, #167) and ``cost_justification_query_ids``:
    the justification-floor reason, and exactly which queries the floor
    itself moved, which this pass would otherwise discard whenever it
    rewrites a reason to describe the final move. The reason is appended
    only to a (src, dst) record whose own net-moved queries overlap that id
    set -- a record for a different move the floor never touched (a
    different target engine, or queries that moved for an unrelated reason)
    does not get the floor's text (a review of #375 found exactly this: the
    floor's reason attached to a 119-query record and to a 29-query record
    neither of which the floor had any part in).

    ``engine_costs`` (optional, #793) maps each engine to its own analysed
    ``cost_estimate.monthly_cost_usd``: the single saving figure a removed engine
    carries, the same one ``tco_analysis.eliminated_engine_costs`` publishes. It is
    stored once, on the engine's first record (ADR-031 Decision 7: counted once),
    and every record of that engine sets ``removal_saving_claimed`` so wording can
    attribute the saving to removing the engine, not to one of its moves. An engine
    without an analysed cost claims the removal but carries no dollar figure (0);
    the flat ``ENGINE_BASE_COST`` placeholder is never shown as a saving.

    ``engines_in_use`` (#949, ``src.shared.engines_in_use``) is what the source runs
    today. Removing any other engine -- a candidate the plan analysed and dropped --
    saves the customer nothing, so it claims no saving and carries no figure.

    Record order is kept; new records follow. Inputs are not mutated.
    """
    before = {qa["query_id"]: qa["assigned_engine"] for qa in before_assignments}
    after_engine = {qid: engine for qid, engine, _ in after}
    # Customer-pinned in-scope queries (#193): the check never moves them, so an
    # engine that keeps one is only partly consolidated, and its records say which.
    pinned = [
        qa["query_id"]
        for qa in before_assignments
        if qa.get("customer_override") and qa.get("in_scope", True)
    ]
    running = set(engines_in_use)
    # The single rule ("no in-scope query remains") lives in one place
    # (src.shared.engine_elimination, #235); ``had_in_scope`` and ``remaining``
    # are its two halves, kept as separate names here because ``remaining``'s
    # per-engine counts are also used below for the "stays for N queries" text.
    had_in_scope = set(in_scope_distribution(before_assignments))
    # The same rule as ``in_scope_distribution``, on the (id, engine, in_scope) rows.
    remaining: dict[str, int] = defaultdict(int)
    for _qid, engine, in_scope in after:
        if in_scope and engine:
            remaining[engine] += 1

    moves: dict[tuple[str, str], int] = {}
    moved_qids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for qid, dst, _ in after:
        src = before.get(qid)
        if src and src != dst:
            moves[(src, dst)] = moves.get((src, dst), 0) + 1
            moved_qids[(src, dst)].add(qid)

    first_record: dict[tuple[str, str], dict] = {}
    # Engine-level ``workload_justified`` verdict (#475 review round 2,
    # finding A), read off whichever input record for that ``from_engine``
    # happens to carry it -- every record a creation site builds for the
    # same engine already carries the identical, once-computed verdict
    # (``_workload_ever_justified``), so the first one found is as good as
    # any. ``None`` (no entry at all) stays the default below: not every
    # engine has a floor to judge it against.
    workload_justified_by_engine: dict[str, bool | None] = {}
    for c in consolidations:
        src = c["from_engine"]
        first_record.setdefault((src, c["to_engine"]), c)
        if src not in workload_justified_by_engine and "workload_justified" in c:
            workload_justified_by_engine[src] = c["workload_justified"]
    ordered = [p for p in first_record if p in moves] + [p for p in moves if p not in first_record]

    reconciled: list[dict] = []
    savings_claimed: set[str] = set()
    for src, dst in ordered:
        count = moves[(src, dst)]
        full = src in had_in_scope and remaining.get(src, 0) == 0
        action = "full" if full else "partial"
        record = dict(
            first_record.get((src, dst))
            or {"from_engine": src, "to_engine": dst, "queries_retained": []}
        )
        prior_retained = list(record.get("queries_retained") or [])
        retained = [] if full else [q for q in prior_retained if after_engine.get(q) == src]
        kept_by_customer = (
            []
            if full
            else [
                q
                for q in pinned
                if before.get(q) == src and after_engine.get(q) == src and q not in retained
            ]
        )
        retention_reason = record.get("retention_reason") if retained else None
        if kept_by_customer:
            pin_note = f"customer chose {display_engine(src)} for {', '.join(kept_by_customer)}"
            retention_reason = (
                f"{retention_reason}; the {pin_note}" if retention_reason else f"The {pin_note}"
            )
            retained = retained + kept_by_customer
        if (
            record.get("query_count") != count
            or record.get("action", "full") != action
            or retained != prior_retained
            or not record.get("reason")
        ):
            # #572: every customer-facing fragment below names the engine with
            # display_engine, never the raw id -- src/dst stay raw only as dict
            # keys (had_in_scope, remaining, moved_qids) above and below.
            src_name = display_engine(src)
            moved = f"{_queries(count, src_name)} moved to {display_engine(dst)}"
            if full:
                record["reason"] = f"{moved}; no in-scope {src_name} query remains"
            elif src not in had_in_scope:
                record["reason"] = (
                    f"Partial consolidation: {moved}; {src_name} had no in-scope query"
                )
            else:
                record["reason"] = (
                    f"Partial consolidation: {moved}; {src_name} stays for "
                    f"{_queries(remaining[src])}"
                    + (
                        f", {len(kept_by_customer)} of them the customer's choice"
                        if kept_by_customer
                        else ""
                    )
                )
            engine_assessment = (unique_value_assessment or {}).get(src, {})
            justification = engine_assessment.get("cost_justification")
            justification_qids = engine_assessment.get("cost_justification_query_ids")
            floor_overlap = (
                moved_qids[(src, dst)]
                if justification_qids is None
                else moved_qids[(src, dst)] & set(justification_qids)
            )
            if justification and floor_overlap:
                # Say how many of this record's own queries the floor actually
                # moved, not just the floor's reason text on its own -- a review
                # of #375 found the wording reading as if the floor's reason
                # applied to every query in the record (e.g. "119 ... moved to
                # aurora_postgresql ... (no search predicate ...)"), when the
                # floor itself only moved a handful of those 119.
                record["reason"] = (
                    f"{record['reason']} ({_queries(len(floor_overlap))} of these, "
                    f"moved by the justification floor: {justification})"
                )
        never_justified = workload_justified_by_engine.get(src) is False
        saved = 0.0
        # No saving when ``src`` was never justified by its own
        # FIRST-ASSIGNMENT workload (#475 review round 2, finding A/
        # suggestion): claiming a dollar figure here is exactly the
        # "Removes $X/mo" claim ``_build_recommendations`` already refuses
        # to make for this engine, and this field also reaches the
        # handler's own log line and the context graph
        # (``src/graph/populators.py``) directly, not only that prose.
        # #949: only an engine the source runs today can be retired for a saving.
        claimed = full and not never_justified and src in running
        if claimed and src not in savings_claimed:
            savings_claimed.add(src)
            saved = float((engine_costs or {}).get(src) or 0.0)
        record.update(
            {
                "query_count": count,
                "action": action,
                "saved_cost_estimate": saved,
                "removal_saving_claimed": claimed,
                "queries_retained": retained,
                "retention_reason": retention_reason,
                # Copied from the engine-level verdict, not just whatever
                # ``first_record`` happened to carry (#475 review round 2,
                # finding A): a (src, dst) pair this function synthesizes
                # fresh -- no input record ever described that exact
                # pair -- would otherwise have no ``workload_justified`` at
                # all, so ``never_justified_engines`` (synthesis_grounding.py)
                # would miss it and the TCO could list a saving again while
                # the prose already refuses to claim one.
                "workload_justified": workload_justified_by_engine.get(src),
            }
        )
        reconciled.append(record)
    return reconciled


def removal_log_lines(consolidations: list[dict]) -> list[str]:
    """The run-log lines for the final records: each move, then one line per engine.

    The saving is an engine-level figure (#793), so it is logged once per removed
    engine, not per move; a move line carries no dollar amount.
    """
    lines: list[str] = []
    by_engine: dict[str, list[dict]] = {}
    for c in consolidations:
        lines.append(
            f"Consolidated {c['query_count']} queries: {c['from_engine']} → {c['to_engine']}"
        )
        if c.get("action", "full") == "full":
            by_engine.setdefault(c["from_engine"], []).append(c)
    for engine, records in by_engine.items():
        saving = sum(r.get("saved_cost_estimate") or 0 for r in records)
        if not records[0].get("removal_saving_claimed", bool(saving)):
            why = (
                "never justified by the workload"
                if records[0].get("workload_justified") is False
                else "not running today; a candidate the plan considered"
            )
            lines.append(f"Removed {engine}: no saving claimed ({why})")
        elif saving:
            moves = f"{len(records)} move{'s' if len(records) != 1 else ''}"
            lines.append(
                f"Removed {engine}: saves ~${saving:,.2f}/mo "
                f"(its analysed cost, counted once across {moves})"
            )
        else:
            lines.append(f"Removed {engine}: no analysed cost estimate to quote as a saving")
    return lines
