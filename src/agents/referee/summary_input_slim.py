"""Drop what the executive-summary instruction never reads from its request (#1064).

``prepare_synthesis_llm_input`` hands the model the deterministic synthesis
result's views. Several carry fields the summary instruction
(``src/skills/executive-summary.md``) never uses or forbids outright: fit and
confidence scores ("No confidence scores"), costs ("Do not mention cost"), the
64-hex query IDs of every risk and trade-off, each engine's capability text
twice, and the long prose of each wave. They made up most of the request's
fixed part: the part the per-list cap (``summary_input_budget``) cannot
shrink, so a workload with many designed tables stayed over the budget even
with every list cut to zero items.

:func:`slim_summary_input` runs first, on the payload ``prepare_synthesis_llm_input``
builds, and only removes or replaces fields; every figure it keeps is copied
as is, so no number in the request changes. What it drops is read from
nowhere else: the summary post-checks (``apply_synthesis_llm_output``) read the
full deterministic result, and the deliverables render ``report.json``, never
this payload.

- ``ranking`` keeps an allowlist (``RANKING_KEEP``): the engine, its role,
  workload share, query and designed-table counts. A new ranking field is
  therefore not sent to the model until it is added there.
- ``migration_waves`` loses ``rationale``, ``gate`` and ``gate_parts`` (prose
  the summary does not quote; the order, engines, counts and shares stay, and
  ``share_basis`` stays on every wave: it is the unit of the wave's share,
  queries or calls).
- A risk, resolved risk or trade-off carries ``query_count`` (how many
  queries) instead of the ``query_ids`` list; the IDs mean nothing to a
  reader of a 3-4 sentence summary.
- A ``design.carried_writes`` risk (#1362) carries ``detail_count`` instead of its
  ``details`` (one long approach per statement); its description already counts the
  statements by intent. Every other risk keeps its ``details``.
- A trade-off's ``target_tables`` is dropped when it only repeats the bare
  names of its ``source_tables``.
- A table mapping's alternatives are the engine names, without scores.
"""

from __future__ import annotations

import copy
from typing import Any

RANKING_KEEP = (
    "target",
    "role",
    "workload_percent",
    "assigned_queries",
    "assigned_queries_in_scope",
    "schema_design_available",
    "target_tables",
    "access_patterns",
    "pattern_groups",
)
WAVE_DROP = ("rationale", "gate", "gate_parts")
# Risks whose details are one long text per statement: the summary gets their count.
SUMMARISED_DETAIL_RULES = frozenset({"design.carried_writes"})


def _bare(table: object) -> str:
    return str(table).rsplit(".", 1)[-1]


def _ids_to_count(entry: dict[str, Any]) -> None:
    """Replace ``query_ids`` by how many there are, once, in place."""
    if isinstance(entry.get("query_ids"), list):
        entry["query_count"] = len(entry.pop("query_ids"))


def slim_summary_input(payload: dict) -> dict:
    """A copy of ``payload`` without the fields the summary does not read."""
    out = copy.deepcopy(payload)

    out["ranking"] = [
        {k: r[k] for k in RANKING_KEEP if k in r}
        for r in out.get("ranking") or []
        if isinstance(r, dict)
    ]

    for w in out.get("migration_waves") or []:
        for name in WAVE_DROP:
            w.pop(name, None)

    risk = out.get("risk_assessment") or {}
    for section in ("risks", "resolved_risks"):
        for r in risk.get(section) or []:
            _ids_to_count(r)
            if r.get("rule") in SUMMARISED_DETAIL_RULES and isinstance(r.get("details"), list):
                r["detail_count"] = len(r.pop("details"))

    for t in out.get("trade_offs") or []:
        _ids_to_count(t)
        sources = {_bare(s) for s in t.get("source_tables") or []}
        targets = t.get("target_tables")
        if isinstance(targets, list) and targets and {_bare(x) for x in targets} <= sources:
            del t["target_tables"]

    for m in out.get("table_mappings") or []:
        m.pop("confidence_score", None)
        alts = m.get("alternatives")
        if isinstance(alts, list):
            m["alternatives"] = [a.get("database") if isinstance(a, dict) else a for a in alts]

    return out
