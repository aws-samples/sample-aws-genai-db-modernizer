# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The statements the collector left out of the workload: the ones AWS DMS ran on the
source (#2375) and the ones SQL Server ran on its own system catalogs (#1894). Their rule
ids, and the figures and sentence computed once from the collector output's
``queries.excluded_statements`` (ADR-032). Triage records the summary; the ``collect``
status line, the approval gate (``src.shared.gate_facts``) and every deliverable
(``facts.excluded_statements``) state it. The rules themselves are in
``src.tools.database.migration_tool_statements`` and
``src.tools.database.system_catalog_statements``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

DMS_OBJECT = "dms_object"
DMS_REPLICATION_SLOT = "dms_replication_slot"
DMS_CURSOR = "dms_cursor"
SQLSERVER_CATALOG = "sqlserver_catalog"
SQLSERVER_SYSTEM_PROCEDURE = "sqlserver_system_procedure"

# The order rules are reported in.
RULES = (
    DMS_OBJECT,
    DMS_REPLICATION_SLOT,
    DMS_CURSOR,
    SQLSERVER_CATALOG,
    SQLSERVER_SYSTEM_PROCEDURE,
)

# Who ran a rule's statements: AWS DMS, or SQL Server on its own catalogs.
DMS = "dms"
CATALOG = "catalog"
ORIGINS = {
    DMS_OBJECT: DMS,
    DMS_REPLICATION_SLOT: DMS,
    DMS_CURSOR: DMS,
    SQLSERVER_CATALOG: CATALOG,
    SQLSERVER_SYSTEM_PROCEDURE: CATALOG,
}
# How the sentence names each origin's statements, after their count.
_ORIGIN_WORDS = {DMS: "from AWS DMS", CATALOG: "that read only SQL Server's system catalogs"}


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word if count == 1 else word + 's'}"


def _sentence(counts: Mapping[str, int], executions: int) -> str:
    """``3 statements from AWS DMS, with 91 executions, were left out of the workload``;
    with both origins, ``1 statement from AWS DMS and 2 that read only ...``."""
    parts: list[str] = []
    for origin in (DMS, CATALOG):
        n = counts.get(origin, 0)
        if n:
            # The first part names the noun: "1 statement from AWS DMS and 2 that read ...".
            head = _plural(n, "statement") if not parts else f"{n:,}"
            parts.append(f"{head} {_ORIGIN_WORDS[origin]}")
    total = sum(counts.values())
    return (
        f"{' and '.join(parts)}, with {_plural(executions, 'execution')}, "
        f"{'was' if total == 1 else 'were'} left out of the workload"
    )


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
    by_origin: dict[str, int] = {}
    for r in rules:
        origin = ORIGINS[r["rule"]]
        by_origin[origin] = by_origin.get(origin, 0) + r["statement_count"]
    return {
        "statement_count": count,
        "execution_count": executions,
        "summary": _sentence(by_origin, executions),
        "rules": rules,
    }


def _records(collector: Mapping[str, Any] | None) -> list[Any]:
    queries = (collector or {}).get("queries") if isinstance(collector, Mapping) else None
    records = queries.get("excluded_statements") if isinstance(queries, Mapping) else None
    return list(records or [])


def excluded_statement_facts(collector: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What every deliverable and the approval gate state about the statements left out:
    ``statement_count``, ``execution_count`` and the one ``statement`` they print. None
    with no collection or nothing left out."""
    summary = excluded_statements_summary(_records(collector))
    if summary is None:
        return None
    return {
        "statement_count": summary["statement_count"],
        "execution_count": summary["execution_count"],
        "statement": summary["summary"] + "; referee-triage/triage.json lists them",
    }


def excluded_statement_count(collector: Mapping[str, Any] | None, origin: str | None = None) -> int:
    """How many statements the collector output records as left out; with ``origin``
    (:data:`DMS` or :data:`CATALOG`), only that origin's. The ``collect`` status line's
    ``dms_statements_excluded`` and ``catalog_statements_excluded``."""
    summary = excluded_statements_summary(_records(collector))
    if summary is None:
        return 0
    return sum(
        r["statement_count"]
        for r in summary["rules"]
        if origin is None or ORIGINS[r["rule"]] == origin
    )
