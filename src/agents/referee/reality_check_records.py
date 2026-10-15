"""Reality check consolidation records: fit them to the net per-query moves (#218)."""

from __future__ import annotations

from collections import defaultdict

from src.shared.engine_elimination import in_scope_distribution
from src.shared.engine_names import display_engine


def _queries(n: int, engine: str = "") -> str:
    """``"1 query"`` / ``"3 queries"`` (``"3 documentdb queries"`` with ``engine``).

    Local on purpose: the report layer's ``plural_noun`` sits above the agents, and
    importing it here would invert the layering.
    """
    noun = "query" if n == 1 else "queries"
    return f"{n} {engine} {noun}" if engine else f"{n} {noun}"


def reconcile_consolidations(
    before_assignments: list[dict],
    after_assignments: list[dict],
    consolidations: list[dict],
    unique_value_assessment: dict[str, dict] | None = None,
    engine_costs: dict[str, float] | None = None,
) -> list[dict]:
    """Make the consolidation records describe the net per-query moves (#218).

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
      states the final move.

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

    Record order is kept; new records follow. Inputs are not mutated.
    """
    before = {qa["query_id"]: qa["assigned_engine"] for qa in before_assignments}
    after_engine = {qa["query_id"]: qa["assigned_engine"] for qa in after_assignments}
    # The single rule ("no in-scope query remains") lives in one place
    # (src.shared.engine_elimination, #235); ``had_in_scope`` and ``remaining``
    # are its two halves, kept as separate names here because ``remaining``'s
    # per-engine counts are also used below for the "stays for N queries" text.
    had_in_scope = set(in_scope_distribution(before_assignments))
    remaining: dict[str, int] = defaultdict(int, in_scope_distribution(after_assignments))

    moves: dict[tuple[str, str], int] = {}
    moved_qids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for qa in after_assignments:
        src = before.get(qa["query_id"])
        dst = qa["assigned_engine"]
        if src and src != dst:
            moves[(src, dst)] = moves.get((src, dst), 0) + 1
            moved_qids[(src, dst)].add(qa["query_id"])

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
        claimed = full and not never_justified
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
                "retention_reason": record.get("retention_reason") if retained else None,
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
            lines.append(f"Removed {engine}: no saving claimed (never justified by the workload)")
        elif saving:
            moves = f"{len(records)} move{'s' if len(records) != 1 else ''}"
            lines.append(
                f"Removed {engine}: saves ~${saving:,.2f}/mo "
                f"(its analysed cost, counted once across {moves})"
            )
        else:
            lines.append(f"Removed {engine}: no analysed cost estimate to quote as a saving")
    return lines
