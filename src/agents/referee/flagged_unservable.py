"""Flagged queries no kept engine can take (#888), for ``report.json``.

Two kinds of unservable query never reach the move ledger, so the decision
trace (``query_traces.unservable``, #729) cannot find them:

- a reality-check model flag whose outcome is ``kept``: the review found the
  engine cannot serve the query, but no engine the check kept can take it, so
  it stays where it is (``reality-check/output.json`` ``flag_decisions``);
- a post-schema router terminal query: a design review flagged it and no kept
  engine can take it, or the cascade reached its last pass
  (``post-schema-router/router_output.json`` ``terminal``).

Synthesis records both here as one structured list, ``flagged_unservable``,
one row per query; the facts layer states them next to the fallback ones
(``report_facts.unservable_query_facts``, kind ``flagged``). A row holds only
for a query still on the flagged engine in the effective assignment and in
scope: a later move (a customer edit, a re-route) clears it. The model's own
explanation stays in the artifacts it came from; no deliverable renders it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agents.referee.query_scoring import GATE_KIND_MODEL_FLAG
from src.shared.engine_names import display_engine
from src.shared.flag_reasons import flag_reason_text
from src.shared.move_reasons import gate_text

SOURCE_REALITY_CHECK = "reality_check"
SOURCE_DESIGN_REVIEW = "design_review"

CAUSE_NO_KEPT_ENGINE = "no_kept_engine"
CAUSE_DEPTH_LIMIT = "depth_limit"


def flagged_unservable_explanation(source: str, cause: str, engine: str, category: str) -> str:
    """Why a flagged query stays on ``engine``, in customer-facing words."""
    name = display_engine(engine)
    who = "The reality check's review" if source == SOURCE_REALITY_CHECK else "A design review"
    found = f"{who} found {name} cannot serve this query ({flag_reason_text(category)})"
    if cause == CAUSE_DEPTH_LIMIT:
        return f"{found}; re-routing stopped at its last pass, so it stays on {name}."
    return f"{found}, and no other engine the plan keeps can take it, so it stays on {name}."


def flagged_unservable_records(
    assignment: Mapping[str, Any] | None,
    reality_check: Mapping[str, Any] | None,
    router_output: Mapping[str, Any] | None,
    query_capabilities: Mapping[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """``report.json``'s ``flagged_unservable`` rows, by engine then query id.

    One row per query: the reality check's ``kept`` flag decisions first, then
    the router's terminal queries for a query the check did not already list.
    A query is listed only while the effective ``assignment`` keeps it in
    scope on the flagged engine.
    """
    engine_of = {
        str(qa["query_id"]): qa.get("assigned_engine")
        for qa in (assignment or {}).get("query_assignments") or []
        if isinstance(qa, Mapping) and qa.get("query_id") and qa.get("in_scope", True)
    }
    caps = query_capabilities or {}
    rows: dict[str, dict[str, Any]] = {}

    def add(qid: str, engine: str, category: str, source: str, cause: str) -> None:
        if qid in rows or not engine or engine_of.get(qid) != engine:
            return
        rows[qid] = {
            "query_id": qid,
            "engine": engine,
            "engine_name": display_engine(engine),
            "source": source,
            "cause": cause,
            "reason_category": category,
            "reason_label": flag_reason_text(category),
            "explanation": flagged_unservable_explanation(source, cause, engine, category),
            "gate": GATE_KIND_MODEL_FLAG,
            "gate_text": gate_text(GATE_KIND_MODEL_FLAG) or "",
            "required_capabilities": sorted({str(c) for c in caps.get(qid) or []}),
        }

    for decision in (reality_check or {}).get("flag_decisions") or []:
        if not isinstance(decision, Mapping) or decision.get("outcome") != "kept":
            continue
        for qid in decision.get("query_ids") or []:
            add(
                str(qid),
                str(decision.get("flagged_engine") or ""),
                str(decision.get("reason_category") or "other"),
                SOURCE_REALITY_CHECK,
                CAUSE_NO_KEPT_ENGINE,
            )
    for term in (router_output or {}).get("terminal") or []:
        if not isinstance(term, Mapping) or not term.get("query_id"):
            continue
        cause = term.get("cause")
        add(
            str(term["query_id"]),
            str(term.get("flagged_engine") or ""),
            str(term.get("reason_category") or "other"),
            SOURCE_DESIGN_REVIEW,
            cause if cause == CAUSE_DEPTH_LIMIT else CAUSE_NO_KEPT_ENGINE,
        )
    return sorted(rows.values(), key=lambda r: (r["engine"], r["query_id"]))
