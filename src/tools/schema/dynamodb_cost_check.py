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

This module deliberately has no Strands/Bedrock dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from src.contracts.dynamodb_model_output import HotPartitionEntry

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


def compute_performances_and_costs_entries(raw_entries: list[Any]) -> list[dict]:
    """Validate hot partition entries against the ``HotPartitionEntry`` contract.

    Raises ``pydantic.ValidationError`` on the first invalid entry (e.g. an
    ``at_risk`` entry without a ``mitigation``). Returns the validated entries
    as JSON-mode dicts.
    """
    validated = [HotPartitionEntry.model_validate(e) for e in raw_entries]
    logger.info("Validated %d hot partition entries", len(validated))
    return [e.model_dump(mode="json") for e in validated]


def check_draft_costs(draft: dict) -> dict:
    """Run :func:`compute_performances_and_costs_entries` over a draft's
    ``hot_partition_analysis`` and summarise the outcome.

    ``passed`` is true exactly when the Strands tool would succeed on the same
    entries. The check is read-only: it never modifies ``draft``.

    Returns ``{"passed", "entry_count", "results", "per_table",
    "hot_partition_findings", "errors"}`` where ``results`` are the validated
    entries, ``per_table`` the capacity figures per table/GSI/operation,
    ``hot_partition_findings`` the at-risk entries with their mitigation, and
    ``errors`` one ``{"index", "error"}`` per rejected entry (``index`` is
    ``None`` when ``hot_partition_analysis`` itself is missing or not a list).
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
            results.extend(compute_performances_and_costs_entries([entry]))
        except ValidationError as exc:
            errors.append({"index": index, "error": str(exc)})

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
