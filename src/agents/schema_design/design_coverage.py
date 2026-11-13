# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every statement a design input lists is named by the design (#2153).

ADR-027 (#1602) says every statement maps to the design that serves it. Before #2153
only a revision checked that (``revision_verifier.check_coverage``): a first design,
single-pass or merged from parts, could leave an assigned statement out of every access
pattern and still pass, and the report then showed it only as ungrouped.

The check runs wherever the scope check does (``handler.apply_schema_scope``: finalize,
``--merge``, the Bedrock designers and the external re-check), for the engines that
remodel the rows (:data:`COVERAGE_ENGINES`; Aurora reads a design view, not statements).
A statement is required when the engine's design input lists it: in scope for the
engine in the assignment, not a routine's scratch statement (both as
``filter_collector_for_assignment`` filters them), and touching a real source table (a
statement with none is ``excluded_queries``, never designed). An entry in
``unsupported_patterns`` does not count: it records that a query is not served. Each
statement left out is one ``Not designed by <engine>: ...`` message that names the part
whose input lists it, so the merge fix-up knows which draft to edit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.schema_design.group_merger import ENGINE_LIST_FIELDS
from src.agents.schema_design.revision_verifier import check_coverage
from src.agents.schema_design.scope import (
    COVERAGE_PREFIX,
    check_failed_message,
    covered_query_ids,
)
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: The engines whose designs must name every statement they are given: the remodelling
#: engines, the ones whose designs merge from parts (Aurora reads a design view).
COVERAGE_ENGINES = frozenset(ENGINE_LIST_FIELDS)


def required_query_ids(collector: Mapping[str, Any], assignment: dict, engine: str) -> list[str]:
    """The statements ``engine``'s design input lists that a design must name, in order."""
    from src.agents.schema_design.group_splitter import has_no_source_table
    from src.agents.schema_design.handler import filter_collector_for_assignment

    filtered = filter_collector_for_assignment(dict(collector), assignment, engine)
    ids: list[str] = []
    for q in (filtered.get("queries") or {}).get("query_patterns") or []:
        if isinstance(q, dict) and q.get("query_id") is not None and not has_no_source_table(q):
            ids.append(str(q["query_id"]))
    return list(dict.fromkeys(ids))


def _excluded(schema_output: Mapping[str, Any]) -> set[str]:
    return {
        str(q["query_id"])
        for q in schema_output.get("excluded_queries") or []
        if isinstance(q, Mapping) and q.get("query_id") is not None
    }


def coverage_violations(
    engine: str,
    schema_output: dict,
    required: Iterable[str],
    parts: Mapping[str, int] | None = None,
) -> list[str]:
    """One message per statement in ``required`` no access pattern of ``schema_output``
    names (``revision_verifier.check_coverage``). ``parts`` maps a statement to the
    part whose input lists it, for a design merged from parts."""
    if engine not in COVERAGE_ENGINES or not isinstance(schema_output, dict):
        return []
    excluded = _excluded(schema_output)
    wanted = [q for q in required if q not in excluded]
    covered = covered_query_ids(engine, schema_output)
    uncovered = [q for q in wanted if q not in covered]
    issues = check_coverage(schema_output, wanted, engine)  # one issue per uncovered id, in order
    messages: list[str] = []
    for qid, _issue in zip(uncovered, issues, strict=True):
        if not parts:
            where = "this engine's design input"
        elif qid in parts:
            where = f"group {parts[qid]}'s input"
        else:
            where = "no group's input"
        messages.append(
            f"{COVERAGE_PREFIX}{engine}: query '{qid}' is assigned to {engine} and listed in "
            f"{where}, but no access pattern names it. Design it in an access pattern "
            "(whatever its query_type); a trade-off or an unsupported_patterns entry does "
            "not serve it."
        )
    return messages


def input_parts(store: ArtifactStore, base_key: str) -> dict[str, int]:
    """Statement -> the part whose input lists it, for a design split into parts
    (``groups_manifest.json`` under ``base_key``); empty for a single-pass design."""
    from src.agents.schema_design.pattern_input import input_query_ids

    manifest_key = f"{base_key}/groups_manifest.json"
    if not store.exists(manifest_key):
        return {}
    manifest = store.read_json(manifest_key)
    parts: dict[str, int] = {}
    for group in manifest.get("groups") or []:
        idx = int(group["group_index"])
        try:
            data = store.read_json(f"{base_key}/input_group_{idx}.json")
        except Exception as exc:  # an unreadable input only loses the part's name
            logger.debug("No input for group %s: %s", idx, exc)
            data = None
        for qid in input_query_ids((data or {}).get("collector_output") or {}):
            parts.setdefault(qid, idx)
    return parts


def check_design_coverage(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    engine: str,
    schema_output: dict,
    assignment_version: int,
) -> list[str]:
    """The coverage messages of ``schema_output`` against assignment ``v{N}``.

    Empty for an engine outside :data:`COVERAGE_ENGINES`, a legacy version 0, a job
    with no assignment or collector output, or a skipped placeholder (no
    ``access_patterns``). A check that breaks fails closed (#2154): its one message names
    the error, so the design fails validation, as the scope check does; a contract version
    error is raised."""
    from src.shared.contract_versioning import ContractVersionError
    from src.storage.assignment_versioning import read_assignment
    from src.storage.collector_for_models import read_collector_for_models

    if engine not in COVERAGE_ENGINES or assignment_version <= 0:
        return []
    if not isinstance(schema_output, dict) or "access_patterns" not in schema_output:
        return []
    try:
        assignment = read_assignment(store, database_name, job_id, assignment_version)
        collector = read_collector_for_models(store, database_name, job_id, missing_ok=True)
        if assignment is None or not collector:
            return []
        required = required_query_ids(collector, assignment, engine)
        base_key = f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}"
        return coverage_violations(engine, schema_output, required, input_parts(store, base_key))
    except ContractVersionError:
        raise
    except Exception as exc:  # noqa: BLE001 - fails closed (#2154): a violation, not a pass
        logger.warning(
            "Coverage check failed for %s (assignment v%s): %s",
            engine,
            assignment_version,
            type(exc).__name__,
        )
        return [check_failed_message(COVERAGE_PREFIX, engine, exc)]
