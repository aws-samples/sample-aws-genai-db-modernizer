# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The statements AWS DMS ran on the source, which the collector left out of the workload
(#2375): their rule ids and the figures and sentence computed once from the collector
output's ``queries.excluded_statements`` (ADR-032). Triage records the summary; the
``collect`` status line, the approval gate (``src.shared.gate_facts``) and every
deliverable (``facts.excluded_statements``) state it. The rules themselves are in
``src.tools.database.migration_tool_statements``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

DMS_OBJECT = "dms_object"
DMS_REPLICATION_SLOT = "dms_replication_slot"
DMS_CURSOR = "dms_cursor"

# The order rules are reported in.
RULES = (DMS_OBJECT, DMS_REPLICATION_SLOT, DMS_CURSOR)


def excluded_statements_summary(excluded: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Triage's record of the statements the collector left out: how many, their
    executions, and per rule its reason and the statement ids. ``None`` when none were."""
    by_rule: dict[str, list[Mapping[str, Any]]] = {}
    for record in excluded:
        if isinstance(record, Mapping) and record.get("rule") in RULES:
            by_rule.setdefault(str(record["rule"]), []).append(record)
    if not by_rule:
        return None
    rules: list[dict[str, Any]] = []
    for rule in RULES:
        records = by_rule.get(rule)
        if not records:
            continue
        rules.append(
            {
                "rule": rule,
                "reasons": sorted({str(r.get("reason") or "") for r in records}),
                "statement_count": len(records),
                "execution_count": sum(int(r.get("execution_count") or 0) for r in records),
                "query_ids": [str(r.get("query_id")) for r in records],
            }
        )
    count = sum(r["statement_count"] for r in rules)
    executions = sum(r["execution_count"] for r in rules)
    return {
        "statement_count": count,
        "execution_count": executions,
        "summary": (
            f"{count:,} {'statement' if count == 1 else 'statements'} from AWS DMS, with "
            f"{executions:,} {'execution' if executions == 1 else 'executions'}, "
            f"{'was' if count == 1 else 'were'} left out of the workload"
        ),
        "rules": rules,
    }


def excluded_statement_facts(collector: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What every deliverable and the approval gate state about the statements left out:
    ``statement_count``, ``execution_count`` and the one ``statement`` they print. None
    with no collection or nothing left out."""
    queries = (collector or {}).get("queries") if isinstance(collector, Mapping) else None
    records = queries.get("excluded_statements") if isinstance(queries, Mapping) else None
    summary = excluded_statements_summary(records or [])
    if summary is None:
        return None
    return {
        "statement_count": summary["statement_count"],
        "execution_count": summary["execution_count"],
        "statement": summary["summary"] + "; referee-triage/triage.json lists them",
    }


def excluded_statement_count(collector: Mapping[str, Any] | None) -> int:
    """How many statements the collector output records as left out (the ``collect``
    status line's ``dms_statements_excluded``)."""
    facts = excluded_statement_facts(collector)
    return int(facts["statement_count"]) if facts else 0
