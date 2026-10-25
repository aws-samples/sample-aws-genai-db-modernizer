"""A fixed token budget for the executive-summary request (#762).

Since #628 (payload parity) Bedrock and headless (Claude Code) reason over the
same ``prepare_synthesis_llm_input`` payload. Its lists grow with the
workload -- one ``query_groups`` entry per group, every risk's table
lists, one trade-off per design decision, every table a wave or an engine
serves -- so with no limit a large enough workload overflows the model's context window, the Bedrock call fails,
and the summary silently falls back to the deterministic one.

:func:`apply_summary_input_budget` runs on that shared payload, after it is
built, so both modes still see identical data:

- Under the budget, the payload is returned unchanged and carries no
  ``input_budget`` field: nothing is capped, so nothing is reported.
- Over it, every growing list is cut to the same per-list limit, trying
  ``_LIST_LIMITS`` from the most generous down until the request fits. The
  order is deterministic and independent of the input order: query groups an
  engine's ``top_query_groups`` names first, then by ``query_count`` (then
  name); tables by how many in-scope queries touch them across the whole
  assignment, every engine's queries included (``table_query_weights``, then
  name); trade-offs by the queries they touch (``query_count``, then engine
  and description). Every cut list's own length is kept next to it as
  ``<field>_total`` (computed before the cut, ``TOTAL_FIELD_NAMES``), and
  ``input_budget`` lists every capped path with its totals, plus an "N more"
  aggregate for the dropped query groups (how many, and how many queries they
  carry). A log line says the cap applied.

The cap only works on lists, so the request's fixed part (what no cut removes)
must stay small on its own: ``summary_input_slim.slim_summary_input`` runs
before it and drops what the summary instruction never reads (#1064). A test
holds every bundled sample's request within the budget with its lists kept.

What the cap never touches: every scalar (``workload_percent``,
``assigned_queries``, wave ``query_count``/``table_count``/
``workload_share_percent``, costs), ``ranking``, ``tco_analysis``, the
deterministic summary, ``schema_design_status``, ``mitigation_strategies``, and
the risk entries themselves (every risk and resolved risk stays, with its
severity and description, so a stated risk count still matches): only the
lists inside each entry are cut. The summary post-checks
(``apply_synthesis_llm_output``) read the full deterministic result, never this
payload, so a table cut from a list here is still checked correctly.

The budget is measured on the larger of the two forms the payload takes: the
``indent=2`` ``llm_input.json`` headless reads (``headless_text``), not the
compact JSON the Bedrock prompt embeds, so a request within it is within it in
both modes. 24,000 estimated tokens at one token per three characters
(conservative for JSON, which tokenizes at roughly 3-4 characters per token),
and at most 2,000 lines, the Read tool's default page, so headless reads it in
one call. The compact Bedrock form is about 30% smaller, so with the instruction
text (about 2.5k tokens) a capped Bedrock prompt stays well under 14% of a
200k-token window. The wordpress sample's full request (about 11.2k tokens,
930 lines) is never capped; discourse (1,654 queries, about 73.8k uncapped)
is, to 25 items per list.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import Callable
from typing import Any

from src.agents.prompt_framing import with_untrusted_notice
from src.shared.progress import say

logger = logging.getLogger(__name__)

SUMMARY_INPUT_TOKEN_BUDGET = 24_000
# The Read tool's default page: headless reads a request this long in one call.
SUMMARY_INPUT_LINE_BUDGET = 2_000
CHARS_PER_TOKEN = 3
# Per-list limits tried in turn until the payload fits; 0 keeps only the totals.
_LIST_LIMITS = (200, 100, 50, 25, 12, 6, 3, 1, 0)


def headless_text(payload: object) -> str:
    """``payload`` as headless reads it: ``llm_input.json``, the store's ``indent=2`` JSON.

    The larger of the two forms (the Bedrock prompt embeds compact JSON), so a
    payload within the budget here is within it in both modes. A request dict is
    measured as it is written, led by its ``untrusted_data_notice`` (#1317), so the
    line and token caps hold for the file itself.
    """
    if isinstance(payload, dict):
        payload = with_untrusted_notice(payload)
    return json.dumps(payload, indent=2, default=str)


def headless_lines(payload: object) -> int:
    """Lines of ``llm_input.json`` for ``payload``."""
    return headless_text(payload).count("\n") + 1


def estimate_tokens(payload: object) -> int:
    """Estimated tokens of ``payload`` in its larger form (``headless_text``)."""
    return math.ceil(len(headless_text(payload)) / CHARS_PER_TOKEN)


def _bare(table: str) -> str:
    return str(table).rsplit(".", 1)[-1]


def table_query_weights(query_assignments: list[dict]) -> dict[str, int]:
    """In-scope queries touching each table (bare name), over the whole assignment.

    Every engine's queries count, not only the ones the request's
    ``query_groups`` carry (in synthesis those are the relational engine's
    groups, so a DynamoDB or DocumentDB table would otherwise weigh nothing
    and its lists would be cut alphabetically, #771 review).
    """
    weights: dict[str, int] = {}
    for qa in query_assignments:
        if qa.get("in_scope") is False:
            continue
        for t in {_bare(t) for t in qa.get("source_tables") or [] if t}:
            weights[t] = weights.get(t, 0) + 1
    return weights


def _group_weights(query_groups: list[dict]) -> dict[str, int]:
    """Fallback with no assignment: queries per table summed over the query groups."""
    weights: dict[str, int] = {}
    for g in query_groups:
        for t in g.get("tables") or []:
            weights[_bare(t)] = weights.get(_bare(t), 0) + int(g.get("query_count") or 0)
    return weights


# The ``<field>_total`` keys a cut adds next to a cut list: internal field
# names the summary may not name (``synthesis_grounding._INTERNAL_FIELD_NAMES``).
TOTAL_FIELD_NAMES = (
    "tables_total",
    "affected_tables_total",
    "table_owners_total",
    "table_groups_total",
    "source_tables_total",
    "target_tables_total",
    "recommended_engine_by_table_total",
    "trade_offs_total",
)


class _Capper:
    """Cuts every growing list of one payload to ``limit`` and records what it cut."""

    def __init__(self, limit: int, weights: dict[str, int]) -> None:
        self.limit = limit
        self.weights = weights
        self.capped: dict[str, dict] = {}

    def _by_weight(self, name: object) -> tuple[int, str]:
        return (-self.weights.get(_bare(str(name)), 0), str(name))

    def _record(self, path: str, total: int, kept: int) -> None:
        entry = self.capped.setdefault(
            path, {"path": path, "lists": 0, "items_total": 0, "items_kept": 0}
        )
        entry["lists"] += 1
        entry["items_total"] += total
        entry["items_kept"] += kept

    def cut(self, items: list, path: str, key: Callable | None = None) -> list | None:
        """``items`` cut to the limit (sorted by ``key`` first), or None when it fits."""
        if len(items) <= self.limit:
            return None
        ordered = sorted(items, key=key) if key else list(items)
        kept = ordered[: self.limit]
        self._record(path, len(items), len(kept))
        return kept

    def field(self, entry: dict | None, name: str, path: str, key: Callable | None = None) -> None:
        """Cut ``entry[name]`` in place, keeping its length as ``<name>_total``."""
        if not isinstance(entry, dict) or not isinstance(entry.get(name), list):
            return
        total = len(entry[name])
        kept = self.cut(entry[name], path, key)
        if kept is not None:
            entry[name] = kept
            entry[f"{name}_total"] = total

    def tables(self, entry: dict | None, name: str, path: str) -> None:
        self.field(entry, name, path, key=self._by_weight)


def _cap(payload: dict, limit: int, weights: dict[str, int]) -> tuple[dict, list[dict]]:
    out = copy.deepcopy(payload)
    c = _Capper(limit, weights)

    groups = out.get("query_groups") or []
    # A group an engine's ``top_query_groups`` names (ranked by throughput)
    # is kept first, so the request never names a group it then drops.
    pinned = {
        name
        for e in (out.get("effective_architecture") or {}).get("engines") or []
        for name in e.get("top_query_groups") or []
    }
    kept_groups = c.cut(
        groups,
        "query_groups",
        key=lambda g: (
            g.get("group_name") not in pinned,
            -(g.get("query_count") or 0),
            str(g.get("group_name")),
        ),
    )
    if kept_groups is not None:
        kept_names = {id(g) for g in kept_groups}
        c.capped["query_groups"]["omitted_query_count"] = sum(
            g.get("query_count") or 0 for g in groups if id(g) not in kept_names
        )
        out["query_groups"] = kept_groups
    for g in out.get("query_groups") or []:
        c.tables(g, "tables", "query_groups[].tables")

    ea = out.get("effective_architecture") or {}
    for e in ea.get("engines") or []:
        c.tables(e, "tables", "effective_architecture.engines[].tables")
    by_table = ea.get("recommended_engine_by_table")
    if isinstance(by_table, dict):
        kept_tables = c.cut(
            list(by_table),
            "effective_architecture.recommended_engine_by_table",
            key=c._by_weight,
        )
        if kept_tables is not None:
            ea["recommended_engine_by_table"] = {t: by_table[t] for t in kept_tables}
            ea["recommended_engine_by_table_total"] = len(by_table)

    risk = out.get("risk_assessment") or {}
    for section in ("risks", "resolved_risks"):
        for r in risk.get(section) or []:
            c.tables(r, "affected_tables", f"risk_assessment.{section}[].affected_tables")

    for w in out.get("migration_waves") or []:
        c.tables(w, "tables", "migration_waves[].tables")
        c.field(
            w,
            "table_owners",
            "migration_waves[].table_owners",
            key=lambda o: c._by_weight(o.get("table", "")),
        )
        c.field(
            w,
            "table_groups",
            "migration_waves[].table_groups",
            key=lambda tg: (-(tg.get("query_count") or 0), ",".join(tg.get("tables") or [])),
        )
        for tg in w.get("table_groups") or []:
            c.tables(tg, "tables", "migration_waves[].table_groups[].tables")

    # Trade-offs are prose and grow with every designed table (#1064): the ones
    # that touch the most queries stay, in a fixed order among equals.
    trade_offs = out.get("trade_offs") or []
    kept_trade_offs = c.cut(
        trade_offs,
        "trade_offs",
        key=lambda t: (
            -(t.get("query_count") or 0),
            str(t.get("engine")),
            str(t.get("description")),
        ),
    )
    if kept_trade_offs is not None:
        out["trade_offs"] = kept_trade_offs
        out["trade_offs_total"] = len(trade_offs)
    for t in out.get("trade_offs") or []:
        c.tables(t, "source_tables", "trade_offs[].source_tables")
        c.tables(t, "target_tables", "trade_offs[].target_tables")
    kept_mappings = c.cut(
        out.get("table_mappings") or [],
        "table_mappings",
        key=lambda m: c._by_weight(m.get("source_table", "")),
    )
    if kept_mappings is not None:
        out["table_mappings"] = kept_mappings

    return out, list(c.capped.values())


def apply_summary_input_budget(
    payload: dict, budget: int | None = None, table_weights: dict[str, int] | None = None
) -> dict:
    """``payload`` within ``budget`` estimated tokens (default ``SUMMARY_INPUT_TOKEN_BUDGET``).

    ``table_weights`` ranks tables in a cut (``table_query_weights`` over the
    assignment); without it, the request's own query groups are used.
    Returns ``payload`` itself when it already fits; otherwise a capped copy
    carrying ``input_budget`` (see the module docstring).
    """
    budget = SUMMARY_INPUT_TOKEN_BUDGET if budget is None else budget
    uncapped = estimate_tokens(payload)
    uncapped_lines = headless_lines(payload)
    if uncapped <= budget and uncapped_lines <= SUMMARY_INPUT_LINE_BUDGET:
        return payload
    weights = (
        table_weights
        if table_weights is not None
        else _group_weights(payload.get("query_groups") or [])
    )
    capped: dict = payload
    for limit in _LIST_LIMITS:
        capped, lists = _cap(payload, limit, weights)
        # The status counts toward the estimate. It is measured with its
        # widest values (the uncapped estimate and lines, ``false``), so filling in the
        # real ones can only shrink it: ``estimated_tokens`` is an upper bound.
        status: dict[str, Any] = {
            "budget_tokens": budget,
            "estimated_tokens_uncapped": uncapped,
            "estimated_tokens": uncapped,
            "lines": uncapped_lines,
            "list_limit": limit,
            "within_budget": False,
            "capped_lists": lists,
        }
        capped["input_budget"] = status
        status["estimated_tokens"] = estimate_tokens(capped)
        status["lines"] = headless_lines(capped)
        if status["estimated_tokens"] <= budget and status["lines"] <= SUMMARY_INPUT_LINE_BUDGET:
            status["within_budget"] = True
            break
    estimated = status["estimated_tokens"]
    message = (
        f"Summary input capped to {status['list_limit']} items per list: "
        f"~{uncapped} -> ~{estimated} estimated tokens, {status['lines']} lines "
        f"(budget {budget} tokens, {SUMMARY_INPUT_LINE_BUDGET} lines, "
        f"{len(status['capped_lists'])} list paths cut)"
    )
    if status["within_budget"]:
        logger.info(message)
        say(f"[synthesis] {message}")
    else:
        logger.warning("%s; still over the budget", message)
        say(f"[synthesis] WARNING: {message}; still over the budget")
    return capped
