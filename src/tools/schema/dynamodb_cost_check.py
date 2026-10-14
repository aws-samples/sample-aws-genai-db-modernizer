"""DynamoDB hot-partition / capacity check, shared by Bedrock and external mode.

``src/skills/dynamodb-data-modeling.md`` only lets a design report
``validation_passed: true`` once this check has run successfully. It has two
callers that must agree exactly:

- Bedrock mode: the Strands tool ``compute_performances_and_costs`` in
  ``dynamodb_schema_agent.py`` is a thin wrapper around
  :func:`compute_performances_and_costs_entries`.
- External (Claude Code) mode: ``scripts/run_schema_design.py --check-costs
  <draft>`` calls :func:`check_draft_costs` on a group draft, which runs the
  same function per entry so it can report every failing entry at once.

Both also *compute* the arithmetic the skill no longer asks the model to do
(#626): ``partition_limit`` (3000 for reads, 1000 for writes),
``utilization_pct = min(100, rcu_or_wcu_per_second / partition_limit × 100)``
to one decimal, and ``at_risk = utilization_pct > 80``. The model supplies an
entry's table, operation, ``rcu_or_wcu_per_second``, contributing patterns
and mitigation; the returned ``results`` carry the computed three, which the
design copies. ``check_draft_costs`` reports a draft whose own values disagree
(or are missing), and also compares each access pattern's ``design_rps`` with
the group's ``design_metrics`` when the caller passes them.

This module deliberately has no Strands/Bedrock dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from src.contracts.design_metrics import DynamoDBPartitionLimits
from src.contracts.dynamodb_model_output import HotPartitionEntry
from src.tools.schema.design_metrics import (
    check_design_numbers,
    check_hot_partition_entry,
    expected_utilization_pct,
)

logger = logging.getLogger(__name__)

_PER_TABLE_FIELDS = (
    "table_name",
    "gsi_name",
    "operation",
    "rcu_or_wcu_per_second",
    "partition_limit",
    "utilization_pct",
    "at_risk",
)


def computed_entry(raw: Any) -> dict:
    """``raw`` with ``partition_limit``, ``utilization_pct`` and ``at_risk`` computed.

    Raises ``pydantic.ValidationError`` when the result is not a valid
    ``HotPartitionEntry`` (e.g. ``at_risk`` without a ``mitigation``).
    """
    entry = dict(raw) if isinstance(raw, dict) else {}
    limits = DynamoDBPartitionLimits()
    try:
        units = float(entry.get("rcu_or_wcu_per_second"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        units = None
    if units is not None and entry.get("operation") in ("read", "write"):
        limit = (
            limits.read_units_per_second
            if entry["operation"] == "read"
            else limits.write_units_per_second
        )
        utilization = expected_utilization_pct(units, limit)
        entry.update(
            partition_limit=limit,
            utilization_pct=utilization,
            at_risk=utilization > limits.at_risk_threshold_pct,
        )
    return HotPartitionEntry.model_validate(entry).model_dump(mode="json")


def compute_performances_and_costs_entries(raw_entries: list[Any]) -> list[dict]:
    """Compute and validate hot partition entries (see :func:`computed_entry`).

    Raises ``pydantic.ValidationError`` on the first invalid entry. Returns the
    entries as JSON-mode dicts, with the computed ``partition_limit``,
    ``utilization_pct`` and ``at_risk`` the design must use.
    """
    computed = [computed_entry(e) for e in raw_entries]
    logger.info("Computed %d hot partition entries", len(computed))
    return computed


def check_draft_costs(draft: dict, design_metrics: dict | None = None) -> dict:
    """Run :func:`compute_performances_and_costs_entries` over a draft's
    ``hot_partition_analysis`` and summarise the outcome.

    ``passed`` is true when every entry is valid once computed *and* the
    draft's own ``partition_limit`` / ``utilization_pct`` / ``at_risk`` match
    the computed ones (and, with ``design_metrics``, every ``design_rps``
    matches). The check is read-only: it never modifies ``draft``.

    Returns ``{"passed", "entry_count", "results", "per_table",
    "hot_partition_findings", "errors"}`` where ``results`` are the validated
    entries with the computed figures, ``per_table`` the capacity figures per table/GSI/operation,
    ``hot_partition_findings`` the at-risk entries with their mitigation, and
    ``errors`` one ``{"index", "error"}`` per rejected entry (``index`` is
    ``None`` when ``hot_partition_analysis`` itself is missing or not a list,
    or for an access pattern whose ``design_rps`` disagrees with
    ``design_metrics``).
    """
    entries = draft.get("hot_partition_analysis") if isinstance(draft, dict) else None
    if not isinstance(entries, list):
        return {
            "passed": False,
            "entry_count": 0,
            "results": [],
            "per_table": [],
            "hot_partition_findings": [],
            "errors": [{"index": None, "error": "hot_partition_analysis is missing or not a list"}],
        }

    results: list[dict] = []
    errors: list[dict] = []
    for index, entry in enumerate(entries):
        try:
            computed = computed_entry(entry)
        except ValidationError as exc:
            errors.append({"index": index, "error": str(exc)})
            continue
        results.append(computed)
        own = check_hot_partition_entry(entry, f"[{index}]") if isinstance(entry, dict) else []
        missing = [
            f
            for f in ("partition_limit", "utilization_pct", "at_risk")
            if not isinstance(entry, dict) or entry.get(f) is None
        ]
        if missing:
            own.append(
                f"hot_partition_analysis [{index}]: {', '.join(missing)} missing; "
                "copy them from this check's results"
            )
        if own:
            errors.append({"index": index, "error": "; ".join(own)})

    if design_metrics:
        # Hot-partition messages were reported per entry above; keep the rest.
        for message in check_design_numbers("dynamodb", draft, design_metrics):
            if not message.startswith("hot_partition_analysis"):
                errors.append({"index": None, "error": message})

    return {
        "passed": not errors,
        "entry_count": len(entries),
        "results": results,
        "per_table": [{k: r.get(k) for k in _PER_TABLE_FIELDS} for r in results],
        "hot_partition_findings": [
            {
                "table_name": r["table_name"],
                "gsi_name": r["gsi_name"],
                "operation": r["operation"],
                "utilization_pct": r["utilization_pct"],
                "mitigation": r["mitigation"],
                "contributing_patterns": r["contributing_patterns"],
            }
            for r in results
            if r["at_risk"]
        ],
        "errors": errors,
    }
