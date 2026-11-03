# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which risk belongs to a DynamoDB design overlap (#1527, #1780).

Synthesis raises one MEDIUM ``DATA_CONSISTENCY`` risk per overlap the group merge
leaves for review (#426), attributed to the overlap's engine and with the overlap's
source tables as ``affected_tables``. The merge gives each source table to one overlap
at most, so the engine and the source tables identify the risk. The derived copies'
stream sync (``facts.derived_copy_sync``) and the merge-review decisions
(``facts.dynamodb_merge_reviews``) both link an overlap to its risk with this one rule,
read from the risk's type, engine and affected tables, never from the text the
overlap and its risk share (ADR-032 Decision 6).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from src.shared.table_keys import normalize_table_name

DATA_CONSISTENCY = "DATA_CONSISTENCY"


def _keys(tables: Any) -> set[str]:
    values = tables if isinstance(tables, list | tuple | set | frozenset) else []
    return {normalize_table_name(t) for t in values if isinstance(t, str) and t}


def overlap_risk_id(
    engine: Any,
    source_tables: Iterable[Any],
    risks: Iterable[Mapping[str, Any]],
    taken: Collection[str] = (),
) -> str | None:
    """The id of the first open ``DATA_CONSISTENCY`` risk of ``engine`` whose affected
    tables are ``source_tables`` (compared by :func:`normalize_table_name`) and whose id
    is not in ``taken``, or None.

    The engine is the one a risk is attributed to, the ``[engine]`` its description
    starts with (as every deliverable reads it)."""
    sources = _keys(list(source_tables))
    if not sources:
        return None
    for risk in risks:
        if risk.get("risk_type") != DATA_CONSISTENCY or not risk.get("risk_id"):
            continue
        if str(risk["risk_id"]) in taken:
            continue
        if not str(risk.get("description") or "").startswith(f"[{engine}]"):
            continue
        if _keys(risk.get("affected_tables")) == sources:
            return str(risk["risk_id"])
    return None


def overlap_risk_ids(
    overlaps: Iterable[tuple[Any, Iterable[Any]]], risks: Iterable[Mapping[str, Any]]
) -> list[str | None]:
    """The risk of each overlap, given as ``(engine, source_tables)`` in the merge's
    order (``merge_overlaps``, the order synthesis raises their risks in): each risk goes
    to one overlap at most, so two reviews of the same source tables (one carried in
    from an earlier merge, with other targets) get their own risks, never one twice."""
    risks = list(risks)
    taken: set[str] = set()
    out: list[str | None] = []
    for engine, sources in overlaps:
        risk_id = overlap_risk_id(engine, sources, risks, taken)
        if risk_id is not None:
            taken.add(risk_id)
        out.append(risk_id)
    return out
