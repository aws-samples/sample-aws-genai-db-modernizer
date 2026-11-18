# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The approval gate's status data for one assignment version (#1307, #2326, ADR-032).

``gate_block_facts`` (``src.shared.gate_facts``) computes the gate's figures from this. The
chat gate (``scripts/run_assessment.py``'s ``_reality_check_artifact``, which
``scripts/print_summary.py --gate`` prints) and the UI gate (``GET
/assessments/{job_id}/assignments``) both build it here, so both describe the version the
page states:

- With ``synthesis/v<N>/report.json`` for the version, the figures are the report's facts:
  each engine's in-scope queries (``engine_query_counts``), the removed engines, the queries
  with no engine in the scope, the source tables and the cache candidates.
- Before that, each engine's queries are the version's own in-scope queries, so a customer
  edit newer than the reality-check record counts as edited. An engine the reality check
  removed, or one that had queries before it, is dropped when the version leaves it no
  in-scope query.
- Without a reality-check record the version's own in-scope owners are both distributions,
  as the page's stand-in reality check has them.

The queries counted are the in-scope ones, the report's ``in_scope_query_count`` meaning;
``query_total_count`` adds the queries with no engine in the scope, as the report does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.shared.engine_elimination import in_scope_distribution
from src.shared.gate_facts import cache_candidate_reads

# The cache overlay fields the gate reads.
GATE_OVERLAY_FIELDS = (
    "query_count",
    "calls_per_second",
    "call_share_percent",
    "candidate_query_count",
    "candidate_call_share_percent",
)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def report_facts_for_version(store: Any, db: str, job_id: str, version: int) -> dict | None:
    """The ``facts`` of ``synthesis/v<version>/report.json``, or None when that report has
    none (not written yet, or written before the facts block)."""
    key = f"{db}/{job_id}/synthesis/v{version}/report.json"
    if not store.exists(key):
        return None
    report = store.read_json(key)
    facts = report.get("facts") if isinstance(report, Mapping) else None
    if not isinstance(facts, Mapping) or not isinstance(facts.get("engine_query_counts"), Mapping):
        return None
    return dict(facts)


def gate_status(
    store: Any,
    db: str,
    job_id: str,
    version: int,
    *,
    collector: Mapping[str, Any] | None = None,
    query_assignments: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The status data ``gate_block_facts`` reads, for assignment ``version``.

    ``query_assignments`` (the UI route's, with legacy cache owners moved) defaults to the
    stored version's. ``collector`` gives the cache candidates' call rates.
    ``consolidations`` is the reality check's own list, as recorded.
    """
    from src.agents.referee.engine_scope import scope_from_store
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        read_reality_check_output,
        schema_design_engines,
    )

    path = assignment_artifact_path(db, job_id, version)
    raw = store.read_json(path) if store.exists(path) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    qas = [
        qa
        for qa in (
            query_assignments
            if query_assignments is not None
            else raw.get("query_assignments") or []
        )
        if isinstance(qa, Mapping)
    ]
    rc = read_reality_check_output(store, db, job_id, version)
    after = in_scope_distribution(qas)
    gate: dict[str, Any] = {}
    if rc is not None:
        before = dict(rc.get("before_distribution") or {})
        settled = [str(e) for e in rc.get("eliminated_engines") or []]
        gate["consolidations"] = list(rc.get("consolidations") or [])
        if qas:
            # Re-checked against the version: an engine is dropped when it leaves the
            # engine no in-scope query (#235), whatever the record said.
            candidates = set(settled) | {str(e) for e, n in before.items() if _int(n)}
            eliminated = sorted(e for e in candidates if not after.get(e))
        else:
            # No readable version: the record's own result and its settled answer (#235).
            after, eliminated = dict(rc.get("after_distribution") or {}), settled
    else:
        before, eliminated = dict(after), []
    gate["before_distribution"] = before
    gate["after_distribution"] = after
    gate["eliminated_engines"] = eliminated
    scope = scope_from_store(store, db, job_id)
    if scope is not None:
        gate["engine_scope"] = sorted(scope)
    unservable = len(raw.get("unservable_queries") or [])
    if unservable:
        gate["unservable_query_count"] = unservable
    overlay = raw.get("cache_overlay")
    if isinstance(overlay, Mapping) and overlay:
        gate["cache_overlay"] = {k: overlay[k] for k in GATE_OVERLAY_FIELDS if k in overlay}
        queries = ((collector or {}).get("queries") or {}).get("query_patterns") or []
        reads = cache_candidate_reads(qas, queries)
        if reads:
            gate["cache_reads"] = reads
    gate["schema_design_engines"] = schema_design_engines(store, db, job_id, version)

    facts = report_facts_for_version(store, db, job_id, version)
    if facts is not None:
        _apply_report_facts(gate, facts)
    return gate


def _apply_report_facts(gate: dict[str, Any], facts: Mapping[str, Any]) -> None:
    """The figures the version's report states replace the ones counted from the version,
    so the gate and the deliverables agree (ADR-032)."""
    gate["after_distribution"] = {str(e): n for e, n in facts["engine_query_counts"].items()}
    gate["eliminated_engines"] = sorted(
        str(r["engine"])
        for r in facts.get("removed_engines") or []
        if isinstance(r, Mapping) and r.get("engine")
    )
    no_engine = _int(facts.get("no_engine_in_scope_query_count"))
    if no_engine:
        gate["unservable_query_count"] = no_engine
    else:
        gate.pop("unservable_query_count", None)
    tables = _int(facts.get("source_table_count"))
    if tables is not None:
        gate["source_table_count"] = tables
    cache = facts.get("cache_layer")
    if isinstance(cache, Mapping) and "cache_overlay" in gate:
        gate["cache_overlay"] = {
            **gate["cache_overlay"],
            "query_count": cache.get("designed_read_count"),
            "call_share_percent": cache.get("designed_call_share_percent"),
            "candidate_query_count": cache.get("candidate_read_count"),
            "candidate_call_share_percent": cache.get("candidate_call_share_percent"),
        }
